"""Unit tests for osu_coach.analysis.

The stats are checked on hand-made judgements; storing is checked with a fake session that
records each statement compiled to Postgres SQL. analyze_replay runs on map 22423 (the
saved fixture) with replays built to play it (tests/osrdata.py). The same code against
Postgres is in
tests/integration/test_analysis_live.py.
"""

import statistics
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach import analysis
from osu_coach.analysis import (
    INSERT_BATCH,
    MapNotStoredError,
    ReplayNotFoundError,
    ReplayStats,
    analyze_replay,
    replay_stats,
    results_by_pattern,
    store_analysis,
)
from osu_coach.difficulty import HitResult, UnsupportedModsError
from osu_coach.judge import Judgement
from osu_coach.models import Replay
from osu_coach.osrfile import parse_replay
from osu_coach.osu.files import BeatmapFiles
from osu_coach.osufile import parse_osu
from osu_coach.replays import frames_frame
from tests.osrdata import RawFrame, osr, play
from tests.support import FIXTURES, load

PG = postgresql.dialect()  # type: ignore[no-untyped-call]  # SQLAlchemy leaves it untyped


def hit(
    error_ms: int, distance_px: float = 10.0, result: HitResult = "300", idx: int = 0
) -> Judgement:
    return Judgement(
        idx,
        "circle",
        1000,
        result,
        1000 + error_ms,
        error_ms,
        distance_px,
    )


def miss(idx: int = 0, pressed_ms: int | None = None) -> Judgement:
    """A miss: never pressed, or (pressed_ms) pressed too early inside 400 ms."""
    if pressed_ms is None:
        return Judgement(idx, "circle", 1000, "miss")
    return Judgement(idx, "circle", 1000, "miss", 1000 + pressed_ms, pressed_ms, 5.0)


# ---------- replay_stats ----------


def test_stats() -> None:
    judgements = [hit(-10, 4), hit(0, 8), hit(10, 12), hit(20, 16, "100"), miss()]

    stats = replay_stats(judgements)

    assert stats.counts == {"300": 3, "100": 1, "50": 0, "miss": 1}
    assert stats.mean_error_ms == 5  # (-10 + 0 + 10 + 20) / 4
    assert stats.unstable_rate == pytest.approx(statistics.pstdev([-10, 0, 10, 20]) * 10)
    assert stats.unstable_rate == pytest.approx(111.80, abs=0.01)
    assert stats.mean_aim_error_px == 10  # (4 + 8 + 12 + 16) / 4


def test_ur_is_the_population_spread() -> None:
    # osu!'s UR: the spread of these hits (divide by n), not an estimate (n - 1).
    stats = replay_stats([hit(-5), hit(5)])

    assert stats.unstable_rate == 50  # pstdev([-5, 5]) = 5; stdev would give 70.7


def test_misses_have_no_timing_or_aim() -> None:
    # An early press that only got a miss isn't a hit: its timing and aim don't count.
    with_misses = replay_stats([hit(-4), hit(4), miss(1, pressed_ms=-300), miss(2)])
    without = replay_stats([hit(-4), hit(4)])

    assert with_misses.unstable_rate == without.unstable_rate == 40
    assert with_misses.mean_error_ms == without.mean_error_ms == 0
    assert with_misses.mean_aim_error_px == without.mean_aim_error_px == 10
    assert with_misses.counts["miss"] == 2


@pytest.mark.parametrize(
    ("rate", "ur", "mean"),
    [
        (1.0, 60, 6),
        (1.5, 40, 4),  # DT: 15 ms of song time is 10 ms of the player's
        (0.75, 80, 8),  # HT: song time passes slower than real time
    ],
)
def test_timing_is_in_real_time(rate: float, ur: float, mean: float) -> None:
    stats = replay_stats([hit(0), hit(12)], rate=rate)

    assert stats.unstable_rate == pytest.approx(ur)
    assert stats.mean_error_ms == pytest.approx(mean)
    assert stats.mean_aim_error_px == 10  # aim isn't about time: unchanged


@pytest.mark.parametrize(
    "judgements",
    [[], [miss()], [hit(5)], [hit(5), miss(1)]],
    ids=["nothing", "all misses", "one hit", "one hit and a miss"],
)
def test_fewer_than_two_hits_give_no_stats(judgements: list[Judgement]) -> None:
    stats = replay_stats(judgements)

    assert (stats.unstable_rate, stats.mean_error_ms, stats.mean_aim_error_px) == (None,) * 3


def test_50s_and_100s_count_as_hits() -> None:
    stats = replay_stats([hit(-90, result="50"), hit(50, result="100")])

    assert stats.mean_error_ms == -20


# ---------- store_analysis ----------


class FakeSession:
    """Records statements (as Postgres SQL, with their rows) and commits."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.statements: list[tuple[Any, Any]] = []

    async def execute(self, statement: Any, params: Any = None) -> None:
        self.statements.append((statement, params))
        sql = str(statement.compile(dialect=PG)).split()
        self.events.append(" ".join(sql[:3]))  # e.g. "DELETE FROM hit_results"

    async def commit(self) -> None:
        self.events.append("commit")


STATS = ReplayStats({"300": 2, "100": 0, "50": 0, "miss": 1}, 40.0, 0.0, 10.0)


async def store(session: FakeSession, judgements: list[Judgement]) -> None:
    await store_analysis(cast(AsyncSession, session), 7, 22423, judgements, STATS)


async def test_storing_replaces_the_results_in_one_transaction() -> None:
    session = FakeSession()
    await store(session, [hit(-4, idx=0), hit(4, idx=1), miss(idx=2)])

    assert session.events == [
        "DELETE FROM hit_results",
        "INSERT INTO hit_results",
        "UPDATE replays SET",
        "commit",
    ]
    delete = session.statements[0][0].compile(dialect=PG)
    assert "WHERE hit_results.replay_id = " in str(delete)
    assert list(delete.params.values()) == [7]  # only this replay's results


async def test_each_judgement_is_a_row() -> None:
    session = FakeSession()
    await store(session, [hit(-4, 3.5, idx=0), miss(idx=1), miss(idx=2, pressed_ms=-300)])

    rows = session.statements[1][1]
    assert rows == [
        {"replay_id": 7, "idx": 0, "result": "300", "hit_ms": 996, "error_ms": -4,
         "aim_error_px": 3.5, "forced": False},
        {"replay_id": 7, "idx": 1, "result": "miss", "hit_ms": None, "error_ms": None,
         "aim_error_px": None, "forced": False},
        # An early miss keeps its press: when, how early, and where the cursor was.
        {"replay_id": 7, "idx": 2, "result": "miss", "hit_ms": 700, "error_ms": -300,
         "aim_error_px": 5.0, "forced": False},
    ]  # fmt: skip


async def test_the_forced_flag_is_kept() -> None:
    session = FakeSession()
    forced = Judgement(0, "circle", 1000, "miss", forced=True)
    await store(session, [forced])

    assert session.statements[1][1][0]["forced"] is True


async def test_the_summary_goes_on_the_replay() -> None:
    session = FakeSession()
    await store(session, [hit(0)])

    update = session.statements[2][0].compile(dialect=PG)
    assert "WHERE replays.id = " in str(update)
    assert "analyzed_at=now()" in str(update)  # the database's clock
    assert update.params == {
        "beatmap_id": 22423,
        "unstable_rate": 40.0,
        "mean_error_ms": 0.0,
        "mean_aim_error_px": 10.0,
        "id_1": 7,
    }


@pytest.mark.parametrize(
    ("objects", "batches"),
    [
        (0, []),  # nothing judged (a map with only spinners): no insert at all
        (1, [1]),
        (INSERT_BATCH, [INSERT_BATCH]),
        (INSERT_BATCH + 1, [INSERT_BATCH, 1]),  # one row over: a second insert
        (2856, [1000, 1000, 856]),  # Eye Of Eternity's circles and slider heads
    ],
)
async def test_rows_are_inserted_in_batches(objects: int, batches: list[int]) -> None:
    session = FakeSession()
    await store(session, [hit(0, idx=i) for i in range(objects)])

    inserts = [p for s, p in session.statements if str(s).startswith("INSERT")]
    assert [len(p) for p in inserts] == batches
    assert [r["idx"] for p in inserts for r in p] == list(range(objects))  # each once, in order
    assert session.events[-1] == "commit"


# ---------- results_by_pattern ----------


class QuerySession:
    """Answers the by-pattern query with fixed rows, and keeps the statement."""

    def __init__(self, rows: list[tuple[str, str, int]]) -> None:
        self.rows = rows
        self.statement: Any = None

    async def execute(self, statement: Any) -> list[tuple[str, str, int]]:
        self.statement = statement
        return self.rows


async def test_results_by_pattern() -> None:
    session = QuerySession([("stream", "300", 532), ("stream", "miss", 8), ("jump", "300", 115)])

    by_pattern = await results_by_pattern(cast(AsyncSession, session), 1)

    # Every result is listed for each pattern, 0 where none happened.
    assert by_pattern == {
        "stream": {"300": 532, "100": 0, "50": 0, "miss": 8},
        "jump": {"300": 115, "100": 0, "50": 0, "miss": 0},
    }


async def test_results_by_pattern_query() -> None:
    session = QuerySession([])

    assert await results_by_pattern(cast(AsyncSession, session), 1) == {}

    sql = " ".join(str(session.statement.compile(dialect=PG)).split())
    # Through the map the replay was analyzed against, object by object.
    assert "JOIN replays ON replays.id = hit_results.replay_id" in sql
    assert (
        "JOIN hit_objects ON hit_objects.beatmap_id = replays.beatmap_id "
        "AND hit_objects.idx = hit_results.idx"
    ) in sql
    assert "WHERE hit_results.replay_id = " in sql
    assert "GROUP BY hit_objects.pattern, hit_results.result" in sql


# ---------- analyze_replay ----------

MAP_22423 = parse_osu((FIXTURES / "22423.osu").read_text(encoding="utf-8"))
CHECKSUM_22423 = load("beatmap.json")["checksum"]
JUDGED = sum(1 for h in MAP_22423.hit_objects if h.kind != "spinner")  # 251: 3 spinners


class AnalyzeEnv:
    """A stored replay of map 22423 (its frames really written to Parquet), a fake
    session that only loads it, and the stored-map check and the storing replaced."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.map_stored = True
        self.stored: list[tuple[int, int, list[Judgement], ReplayStats]] = []
        self.file_gets: list[tuple[int, str | None]] = []

        async def stored_map(session: Any, checksum: str) -> int | None:
            return 22423 if self.map_stored else None

        async def store(
            session: Any, replay_id: int, beatmap_id: int, js: Any, stats: ReplayStats
        ) -> None:
            self.stored.append((replay_id, beatmap_id, list(js), stats))

        monkeypatch.setattr(analysis, "stored_map", stored_map)
        monkeypatch.setattr(analysis, "store_analysis", store)
        self.replay(frames=play(MAP_22423.hit_objects))

    def replay(
        self, frames: list[RawFrame], mods: list[str] | None = None, counts: tuple[int, ...] = ()
    ) -> None:
        table = frames_frame(parse_replay(osr(frames=frames, seed=None)).frames)
        table.write_parquet(self.data_dir / "frames.parquet")
        c300, c100, c50, cmiss = counts or (254, 0, 0, 0)
        self.row: Replay | None = Replay(
            id=7, beatmap_checksum=CHECKSUM_22423, mods=mods or [], frames_path="frames.parquet",
            count_300=c300, count_100=c100, count_50=c50, count_miss=cmiss,
        )  # fmt: skip

    async def get(self, model: type, key: int) -> Replay | None:
        return self.row

    async def file(self, beatmap_id: int, checksum: str | None) -> Path:
        self.file_gets.append((beatmap_id, checksum))
        return FIXTURES / "22423.osu"

    async def run(self, replay_id: int = 7) -> dict[str, Any]:
        files = cast(BeatmapFiles, SimpleNamespace(get=self.file))
        session = cast(AsyncSession, SimpleNamespace(get=self.get))
        return await analyze_replay(session, files, self.data_dir, replay_id)


@pytest.fixture
def aenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AnalyzeEnv:
    return AnalyzeEnv(monkeypatch, tmp_path)


async def test_analyze_a_replay(aenv: AnalyzeEnv) -> None:
    summary = await aenv.run()

    [(replay_id, beatmap_id, js, stats)] = aenv.stored
    assert (replay_id, beatmap_id, len(js)) == (7, 22423, JUDGED)
    assert stats.counts == {"300": JUDGED, "100": 0, "50": 0, "miss": 0}
    assert summary == {
        "replay_id": 7,
        "beatmap_id": 22423,
        "counts": stats.counts,
        # osu! also counts the 3 spinners, which aren't judged yet: no match on this map.
        "matches_official": False,
        "unstable_rate": 0.0,
    }
    # The map's file is asked for with the replay's checksum, so a wrong copy is refused.
    assert aenv.file_gets == [(22423, CHECKSUM_22423)]


async def test_matches_official_compares_every_count(aenv: AnalyzeEnv) -> None:
    # Skipping object 4: the next tap (object 5's, 107 ms later) lands on 4, which is
    # stacked under it and still inside its 50 window, so 4 is a 50 and 5 a miss.
    skipped = play(MAP_22423.hit_objects, missed=(4,))
    aenv.replay(skipped, counts=(JUDGED - 2, 0, 1, 1))
    assert (await aenv.run())["matches_official"] is True

    aenv.replay(skipped, counts=(JUDGED - 2, 1, 0, 1))  # a 100 where we judged a 50
    assert (await aenv.run())["matches_official"] is False


async def test_speed_mods_reach_the_stats(aenv: AnalyzeEnv) -> None:
    aenv.replay(play(MAP_22423.hit_objects, errors=(-6, 6)), mods=["DT"])

    await aenv.run()

    ur = aenv.stored[0][3].unstable_rate
    assert ur == pytest.approx(40, abs=0.1)  # 60 in song time, / 1.5 in real time


async def test_hr_flips_the_map_when_judging(aenv: AnalyzeEnv) -> None:
    # Taps where the file says, but with HR the map was flipped: nothing is hit.
    aenv.replay(play(MAP_22423.hit_objects), mods=["HR"])

    await aenv.run()

    counts = aenv.stored[0][3].counts
    assert counts["300"] < JUDGED // 4  # only circles near the middle line still hit


async def test_an_unknown_replay(aenv: AnalyzeEnv) -> None:
    aenv.row = None

    with pytest.raises(ReplayNotFoundError, match="no replay 99"):
        await aenv.run(99)
    assert aenv.stored == []


async def test_the_map_must_be_stored(aenv: AnalyzeEnv) -> None:
    aenv.map_stored = False

    with pytest.raises(MapNotStoredError, match="replay 7's map isn't stored yet"):
        await aenv.run()
    assert aenv.file_gets == [] and aenv.stored == []


async def test_unsupported_mods_store_nothing(aenv: AnalyzeEnv) -> None:
    aenv.replay(play(MAP_22423.hit_objects), mods=["RX"])

    with pytest.raises(UnsupportedModsError, match="RX"):
        await aenv.run()
    assert aenv.stored == []
