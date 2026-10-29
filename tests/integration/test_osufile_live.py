"""Integration test: the parser agrees with osu! on many real maps.

Run with:  uv run pytest -m integration
Needs:     Redis (docker compose up -d redis) and osu! credentials in .env. Skips with the
           reason if either is missing.
Maps come from peppy's best scores (old maps, old file formats) and the current #1
player's (new maps), so many file versions are covered. Downloads go to pytest's
temporary folder.
"""

from collections import Counter
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from redis.asyncio import Redis

from osu_coach.config import get_settings
from osu_coach.osu.auth import TokenProvider
from osu_coach.osu.client import OsuClient
from osu_coach.osu.files import BeatmapFiles
from osu_coach.osu.ratelimiter import RateLimiter
from osu_coach.osu.schemas import ScoreBeatmap
from osu_coach.osufile import parse_osu

pytestmark = pytest.mark.integration

MAPS_PER_PLAYER = 10


@pytest.fixture
async def maps(tmp_path: Path) -> AsyncIterator[list[tuple[ScoreBeatmap, Path]]]:
    """Real maps and their downloaded .osu files."""
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
        files = BeatmapFiles(http, limiter, tmp_path / "beatmaps")
        ranking = await osu._get("/rankings/osu/performance")
        top_player = ranking.json()["ranking"][0]["user"]["id"]

        found: dict[int, ScoreBeatmap] = {}
        for player in (2, top_player):  # peppy: old maps; the #1 player: new ones
            for score in await osu.get_best_scores(player, limit=MAPS_PER_PLAYER):
                found[score.beatmap.id] = score.beatmap
        downloaded = [(b, await files.get(b.id, b.checksum)) for b in found.values()]
        yield downloaded
    await redis.aclose()


async def test_parser_agrees_with_osu(maps: list[tuple[ScoreBeatmap, Path]]) -> None:
    versions: set[int] = set()
    for beatmap, path in maps:
        parsed = parse_osu(path.read_text(encoding="utf-8"))
        versions.add(parsed.format_version)
        kinds = Counter(o.kind for o in parsed.hit_objects)
        where = f"map {beatmap.id} (file format v{parsed.format_version})"

        assert (kinds["circle"], kinds["slider"], kinds["spinner"]) == (
            beatmap.count_circles,
            beatmap.count_sliders,
            beatmap.count_spinners,
        ), where
        d = parsed.difficulty
        # AR included: for old formats it comes from OD, as osu!'s own value does.
        assert (d.ar, d.od, d.cs, d.hp) == pytest.approx(
            (beatmap.ar, beatmap.od, beatmap.cs, beatmap.hp)
        ), where
        times = [o.time_ms for o in parsed.hit_objects]
        assert times == sorted(times), where

    assert len(maps) >= MAPS_PER_PLAYER, "expected maps from both players"
    assert len(versions) >= 2, f"expected several file format versions, got {versions}"
