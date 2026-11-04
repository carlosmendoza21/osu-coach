"""Unit tests for osu_coach.replays.

The files are real (pytest's temporary folder) and the replays are built by
tests/osrdata.py; a fake session answers the database queries and records them,
compiled to Postgres SQL. The same code against Postgres is in
tests/integration/test_replays_live.py.
"""

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import polars as pl
import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach import replays
from osu_coach.osrfile import Frame, ReplayFileError, parse_replay
from osu_coach.osu.client import OsuApiError, OsuClient, OsuNotFoundError
from osu_coach.osu.files import BeatmapFiles
from osu_coach.replays import (
    FRAME_SCHEMA,
    MapChangedError,
    ReplayMap,
    StoredReplay,
    fetch_replay_map,
    frames_frame,
    load_frames,
    sha256_hex,
    store_replay,
    stored_map,
)
from tests.osrdata import CHECKSUM, PLAYED_AT, lazer_osr, osr

PG = postgresql.dialect()  # type: ignore[no-untyped-call]  # SQLAlchemy leaves it untyped


class FakeSession:
    """Answers each `scalar()` from a list, in order; records statements and commits."""

    def __init__(self, *answers: int | None) -> None:
        self.answers = list(answers)
        self.events: list[str] = []
        self.statements: list[Any] = []

    async def scalar(self, statement: Any) -> int | None:
        self.statements.append(statement)
        sql = str(statement.compile(dialect=PG)).split()
        self.events.append("INSERT" if sql[0] == "INSERT" else "SELECT")
        return self.answers.pop(0)

    async def commit(self) -> None:
        self.events.append("commit")


async def store(session: FakeSession, data_dir: Path, data: bytes) -> StoredReplay:
    return await store_replay(cast(AsyncSession, session), data_dir, data)


def files_in(folder: Path) -> list[str]:
    return sorted(str(p.relative_to(folder)).replace("\\", "/") for p in folder.rglob("*.*"))


# ---------- store_replay ----------


async def test_a_new_replay_is_stored(tmp_path: Path) -> None:
    data = osr()
    digest = sha256_hex(data)
    session = FakeSession(None, 7)  # not stored before; the insert returns id 7

    assert await store(session, tmp_path, data) == StoredReplay(7, created=True)
    assert session.events == ["SELECT", "INSERT", "commit"]
    # The original file and the frames, named by the file's hash; no temporary files left.
    assert files_in(tmp_path) == [f"frames/{digest}.parquet", f"uploads/{digest}.osr"]
    assert (tmp_path / "uploads" / f"{digest}.osr").read_bytes() == data


async def test_the_stored_frames_are_the_parsed_ones(tmp_path: Path) -> None:
    data = osr()
    await store(FakeSession(None, 7), tmp_path, data)

    frames = load_frames(tmp_path, f"frames/{sha256_hex(data)}.parquet")
    assert [Frame(*row) for row in frames.iter_rows()] == list(parse_replay(data).frames)


@pytest.mark.parametrize("data", [osr(), lazer_osr()], ids=["stable", "lazer"])
async def test_the_row_holds_the_replay(tmp_path: Path, data: bytes) -> None:
    session = FakeSession(None, 7)
    await store(session, tmp_path, data)
    replay = parse_replay(data)

    insert = session.statements[1].compile(dialect=PG)
    assert insert.params == {
        "file_sha256": sha256_hex(data),
        "beatmap_checksum": CHECKSUM,
        "score_id": replay.score_id,
        "player_name": "tester",
        "mods": list(replay.mods),
        "lazer": replay.lazer,
        "game_version": replay.game_version,
        "count_300": 100,
        "count_100": 5,
        "count_50": 1,
        "count_geki": 10,
        "count_katu": 2,
        "count_miss": 3,
        "score": 123456,
        "max_combo": 150,
        "perfect": False,
        "played_at": PLAYED_AT,
        "frame_count": len(replay.frames),
        "frames_path": f"frames/{sha256_hex(data)}.parquet",  # relative to the data folder
    }
    # Two uploads of one file at the same moment keep one row.
    sql = str(insert)
    assert "ON CONFLICT (file_sha256) DO NOTHING" in sql
    assert sql.endswith("RETURNING replays.id")


async def test_the_lookup_is_by_the_files_hash(tmp_path: Path) -> None:
    data = osr()
    session = FakeSession(None, 7)
    await store(session, tmp_path, data)

    lookup = session.statements[0].compile(dialect=PG)
    assert "WHERE replays.file_sha256 = " in str(lookup)
    assert list(lookup.params.values()) == [sha256_hex(data)]


async def test_a_known_file_is_not_parsed_or_written_again(tmp_path: Path) -> None:
    # Not even a valid replay: a file seen before isn't parsed at all, just looked up.
    session = FakeSession(5)

    assert await store(session, tmp_path, b"not a replay") == StoredReplay(5, created=False)
    assert session.events == ["SELECT"]
    assert files_in(tmp_path) == []


@pytest.mark.parametrize(
    "data",
    [b"", b"not a replay", osr(mode=1), osr(frames=[], seed=None)],
    ids=["empty", "garbage", "taiko", "no frames"],
)
async def test_a_bad_file_stores_nothing(tmp_path: Path, data: bytes) -> None:
    session = FakeSession(None)

    with pytest.raises(ReplayFileError):
        await store(session, tmp_path, data)
    assert session.events == ["SELECT"]  # no insert, no commit
    assert files_in(tmp_path) == []


async def test_a_lost_race_returns_the_other_uploads_id(tmp_path: Path) -> None:
    # Not stored when first checked, but the same file was stored before our insert: the
    # insert returns no id, and the other upload's row is looked up.
    session = FakeSession(None, None, 9)

    assert await store(session, tmp_path, osr()) == StoredReplay(9, created=False)
    assert session.events == ["SELECT", "INSERT", "commit", "SELECT"]


async def test_files_are_rewritten_the_same(tmp_path: Path) -> None:
    # A retry after a failed database write writes the same files again: same names, same
    # bytes, nothing extra.
    data = osr()
    await store(FakeSession(None, 7), tmp_path, data)
    before = {name: (tmp_path / name).read_bytes() for name in files_in(tmp_path)}
    await store(FakeSession(None, 7), tmp_path, data)

    assert {name: (tmp_path / name).read_bytes() for name in files_in(tmp_path)} == before


# ---------- The frames table ----------


@pytest.mark.parametrize(
    "frames",
    [
        (),  # no frames: an empty table that still has the columns
        (Frame(-949, 170.44168, 189.2321, 0),),  # before the song starts
        (Frame(10, 1.5, 2.25, 5), Frame(26, -20.0, 400.0, 10), Frame(26, 600.0, 0.0, 16)),
        (Frame(2**40, 1e300, -1e300, 2**40),),  # a hand-made file can't overflow a column
    ],
    ids=["empty", "negative time", "several", "huge"],
)
def test_frames_frame(frames: tuple[Frame, ...]) -> None:
    table = frames_frame(frames)

    assert table.schema == pl.Schema(FRAME_SCHEMA)
    assert [Frame(*row) for row in table.iter_rows()] == list(frames)  # same order, exact


def test_frames_round_trip_through_parquet(tmp_path: Path) -> None:
    frames = parse_replay(lazer_osr()).frames
    frames_frame(frames).write_parquet(tmp_path / "f.parquet")

    table = load_frames(tmp_path, "f.parquet")  # a path relative to the data folder
    assert table.schema == pl.Schema(FRAME_SCHEMA)
    assert [Frame(*row) for row in table.iter_rows()] == list(frames)


def test_sha256_hex() -> None:
    empty = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert sha256_hex(b"") == empty
    assert sha256_hex(b"abc") != sha256_hex(b"abd")


# ---------- Writing files ----------


def test_a_write_replaces_the_old_file(tmp_path: Path) -> None:
    path = tmp_path / "new" / "folder" / "f.bin"  # missing folders are created

    replays._write_atomic(path, lambda p: p.write_bytes(b"old"))
    replays._write_atomic(path, lambda p: p.write_bytes(b"new"))

    assert path.read_bytes() == b"new"
    assert files_in(tmp_path) == ["new/folder/f.bin"]


def test_a_failed_write_keeps_the_old_file(tmp_path: Path) -> None:
    path = tmp_path / "f.bin"
    path.write_bytes(b"old")

    def half_then_fail(p: Path) -> None:
        p.write_bytes(b"ne")
        raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        replays._write_atomic(path, half_then_fail)
    assert path.read_bytes() == b"old"  # a reader never sees the half-written file
    assert files_in(tmp_path) == ["f.bin"]  # and the temporary file is gone


# ---------- The replay's map ----------


class FakeOsu:
    """lookup_beatmap only: answers with a map id, or raises."""

    def __init__(self, result: int | Exception = 4767844) -> None:
        self.result = result
        self.lookups: list[str] = []

    async def lookup_beatmap(self, checksum: str) -> Any:
        self.lookups.append(checksum)
        if isinstance(self.result, Exception):
            raise self.result
        return SimpleNamespace(id=self.result)


class FakeFetch:
    """Stands in for beatmaps.fetch_beatmap, recording which map it was asked for."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    async def __call__(self, osu: Any, files: Any, session: Any, beatmap_id: int) -> None:
        self.calls.append(beatmap_id)


@pytest.fixture
def fetch(monkeypatch: pytest.MonkeyPatch) -> FakeFetch:
    fake = FakeFetch()
    monkeypatch.setattr(replays, "fetch_beatmap", fake)
    return fake


async def fetch_map(session: FakeSession, osu: FakeOsu, checksum: str = CHECKSUM) -> ReplayMap:
    return await fetch_replay_map(
        cast(OsuClient, osu), cast(BeatmapFiles, None), cast(AsyncSession, session), checksum
    )


async def test_a_stored_map_isnt_fetched_again(fetch: FakeFetch) -> None:
    osu = FakeOsu()

    assert await fetch_map(FakeSession(22423), osu) == ReplayMap(22423, fetched=False)
    assert osu.lookups == [] and fetch.calls == []  # nothing asked of osu!


async def test_a_missing_map_is_looked_up_and_fetched(fetch: FakeFetch) -> None:
    # Not stored; after the fetch, stored with the replay's exact file.
    osu, session = FakeOsu(4767844), FakeSession(None, 4767844)

    assert await fetch_map(session, osu) == ReplayMap(4767844, fetched=True)
    assert osu.lookups == [CHECKSUM]
    assert fetch.calls == [4767844]
    assert session.events == ["SELECT", "SELECT"]  # checked before and after


async def test_a_changed_map_is_an_error(fetch: FakeFetch) -> None:
    # osu! found the map by the old checksum, but its current file is a newer version:
    # still not stored with the replay's checksum after fetching.
    with pytest.raises(MapChangedError, match="map 4767844 has changed"):
        await fetch_map(FakeSession(None, None), FakeOsu(4767844))
    assert fetch.calls == [4767844]


@pytest.mark.parametrize(
    "error",
    [OsuNotFoundError("unknown map"), OsuApiError("osu! is down")],
    ids=["not found", "osu! down"],
)
async def test_osu_errors_pass_through(fetch: FakeFetch, error: Exception) -> None:
    with pytest.raises(type(error)):
        await fetch_map(FakeSession(None), FakeOsu(error))
    assert fetch.calls == []  # nothing fetched for a map osu! didn't give


async def test_stored_map_needs_the_exact_file_and_its_objects() -> None:
    session = FakeSession(None)
    await stored_map(cast(AsyncSession, session), CHECKSUM.upper())

    query = session.statements[0].compile(dialect=PG)
    sql = " ".join(str(query).split())
    assert "beatmaps.checksum = " in sql
    assert "EXISTS (SELECT * FROM hit_objects WHERE hit_objects.beatmap_id = beatmaps.id)" in sql
    assert list(query.params.values()) == [CHECKSUM]  # lowercased, like osu! writes it


@pytest.mark.parametrize("other_writer_finished", [True, False], ids=["finished", "nothing"])
def test_a_refused_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, other_writer_finished: bool
) -> None:
    # Windows refuses a rename onto a file another writer is renaming at the same moment.
    path = tmp_path / "f.bin"

    def refused(src: Path, dst: Path) -> None:
        if other_writer_finished:
            dst.write_bytes(b"same")  # the other writer's file, with the same content
        raise PermissionError("Access is denied")

    monkeypatch.setattr(os, "replace", refused)  # what _write_atomic calls

    if other_writer_finished:
        replays._write_atomic(path, lambda p: p.write_bytes(b"same"))  # fine: it's there
        assert path.read_bytes() == b"same"
    else:
        with pytest.raises(PermissionError):  # a real permission problem still shows
            replays._write_atomic(path, lambda p: p.write_bytes(b"same"))
    assert not list(tmp_path.glob("*.tmp"))  # the temporary file is gone either way
