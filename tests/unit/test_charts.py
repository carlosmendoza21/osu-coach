"""Unit tests for osu_coach.charts and GET /replays/{id}/charts.

The cursor grid is checked on small hand-made frame tables, the notes with a fake session
answering fixed rows, and the endpoint with the chart data replaced. The same code against
Postgres and real Parquet files is in tests/integration/test_analysis_live.py.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import polars as pl
import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach import charts
from osu_coach.api import replays as api
from osu_coach.charts import (
    GRID_COLS,
    GRID_ROWS,
    CursorGrid,
    NotePoint,
    ReplayCharts,
    Windows,
    cursor_grid,
    note_points,
    song_rate,
)
from osu_coach.db import get_session
from osu_coach.main import create_app
from osu_coach.models import Beatmap, Replay
from osu_coach.replays import FRAME_SCHEMA

PG = postgresql.dialect()  # type: ignore[no-untyped-call]  # SQLAlchemy leaves it untyped


def frames(*points: tuple[float, float]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "time_ms": list(range(len(points))),
            "x": [p[0] for p in points],
            "y": [p[1] for p in points],
            "keys": [0] * len(points),
        },
        schema=FRAME_SCHEMA,
    )


def cell(grid: CursorGrid, row: int, col: int) -> int:
    return grid.counts[row * grid.cols + col]


# ---------- cursor_grid ----------


def test_the_default_grid_is_8_px_cells() -> None:
    grid = cursor_grid(frames())

    assert (grid.cols, grid.rows) == (GRID_COLS, GRID_ROWS) == (64, 48)
    assert grid.counts == [0] * (64 * 48)
    assert grid.off_playfield == 0


@pytest.mark.parametrize(
    ("x", "y", "row", "col"),
    [
        (0, 0, 0, 0),  # the top-left corner
        (7.99, 7.99, 0, 0),  # still the first 8 x 8 cell
        (8, 0, 0, 1),  # the next one along
        (0, 8, 1, 0),  # the next one down
        (256, 192, 24, 32),  # the middle
        (511.99, 383.99, 47, 63),  # the bottom-right corner
    ],
)
def test_a_frame_lands_in_its_cell(x: float, y: float, row: int, col: int) -> None:
    grid = cursor_grid(frames((x, y)))

    assert cell(grid, row, col) == 1
    assert sum(grid.counts) == 1


@pytest.mark.parametrize(
    ("x", "y"),
    [
        (-0.01, 100),  # left of the playfield
        (512, 100),  # the right edge itself is outside (cells are 0-511)
        (100, -5),  # above
        (100, 384),  # the bottom edge
        (-50, 500),
    ],
)
def test_frames_off_the_playfield_are_counted_apart(x: float, y: float) -> None:
    grid = cursor_grid(frames((x, y), (10, 10)))

    assert sum(grid.counts) == 1  # only the one on the playfield
    assert grid.off_playfield == 1


def test_counts_add_up() -> None:
    points = [(10, 10)] * 5 + [(500, 300)] * 3 + [(600, 600)] * 2
    grid = cursor_grid(frames(*points))

    assert cell(grid, 1, 1) == 5
    assert cell(grid, 37, 62) == 3
    assert sum(grid.counts) + grid.off_playfield == len(points)


def test_another_grid_size() -> None:
    grid = cursor_grid(frames((0, 0), (511, 383), (256, 0)), cols=2, rows=2)

    assert grid.counts == [1, 1, 0, 1]  # rows from the top, left to right


# ---------- song_rate ----------


@pytest.mark.parametrize(
    ("mods", "rate"),
    [([], 1.0), (["HD"], 1.0), (["DT"], 1.5), (["HD", "NC"], 1.5), (["HT"], 0.75), (["DC"], 0.75)],
)
def test_song_rate(mods: list[str], rate: float) -> None:
    assert song_rate(mods) == rate


# ---------- note_points ----------


def replay_row(mods: list[str] | None = None, analyzed: bool = True) -> Replay:
    return Replay(
        id=7,
        beatmap_id=22423 if analyzed else None,
        analyzed_at=datetime(2026, 11, 12, tzinfo=UTC) if analyzed else None,
        mods=mods or [],
        frames_path="frames/x.parquet",
    )


class RowsSession:
    """Answers the notes query with fixed (hit, object) pairs, and keeps the statement."""

    def __init__(self, rows: list[tuple[Any, Any]]) -> None:
        self.rows = rows
        self.statement: Any = None

    async def execute(self, statement: Any) -> list[tuple[Any, Any]]:
        self.statement = statement
        return self.rows


def pair(idx: int, result: str, error: int | None, aim: float | None, y: float = 100) -> Any:
    hit = SimpleNamespace(idx=idx, result=result, error_ms=error, aim_error_px=aim)
    obj = SimpleNamespace(time_ms=1000 + idx, x=50.0, y=y, pattern="stream")
    return (hit, obj)


async def notes(rows: list[Any], mods: list[str] | None = None) -> tuple[list[NotePoint], Any]:
    session = RowsSession(rows)
    points = await note_points(cast(AsyncSession, session), replay_row(mods))
    return points, session.statement


async def test_a_note_point() -> None:
    [point], _ = await notes([pair(3, "100", -24, 11.785067859257326)])

    assert point == NotePoint(
        idx=3, time_ms=1003, result="100", error_ms=-24.0, aim_error_px=11.79,  # rounded
        x=50.0, y=100, pattern="stream",
    )  # fmt: skip


async def test_a_note_never_pressed() -> None:
    [point], _ = await notes([pair(0, "miss", None, None)])

    assert (point.result, point.error_ms, point.aim_error_px) == ("miss", None, None)


@pytest.mark.parametrize(
    ("mods", "error"),
    [
        ([], -15.0),
        (["DT"], -10.0),  # real time: 15 ms of song time at 1.5x is 10 ms
        (["HT"], -20.0),
        (["NC"], -10.0),
    ],
)
async def test_timing_is_in_real_time(mods: list[str], error: float) -> None:
    [point], _ = await notes([pair(0, "300", -15, 5.0)], mods)

    assert point.error_ms == error
    assert point.time_ms == 1000  # the time axis stays on the map's clock


async def test_errors_are_rounded_to_two_places() -> None:
    [point], _ = await notes([pair(0, "300", 10, 3.14159)], ["DT"])

    assert (point.error_ms, point.aim_error_px) == (6.67, 3.14)


@pytest.mark.parametrize(("mods", "y"), [([], 100), (["HR"], 284), (["HD", "HR"], 284)])
async def test_hr_flips_positions_as_the_player_saw_them(mods: list[str], y: float) -> None:
    [point], _ = await notes([pair(0, "300", 0, 1.0, y=100)], mods)

    assert point.y == y
    assert point.x == 50.0  # only up and down


async def test_the_query_joins_this_replays_map() -> None:
    _, statement = await notes([])

    sql = " ".join(str(statement.compile(dialect=PG)).split())
    assert (
        "JOIN hit_objects ON hit_objects.beatmap_id = %(beatmap_id_1)s::BIGINT "
        "AND hit_objects.idx = hit_results.idx"
    ) in sql
    assert "WHERE hit_results.replay_id = " in sql
    assert sql.endswith("ORDER BY hit_results.idx")  # map order, for a time axis
    params = statement.compile(dialect=PG).params
    assert params["beatmap_id_1"] == 22423 and params["replay_id_1"] == 7


# ---------- GET /replays/{id}/charts ----------

CHARTS = ReplayCharts(
    rate=1.5,
    windows=Windows(13.0, 39.67, 66.33),
    notes=[NotePoint(0, 1000, "300", -4.0, 2.5, 10.0, 20.0, "jump")],
    cursor=CursorGrid(2, 1, [3, 4], off_playfield=1),
)


class ChartsEnv:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
        self.row: Replay | None = replay_row()
        self.data_dir = data_dir
        self.calls: list[tuple[int, Path]] = []

        async def fake(session: Any, data_dir: Path, replay: Replay) -> ReplayCharts:
            self.calls.append((replay.id, data_dir))
            return CHARTS

        monkeypatch.setattr(api, "replay_charts", fake)

    async def get(self, path: str = "/replays/7/charts") -> httpx.Response:
        app = create_app()

        async def session() -> AsyncIterator[Any]:
            async def get(model: type, key: int) -> Replay | None:
                return self.row

            yield SimpleNamespace(get=get)

        app.dependency_overrides[get_session] = session
        app.dependency_overrides[api.get_data_dir] = lambda: self.data_dir
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)


@pytest.fixture
def cenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ChartsEnv:
    return ChartsEnv(monkeypatch, tmp_path)


async def test_the_charts(cenv: ChartsEnv) -> None:
    resp = await cenv.get()

    assert resp.status_code == 200
    assert resp.json() == {
        "rate": 1.5,
        "windows": {"great": 13.0, "ok": 39.67, "meh": 66.33},
        "notes": [
            {"idx": 0, "time_ms": 1000, "result": "300", "error_ms": -4.0, "aim_error_px": 2.5,
             "x": 10.0, "y": 20.0, "pattern": "jump"},
        ],
        "cursor": {"cols": 2, "rows": 1, "counts": [3, 4], "off_playfield": 1},
    }  # fmt: skip
    assert cenv.calls == [(7, cenv.data_dir)]  # with the configured data folder


async def test_an_unknown_replay_is_404(cenv: ChartsEnv) -> None:
    cenv.row = None

    resp = await cenv.get("/replays/999/charts")

    assert resp.status_code == 404
    assert resp.json() == {"detail": "replay 999 not found"}
    assert cenv.calls == []


@pytest.mark.parametrize(
    "row",
    [
        replay_row(analyzed=False),
        # Analyzed time without a map can't happen through the code, but must not crash.
        Replay(id=7, beatmap_id=None, analyzed_at=datetime(2026, 1, 1, tzinfo=UTC), mods=[]),
    ],
    ids=["not analyzed", "no map"],
)
async def test_a_replay_not_analyzed_is_409(cenv: ChartsEnv, row: Replay) -> None:
    cenv.row = row

    resp = await cenv.get()

    assert resp.status_code == 409
    assert resp.json() == {"detail": "replay 7 isn't analyzed yet"}
    assert cenv.calls == []  # no files read


def test_the_charts_endpoint_is_documented() -> None:
    operation = create_app().openapi()["paths"]["/replays/{replay_id}/charts"]["get"]

    assert set(operation["responses"]) == {"200", "404", "409", "422"}


async def test_replay_charts_puts_the_parts_together(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    frames((10, 10), (600, 10)).write_parquet(tmp_path / "f.parquet")
    row = replay_row(["DT"])
    row.frames_path = "f.parquet"

    async def points(session: Any, replay: Replay) -> list[NotePoint]:
        return [CHARTS.notes[0]]

    monkeypatch.setattr(charts, "note_points", points)

    async def get(model: type, key: int) -> Beatmap:
        return Beatmap(id=key, od=10, cs=4, ar=10, hp=5)

    session = cast(AsyncSession, SimpleNamespace(get=get))
    result = await charts.replay_charts(session, tmp_path, row)

    assert result.rate == 1.5
    assert result.notes == [CHARTS.notes[0]]
    assert (sum(result.cursor.counts), result.cursor.off_playfield) == (1, 1)


# ---------- hit_windows ----------


def beatmap(od: float) -> Beatmap:
    return Beatmap(id=1, od=od, cs=4, ar=9, hp=5)


@pytest.mark.parametrize(
    ("od", "mods", "windows"),
    [
        # 80 - 6 OD, 140 - 8 OD, 200 - 10 OD, each rounded down, minus half a ms.
        (10, [], Windows(19.5, 59.5, 99.5)),
        (9.5, [], Windows(22.5, 63.5, 104.5)),  # gwb's Split EX
        (7, [], Windows(37.5, 83.5, 129.5)),  # map 22423
        (7, ["HR"], Windows(20.5, 60.5, 101.5)),  # OD 7 x 1.4 = 9.8: 21.2, 61.6, 102
        (7, ["EZ"], Windows(58.5, 111.5, 164.5)),  # OD 3.5
        # Speed mods: the same song-time windows, passing faster or slower in real time.
        (7, ["DT"], Windows(25.0, 55.67, 86.33)),
        (7, ["HT"], Windows(50.0, 111.33, 172.67)),
        (10, ["HD", "NC"], Windows(13.0, 39.67, 66.33)),
    ],
)
def test_hit_windows(od: float, mods: list[str], windows: Windows) -> None:
    assert charts.hit_windows(beatmap(od), mods) == windows


async def test_replay_charts_without_its_map(tmp_path: Path) -> None:
    # Can't happen through the code (analyzed maps can't be deleted), but says so if it does.
    async def get(model: type, key: int) -> None:
        return None

    row = replay_row()
    with pytest.raises(LookupError, match="replay 7's map 22423 is missing"):
        await charts.replay_charts(cast(AsyncSession, SimpleNamespace(get=get)), tmp_path, row)
