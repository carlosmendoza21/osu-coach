"""Integration test: judging real replays gives osu!'s official 300/100/50/miss counts.

This is Day 4's goal. The official counts are the ones osu! wrote into each replay file.

Run with:  uv run pytest -m integration
Needs:     .osr files in data/replays/, Redis (docker compose up -d redis) and osu!
           credentials in .env (each replay's map is downloaded). Skips with the reason
           if any is missing. Maps download to pytest's temporary folder.
"""

import asyncio
import statistics
from pathlib import Path

import pytest

from osu_coach.difficulty import UnsupportedModsError, play_conditions
from osu_coach.judge import counts, judge
from osu_coach.osrfile import parse_replay
from osu_coach.osu.client import OsuClient
from osu_coach.osu.files import BeatmapFiles
from osu_coach.osufile import parse_osu
from tests.integration.test_osrfile_live import each_replay, files

__all__ = ["files"]  # the fixture, shared with the replay tests

pytestmark = pytest.mark.integration


@each_replay
async def test_our_counts_match_the_official_ones(
    path: Path, files: tuple[OsuClient, BeatmapFiles]
) -> None:
    osu, store = files
    replay = parse_replay(await asyncio.to_thread(path.read_bytes))
    beatmap = await osu.lookup_beatmap(replay.beatmap_checksum)
    map_path = await store.get(beatmap.id, replay.beatmap_checksum)
    osu_file = parse_osu(await asyncio.to_thread(map_path.read_text, encoding="utf-8"))
    if any(h.kind == "spinner" for h in osu_file.hit_objects):
        pytest.skip(f"map {beatmap.id} has spinners, which aren't judged yet")
    try:
        conditions = play_conditions(osu_file.difficulty, replay.mods)
    except UnsupportedModsError as e:
        pytest.skip(str(e))

    judgements = judge(osu_file, replay.frames, conditions)

    official = {
        "300": replay.count_300,
        "100": replay.count_100,
        "50": replay.count_50,
        "miss": replay.count_miss,
    }
    assert counts(judgements) == official
    # Every circle and slider head is judged once.
    assert [j.idx for j in judgements] == list(range(len(osu_file.hit_objects)))

    # The timing is a real player's: centered near 0 and spread like a top player's.
    errors = [j.error_ms for j in judgements if j.error_ms is not None]
    assert abs(statistics.mean(errors)) < 10
    assert 30 < statistics.pstdev(errors) * 10 < 200  # unstable rate
