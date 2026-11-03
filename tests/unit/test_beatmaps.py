"""Unit tests for osu_coach.beatmaps: fetching, checking and storing one map.

No network and no database: a fake client returns the saved beatmap.json (map 22423), a
fake file store returns the saved 22423.osu, and a fake session records each statement
compiled to Postgres SQL. The same code against Postgres is in
tests/integration/test_beatmaps_live.py.
"""

from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach.beatmaps import (
    BeatmapMismatchError,
    FetchResult,
    UnsupportedModeError,
    fetch_beatmap,
    store_hit_objects,
)
from osu_coach.osu.client import OsuApiError, OsuClient, OsuNotFoundError
from osu_coach.osu.files import BeatmapFileError, BeatmapFiles
from osu_coach.osu.schemas import OsuBeatmap
from osu_coach.osufile import OsuFileError, parse_osu
from osu_coach.patterns import object_features
from tests.support import FIXTURES, load

PG = postgresql.dialect()  # type: ignore[no-untyped-call]  # SQLAlchemy leaves it untyped
CHECKSUM = load("beatmap.json")["checksum"]


def beatmap(**changes: Any) -> OsuBeatmap:
    return OsuBeatmap.model_validate({**load("beatmap.json"), **changes})


class FakeOsu:
    def __init__(self, result: OsuBeatmap | Exception | None = None) -> None:
        self.result = beatmap() if result is None else result
        self.calls: list[int] = []

    async def get_beatmap(self, beatmap_id: int) -> OsuBeatmap:
        self.calls.append(beatmap_id)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeFiles:
    def __init__(self, path: Path | Exception = FIXTURES / "22423.osu") -> None:
        self.path = path
        self.calls: list[tuple[int, str | None]] = []

    async def get(self, beatmap_id: int, checksum: str | None) -> Path:
        self.calls.append((beatmap_id, checksum))
        if isinstance(self.path, Exception):
            raise self.path
        return self.path


class FakeSession:
    """Records statements (as Postgres SQL, with their row count) and commits."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.statements: list[tuple[Any, Any]] = []

    async def execute(self, statement: Any, params: Any = None) -> None:
        self.statements.append((statement, params))
        sql = str(statement.compile(dialect=PG)).split()
        self.events.append(" ".join(sql[:3]))  # e.g. "INSERT INTO beatmaps"

    async def commit(self) -> None:
        self.events.append("commit")


async def run(
    osu: FakeOsu | None = None, files: FakeFiles | None = None, session: FakeSession | None = None
) -> tuple[FetchResult, FakeOsu, FakeFiles, FakeSession]:
    osu, files, session = osu or FakeOsu(), files or FakeFiles(), session or FakeSession()
    result = await fetch_beatmap(
        cast(OsuClient, osu), cast(BeatmapFiles, files), cast(AsyncSession, session), 22423
    )
    return result, osu, files, session


# ---------- Success ----------


async def test_fetches_checks_and_stores_one_map() -> None:
    result, osu, files, _ = await run()

    assert result == FetchResult(beatmap_id=22423, version="Hard", objects=254)
    assert osu.calls == [22423]
    # The file is asked for with osu!'s checksum, so it's verified (or downloaded again).
    assert files.calls == [(22423, CHECKSUM)]


async def test_writes_map_then_objects_then_commits_once() -> None:
    _, _, _, session = await run()

    # The map row first (the objects reference it), then its objects are replaced.
    assert session.events == [
        "INSERT INTO beatmaps",
        "DELETE FROM hit_objects",
        "INSERT INTO hit_objects",
        "commit",
    ]


async def test_the_map_row_has_its_max_combo() -> None:
    _, _, _, session = await run()

    upsert = session.statements[0][0].compile(dialect=PG)
    # A full fetch knows the max combo (a score's map data doesn't).
    assert upsert.params["max_combo_m0"] == 450


async def test_every_object_is_stored_with_its_features() -> None:
    _, _, _, session = await run()

    rows = session.statements[2][1]
    want = object_features(parse_osu((FIXTURES / "22423.osu").read_text(encoding="utf-8")))
    assert len(rows) == 254
    assert rows[0] == {"beatmap_id": 22423, **vars(want[0])}
    assert [r["idx"] for r in rows] == list(range(254))


# ---------- Refusals: nothing is written ----------


@pytest.mark.parametrize("mode", ["taiko", "fruits", "mania"])
async def test_other_game_modes_are_refused(mode: str) -> None:
    files, session = FakeFiles(), FakeSession()

    with pytest.raises(UnsupportedModeError, match=mode):
        await run(FakeOsu(beatmap(mode=mode)), files, session)

    assert files.calls == [], "no download for a map that won't be used"
    assert session.events == []


@pytest.mark.parametrize(
    "changes",
    [
        {"count_circles": 166},  # one circle more in the file than osu! says
        {"count_sliders": 85},
        {"count_spinners": 0},
    ],
)
async def test_file_and_details_must_agree(changes: dict[str, int]) -> None:
    session = FakeSession()

    with pytest.raises(BeatmapMismatchError, match="22423"):
        await run(FakeOsu(beatmap(**changes)), session=session)

    assert session.events == []


@pytest.mark.parametrize(
    "error",
    [
        OsuNotFoundError("/beatmaps/22423"),
        OsuApiError("osu! API returned 503"),
    ],
)
async def test_osu_errors_write_nothing(error: Exception) -> None:
    files, session = FakeFiles(), FakeSession()

    with pytest.raises(type(error)):
        await run(FakeOsu(error), files, session)

    assert files.calls == []
    assert session.events == []


async def test_a_bad_download_writes_nothing() -> None:
    session = FakeSession()

    with pytest.raises(BeatmapFileError):
        await run(files=FakeFiles(BeatmapFileError("MD5 mismatch")), session=session)

    assert session.events == []


async def test_a_broken_file_writes_nothing(tmp_path: Path) -> None:
    broken = tmp_path / "22423.osu"
    broken.write_text("<html>maintenance</html>", encoding="utf-8")
    session = FakeSession()

    with pytest.raises(OsuFileError):
        await run(files=FakeFiles(broken), session=session)

    assert session.events == []


# ---------- store_hit_objects ----------


async def test_store_replaces_a_maps_objects() -> None:
    session = FakeSession()
    features = object_features(parse_osu((FIXTURES / "75.osu").read_text(encoding="utf-8")))

    await store_hit_objects(cast(AsyncSession, session), 75, features)

    delete_sql = str(session.statements[0][0].compile(dialect=PG))
    assert "DELETE FROM hit_objects WHERE hit_objects.beatmap_id" in delete_sql
    assert session.statements[0][0].compile(dialect=PG).params == {"beatmap_id_1": 75}
    assert len(session.statements[1][1]) == 194
    assert all(row["beatmap_id"] == 75 for row in session.statements[1][1])


async def test_store_with_no_objects_only_deletes() -> None:
    session = FakeSession()

    await store_hit_objects(cast(AsyncSession, session), 75, [])

    assert session.events == ["DELETE FROM hit_objects"]
