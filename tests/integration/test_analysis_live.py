"""Integration tests for osu_coach.analysis against a real Postgres.

Run with:  uv run pytest -m integration
Needs:     Postgres (docker compose up -d postgres). Skips with the reason if it's missing.
Uses a separate `<db>_test` database (see conftest.py). Map 22423 is stored with the fake
osu! and file store, and replays on it are built by tests/osrdata.py, as in
tests/integration/test_replays_live.py, whose fixtures these share.
"""

from collections import Counter
from collections.abc import AsyncIterator, Collection
from pathlib import Path
from typing import cast

import httpx
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from osu_coach.analysis import (
    MapNotStoredError,
    ReplayNotFoundError,
    ReplayStats,
    analyze_replay,
    judged_counts,
    replay_stats,
    results_by_pattern,
    store_analysis,
)
from osu_coach.db import get_session
from osu_coach.judge import Judgement
from osu_coach.main import create_app
from osu_coach.osu.files import BeatmapFiles
from osu_coach.osufile import parse_osu
from tests.integration.test_replays_live import (
    MAP_22423,
    LookupOsu,
    db,
    empty_tables,
    fetch_map,
    rows,
    store,
)
from tests.osrdata import osr, play
from tests.support import FIXTURES
from tests.unit.test_beatmaps import FakeFiles

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


# ---------- analyze_replay ----------

MAP = parse_osu((FIXTURES / "22423.osu").read_text(encoding="utf-8"))
JUDGED = sum(1 for h in MAP.hit_objects if h.kind != "spinner")  # 251: 3 spinners


async def run_analysis(db: AsyncEngine, tmp_path: Path, replay_id: int) -> dict[str, object]:
    # A real session, which expires loaded rows on commit, as the worker's does.
    async with async_sessionmaker(db)() as session:
        files = cast(BeatmapFiles, FakeFiles())
        return await analyze_replay(session, files, tmp_path, replay_id)


async def played_replay(db: AsyncEngine, tmp_path: Path) -> int:
    """Map 22423 stored, and a stored replay playing it (taps 6 ms either side)."""
    await fetch_map(db, LookupOsu())
    frames = play(MAP.hit_objects, errors=(-6, 6))
    data = osr(checksum=MAP_22423, frames=frames, seed=None, counts=(JUDGED, 0, 0, 0, 0, 0))
    return (await store(db, tmp_path, data)).id


async def test_a_stored_replay_is_analyzed(db: AsyncEngine, engine: Engine, tmp_path: Path) -> None:
    replay_id = await played_replay(db, tmp_path)

    summary = await run_analysis(db, tmp_path, replay_id)

    assert summary["counts"] == {"300": JUDGED, "100": 0, "50": 0, "miss": 0}
    assert summary["matches_official"] is True  # the header was written with these counts
    assert rows(engine, "SELECT count(*) FROM hit_results") == [(JUDGED,)]
    [(beatmap_id, ur, analyzed)] = rows(
        engine, "SELECT beatmap_id, unstable_rate, analyzed_at IS NOT NULL FROM replays"
    )
    assert (beatmap_id, analyzed) == (22423, True)
    assert ur == pytest.approx(60, abs=0.1)  # 6 ms either side: a spread of 6, x 10


async def test_analyzing_twice_gives_the_same_rows(
    db: AsyncEngine, engine: Engine, tmp_path: Path
) -> None:
    replay_id = await played_replay(db, tmp_path)
    query = "SELECT idx, result, error_ms FROM hit_results ORDER BY idx"

    await run_analysis(db, tmp_path, replay_id)
    first = rows(engine, query)
    await run_analysis(db, tmp_path, replay_id)

    assert rows(engine, query) == first


async def test_a_replay_whose_map_isnt_stored(db: AsyncEngine, tmp_path: Path) -> None:
    replay_id = (await store(db, tmp_path, osr(checksum=MAP_22423))).id

    with pytest.raises(MapNotStoredError):
        await run_analysis(db, tmp_path, replay_id)


async def test_an_unknown_replay(db: AsyncEngine, tmp_path: Path) -> None:
    with pytest.raises(ReplayNotFoundError):
        await run_analysis(db, tmp_path, 999)


# ---------- judged_counts and GET /replays/{id} ----------


async def test_judged_counts(db: AsyncEngine, tmp_path: Path) -> None:
    replay_id = await setup(db, tmp_path)
    async with async_sessionmaker(db)() as session:
        assert await judged_counts(session, replay_id) is None  # not analyzed yet

    await analyze(db, replay_id, judgements(missed={1, 2, 3}))
    async with async_sessionmaker(db)() as session:
        assert await judged_counts(session, replay_id) == {
            "300": OBJECTS - 3, "100": 0, "50": 0, "miss": 3,
        }  # fmt: skip


async def get_replay(db: AsyncEngine, replay_id: int) -> httpx.Response:
    app = create_app()

    async def session() -> AsyncIterator[AsyncSession]:
        async with async_sessionmaker(db)() as s:
            yield s

    app.dependency_overrides[get_session] = session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(f"/replays/{replay_id}")


async def test_get_replay_against_postgres(db: AsyncEngine, tmp_path: Path) -> None:
    replay_id = await played_replay(db, tmp_path)

    pending = (await get_replay(db, replay_id)).json()
    assert (pending["status"], pending["judged"], pending["by_pattern"]) == ("pending", None, {})
    assert pending["beatmap"]["id"] == 22423  # stored already: found by checksum

    await run_analysis(db, tmp_path, replay_id)
    body = (await get_replay(db, replay_id)).json()

    assert body["status"] == "analyzed"
    assert body["judged"] == {"300": JUDGED, "100": 0, "50": 0, "miss": 0}
    assert body["matches_official"] is True
    assert body["replay"]["unstable_rate"] == pytest.approx(60, abs=0.1)
    assert sum(c["300"] for c in body["by_pattern"].values()) == JUDGED
    assert (await get_replay(db, 999)).status_code == 404
