"""Integration tests for osu_coach.analysis against a real Postgres.

Run with:  uv run pytest -m integration
Needs:     Postgres (docker compose up -d postgres). Skips with the reason if it's missing.
Uses a separate `<db>_test` database (see conftest.py). Map 22423 is stored with the fake
osu! and file store, and replays on it are built by tests/osrdata.py, as in
tests/integration/test_replays_live.py, whose fixtures these share.
"""

from collections import Counter
from collections.abc import Collection
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from osu_coach.analysis import ReplayStats, replay_stats, results_by_pattern, store_analysis
from osu_coach.judge import Judgement
from tests.integration.test_replays_live import (
    MAP_22423,
    LookupOsu,
    db,
    empty_tables,
    fetch_map,
    rows,
    store,
)
from tests.osrdata import osr

__all__ = ["db", "empty_tables"]  # the shared fixtures

pytestmark = pytest.mark.integration

OBJECTS = 254  # map 22423's hit objects


def judgements(
    missed: Collection[int] = (), errors: tuple[int, ...] = (-8, 0, 8)
) -> list[Judgement]:
    """A judged play of map 22423: every object hit, timing cycling through `errors`,
    except the ones in `missed`."""
    out = []
    for i in range(OBJECTS):
        if i in missed:
            out.append(Judgement(i, "circle", 1000, "miss"))
        else:
            e = errors[i % len(errors)]
            out.append(Judgement(i, "circle", 1000, "300", 1000 + e, e, 5.0 + i % 3))
    return out


async def setup(db: AsyncEngine, tmp_path: Path, score: int = 1) -> int:
    """Map 22423 with its objects, and a replay on it (a different file per `score`)."""
    await fetch_map(db, LookupOsu())
    return (await store(db, tmp_path, osr(checksum=MAP_22423, score=score))).id


async def analyze(db: AsyncEngine, replay_id: int, js: list[Judgement]) -> ReplayStats:
    stats = replay_stats(js)
    async with async_sessionmaker(db)() as session:
        await store_analysis(session, replay_id, 22423, js, stats)
    return stats


async def by_pattern(db: AsyncEngine, replay_id: int) -> dict[str, dict[str, int]]:
    async with async_sessionmaker(db)() as session:
        return await results_by_pattern(session, replay_id)


# ---------- Storing ----------


async def test_results_and_summary_are_stored(
    db: AsyncEngine, engine: Engine, tmp_path: Path
) -> None:
    replay_id = await setup(db, tmp_path)
    stats = await analyze(db, replay_id, judgements(missed={5, 6}))

    assert rows(engine, "SELECT count(*), count(error_ms) FROM hit_results") == [(254, 252)]
    [(beatmap_id, ur, mean, aim, analyzed)] = rows(
        engine,
        "SELECT beatmap_id, unstable_rate, mean_error_ms, mean_aim_error_px, "
        "analyzed_at IS NOT NULL FROM replays",
    )
    assert (beatmap_id, analyzed) == (22423, True)
    assert (ur, mean, aim) == pytest.approx(
        (stats.unstable_rate, stats.mean_error_ms, stats.mean_aim_error_px)
    )


async def test_a_row_round_trips(db: AsyncEngine, engine: Engine, tmp_path: Path) -> None:
    replay_id = await setup(db, tmp_path)
    forced = Judgement(1, "circle", 1000, "miss", forced=True)
    early = Judgement(2, "circle", 1000, "miss", 700, -300, 7.25)
    js = judgements()
    js[1:3] = [forced, early]
    await analyze(db, replay_id, js)

    assert rows(
        engine,
        "SELECT idx, result, hit_ms, error_ms, aim_error_px, forced FROM hit_results "
        "WHERE idx IN (0, 1, 2) ORDER BY idx",
    ) == [
        (0, "300", 992, -8, 5.0, False),
        (1, "miss", None, None, None, True),
        (2, "miss", 700, -300, 7.25, False),
    ]


async def test_analyzing_again_replaces_everything(
    db: AsyncEngine, engine: Engine, tmp_path: Path
) -> None:
    replay_id = await setup(db, tmp_path)
    await analyze(db, replay_id, judgements(missed={0, 1, 2}))
    # A second run (say, after the judging improves) judges fewer objects, all hit.
    second = judgements()[:200]
    stats = await analyze(db, replay_id, second)

    assert rows(engine, "SELECT count(*), count(*) FILTER (WHERE result = 'miss') "
                        "FROM hit_results") == [(200, 0)]  # fmt: skip
    [(ur,)] = rows(engine, "SELECT unstable_rate FROM replays")
    assert ur == pytest.approx(stats.unstable_rate)


async def test_other_replays_are_left_alone(
    db: AsyncEngine, engine: Engine, tmp_path: Path
) -> None:
    first = await setup(db, tmp_path, score=1)
    second = (await store(db, tmp_path, osr(checksum=MAP_22423, score=2))).id
    await analyze(db, first, judgements())
    await analyze(db, second, judgements(missed=set(range(100))))
    await analyze(db, first, judgements())  # again: must not touch the second's rows

    assert rows(engine, "SELECT replay_id, count(*) FILTER (WHERE result = 'miss') "
                        "FROM hit_results GROUP BY replay_id ORDER BY replay_id") == [
        (first, 0),
        (second, 100),
    ]  # fmt: skip


async def test_an_unanalyzed_replay_has_empty_columns(
    db: AsyncEngine, engine: Engine, tmp_path: Path
) -> None:
    await setup(db, tmp_path)

    assert rows(
        engine,
        "SELECT beatmap_id, unstable_rate, mean_error_ms, mean_aim_error_px, analyzed_at "
        "FROM replays",
    ) == [(None,) * 5]


# ---------- Keys ----------


async def test_deleting_a_replay_deletes_its_results(
    db: AsyncEngine, engine: Engine, tmp_path: Path
) -> None:
    replay_id = await setup(db, tmp_path)
    await analyze(db, replay_id, judgements())

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM replays"))
    assert rows(engine, "SELECT count(*) FROM hit_results") == [(0,)]


async def test_an_analyzed_map_cant_be_deleted(
    db: AsyncEngine, engine: Engine, tmp_path: Path
) -> None:
    # Like a map with scores: deleting it would leave the analysis pointing at nothing.
    replay_id = await setup(db, tmp_path)
    await analyze(db, replay_id, judgements())

    with pytest.raises(IntegrityError, match="replays_beatmap_id_fkey"), engine.begin() as c:
        c.execute(text("DELETE FROM beatmaps WHERE id = 22423"))


def test_results_need_a_replay(engine: Engine) -> None:
    with pytest.raises(IntegrityError, match="hit_results_replay_id_fkey"), engine.begin() as c:
        c.execute(text("INSERT INTO hit_results (replay_id, idx, result, forced) "
                       "VALUES (999, 0, '300', false)"))  # fmt: skip


def test_one_result_per_object(engine: Engine) -> None:
    with pytest.raises(IntegrityError, match="hit_results_pkey"), engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO replays (file_sha256, beatmap_checksum, player_name, mods, lazer, "
                "game_version, count_300, count_100, count_50, count_geki, count_katu, "
                "count_miss, score, max_combo, perfect, played_at, frame_count, frames_path) "
                "VALUES ('h', 'c', 'p', '{}', false, 1, 0, 0, 0, 0, 0, 0, 0, 0, false, now(), 1, "
                "'f')"
            )
        )
        c.execute(text("INSERT INTO hit_results (replay_id, idx, result, forced) "
                       "VALUES (1, 0, '300', false), (1, 0, 'miss', false)"))  # fmt: skip


# ---------- Results by pattern ----------


async def test_results_by_pattern(db: AsyncEngine, engine: Engine, tmp_path: Path) -> None:
    replay_id = await setup(db, tmp_path)
    missed = {3, 50, 51, 120, 200}
    await analyze(db, replay_id, judgements(missed=missed))

    # Worked out here from the stored objects' labels, object by object.
    stored = rows(engine, "SELECT idx, pattern FROM hit_objects WHERE beatmap_id = 22423")
    patterns = {int(str(idx)): str(pattern) for idx, pattern in stored}
    want: dict[str, Counter[str]] = {}
    for idx, pattern in patterns.items():
        want.setdefault(pattern, Counter())["miss" if idx in missed else "300"] += 1

    got = await by_pattern(db, replay_id)
    assert set(got) == set(want)
    for pattern, results in got.items():
        assert results == {"300": want[pattern]["300"], "100": 0, "50": 0,
                           "miss": want[pattern]["miss"]}  # fmt: skip
    assert sum(sum(r.values()) for r in got.values()) == OBJECTS


async def test_results_by_pattern_before_analysis(db: AsyncEngine, tmp_path: Path) -> None:
    replay_id = await setup(db, tmp_path)

    assert await by_pattern(db, replay_id) == {}
    assert await by_pattern(db, 999) == {}  # no such replay
