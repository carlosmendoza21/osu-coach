"""Integration tests: real replays from data/replays/ read correctly and fit their maps.

Run with:  uv run pytest -m integration
Needs:     .osr files in data/replays/ (gitignored: they're other players' plays). The
           map checks also need Redis (docker compose up -d redis) and osu! credentials
           in .env. Each skips with the reason if what it needs is missing.
Maps download to pytest's temporary folder.
"""

import asyncio
import re
from collections.abc import AsyncIterator
from itertools import pairwise
from pathlib import Path

import httpx
import pytest
from redis.asyncio import Redis

from osu_coach.config import get_settings
from osu_coach.osrfile import ReplayFile, parse_replay
from osu_coach.osu.auth import TokenProvider
from osu_coach.osu.client import OsuClient
from osu_coach.osu.files import BeatmapFiles
from osu_coach.osu.ratelimiter import RateLimiter
from osu_coach.osufile import parse_osu
from tests.support import ROOT

pytestmark = pytest.mark.integration

REPLAYS = sorted((ROOT / "data" / "replays").glob("*.osr"))
# The osu! website names downloads solo-replay-osu_<beatmap id>_<score id>.osr.
WEBSITE_NAME = re.compile(r"solo-replay-osu_(\d+)_(\d+)\.osr")
NO_REPLAYS = pytest.param(
    None, marks=pytest.mark.skip(reason="no .osr files in data/replays/"), id="none"
)
each_replay = pytest.mark.parametrize(
    "path", [pytest.param(p, id=p.stem) for p in REPLAYS] or [NO_REPLAYS]
)


def presses(replay: ReplayFile) -> int:
    """How many times a key went down. K1 and K2 also set M1 and M2, so bits 1|2 cover all."""
    return sum(1 for a, b in pairwise(replay.frames) if b.keys & ~a.keys & 3)


@pytest.fixture
async def files(tmp_path: Path) -> AsyncIterator[tuple[OsuClient, BeatmapFiles]]:
    s = get_settings()
    if not s.osu_client_id or not s.osu_client_secret.get_secret_value():
        pytest.skip("OSU_CLIENT_ID / OSU_CLIENT_SECRET not set in .env")
    redis: Redis = Redis.from_url(s.redis_url)
    try:
        await redis.ping()
    except Exception:
        await redis.aclose()
        pytest.skip(f"Redis not reachable at {s.redis_url}. Start it: docker compose up -d redis")

    async with httpx.AsyncClient(timeout=20) as http:
        limiter = RateLimiter(redis)
        osu = OsuClient(
            http, TokenProvider(http, redis, s.osu_client_id, s.osu_client_secret), limiter
        )
        yield osu, BeatmapFiles(http, limiter, tmp_path / "beatmaps")
    await redis.aclose()


# ---------- The file alone ----------


@each_replay
def test_a_real_replay_reads(path: Path) -> None:
    replay = parse_replay(path.read_bytes())

    assert re.fullmatch(r"[0-9a-f]{32}", replay.beatmap_checksum)
    assert replay.player
    assert len(replay.frames) > 100
    times = [f.time_ms for f in replay.frames]
    assert all(a <= b for a, b in pairwise(times)), "frame times must never go back"
    # Cursor positions stay near the playfield (512 x 384); players do overshoot a bit.
    assert all(-512 <= f.x <= 1024 and -384 <= f.y <= 768 for f in replay.frames)
    assert replay.played_at.year >= 2007  # osu! came out in 2007


@each_replay
def test_a_website_download_has_its_score_id(path: Path) -> None:
    named = WEBSITE_NAME.fullmatch(path.name)
    if not named:
        pytest.skip(f"{path.name} isn't named like a website download")

    assert parse_replay(path.read_bytes()).score_id == int(named[2])


# ---------- Against the map, from osu! ----------


@each_replay
async def test_a_real_replay_fits_its_map(
    path: Path, files: tuple[OsuClient, BeatmapFiles]
) -> None:
    osu, store = files
    replay = parse_replay(await asyncio.to_thread(path.read_bytes))

    beatmap = await osu.lookup_beatmap(replay.beatmap_checksum)
    if named := WEBSITE_NAME.fullmatch(path.name):
        assert beatmap.id == int(named[1]), "the checksum names the map in the file name"
    assert beatmap.mode == "osu"
    map_path = await store.get(beatmap.id, replay.beatmap_checksum)
    osu_file = parse_osu(await asyncio.to_thread(map_path.read_text))
    objects = osu_file.hit_objects

    # Every judged object is a 300, 100, 50 or miss; a failed play judges fewer.
    judged = replay.count_300 + replay.count_100 + replay.count_50 + replay.count_miss
    assert judged <= len(objects)
    if judged == len(objects):
        # A finished play: the frames run from before the first object to past the last.
        assert replay.frames[0].time_ms <= objects[0].time_ms
        assert replay.frames[-1].time_ms >= objects[-1].time_ms
    if replay.perfect:
        # A full combo hit every circle and slider head, and each needs a key press.
        clicked = sum(1 for o in objects if o.kind != "spinner")
        assert presses(replay) >= clicked
