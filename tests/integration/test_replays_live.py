"""Integration tests for osu_coach.replays against a real Postgres.

Run with:  uv run pytest -m integration
Needs:     Postgres (docker compose up -d postgres); the last test also needs .osr files in
           data/replays/. Each skips with the reason if what it needs is missing.
Uses a separate `<db>_test` database (see conftest.py) and pytest's temporary folder for
the files. Replays are built by tests/osrdata.py.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from osu_coach.osrfile import ReplayFileError, parse_replay
from osu_coach.replays import StoredReplay, load_frames, sha256_hex, store_replay
from tests.osrdata import CHECKSUM, PLAYED_AT, lazer_osr, osr
from tests.support import ROOT

pytestmark = pytest.mark.integration

REPLAYS = sorted((ROOT / "data" / "replays").glob("*.osr"))


@pytest.fixture
async def db(test_url: URL) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(test_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture(autouse=True)
def empty_tables(engine: Engine) -> Iterator[None]:
    yield
    with engine.begin() as conn:
        # Fail instead of waiting forever if a broken test left a session holding locks.
        conn.execute(text("SET LOCAL lock_timeout = '10s'"))
        conn.execute(text("TRUNCATE replays, beatmaps RESTART IDENTITY CASCADE"))


async def store(db: AsyncEngine, data_dir: Path, data: bytes) -> StoredReplay:
    async with async_sessionmaker(db)() as session:
        return await store_replay(session, data_dir, data)


def rows(engine: Engine, sql: str) -> list[tuple[object, ...]]:
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(sql))]


# ---------- Storing ----------


async def test_a_replay_is_stored(db: AsyncEngine, engine: Engine, tmp_path: Path) -> None:
    data = lazer_osr()
    before = datetime.now(UTC)

    assert await store(db, tmp_path, data) == StoredReplay(1, created=True)

    [row] = rows(
        engine,
        "SELECT file_sha256, beatmap_checksum, score_id, player_name, mods, lazer, "
        "count_300, count_miss, perfect, played_at, frame_count, frames_path, parsed_at "
        "FROM replays",
    )
    digest = sha256_hex(data)
    frames = parse_replay(data).frames
    *stored, parsed_at = row
    assert stored == [
        digest,
        CHECKSUM,
        6604281970,
        "tester",
        [],
        True,
        100,
        3,
        False,
        PLAYED_AT,  # with its time zone, exact to the microsecond
        len(frames),
        f"frames/{digest}.parquet",
    ]
    assert isinstance(parsed_at, datetime)
    assert before - timedelta(seconds=5) <= parsed_at <= datetime.now(UTC) + timedelta(seconds=5)
    assert load_frames(tmp_path, f"frames/{digest}.parquet").height == len(frames)


async def test_mods_are_stored_as_a_list(db: AsyncEngine, engine: Engine, tmp_path: Path) -> None:
    mods = [{"acronym": "HD"}, {"acronym": "DT"}, {"acronym": "CL"}]
    await store(db, tmp_path, lazer_osr({"mods": mods}))

    assert rows(engine, "SELECT mods FROM replays") == [(["HD", "DT", "CL"],)]


async def test_the_same_file_twice_is_one_row(
    db: AsyncEngine, engine: Engine, tmp_path: Path
) -> None:
    first = await store(db, tmp_path, osr())
    second = await store(db, tmp_path, osr())

    assert first == StoredReplay(1, created=True)
    assert second == StoredReplay(1, created=False)
    assert rows(engine, "SELECT count(*) FROM replays") == [(1,)]


async def test_different_files_are_different_rows(
    db: AsyncEngine, engine: Engine, tmp_path: Path
) -> None:
    # A changed score makes a different file, even with the same map and player.
    a = await store(db, tmp_path, osr(score=1))
    b = await store(db, tmp_path, osr(score=2))

    assert (a.id, b.id) == (1, 2)
    assert rows(engine, "SELECT score FROM replays ORDER BY id") == [(1,), (2,)]


async def test_uploads_at_the_same_moment_keep_one_row(
    db: AsyncEngine, engine: Engine, tmp_path: Path
) -> None:
    # Five sessions store the same file at once: all get the same id, one created it.
    # A TaskGroup, not gather: if one fails, the others are cancelled and their sessions
    # closed, instead of being left holding locks.
    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(store(db, tmp_path, osr())) for _ in range(5)]
    results = [t.result() for t in tasks]

    assert len({r.id for r in results}) == 1
    assert sum(r.created for r in results) == 1
    assert rows(engine, "SELECT count(*) FROM replays") == [(1,)]


async def test_a_bad_file_stores_no_row(db: AsyncEngine, engine: Engine, tmp_path: Path) -> None:
    with pytest.raises(ReplayFileError):
        await store(db, tmp_path, osr(mode=3))

    assert rows(engine, "SELECT count(*) FROM replays") == [(0,)]


def test_the_file_hash_is_unique(engine: Engine) -> None:
    # The database itself refuses a second row for one file, whatever the code does.
    insert = text(
        "INSERT INTO replays (file_sha256, beatmap_checksum, player_name, mods, lazer, "
        "game_version, count_300, count_100, count_50, count_geki, count_katu, count_miss, "
        "score, max_combo, perfect, played_at, frame_count, frames_path) "
        "VALUES ('h', 'c', 'p', '{}', false, 1, 0, 0, 0, 0, 0, 0, 0, 0, false, now(), 1, 'f')"
    )
    with engine.begin() as conn:
        conn.execute(insert)
    with pytest.raises(IntegrityError, match="replays_file_sha256_key"), engine.begin() as conn:
        conn.execute(insert)


# ---------- Finding the map ----------


def add_map(engine: Engine, beatmap_id: int, checksum: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO beatmaps (id, beatmapset_id, artist, title, creator, version, "
                "star_rating, ar, od, cs, hp, bpm, length_s, hit_length_s, count_circles, "
                "count_sliders, count_spinners, checksum, status, mode) VALUES (:id, 1, 'a', "
                "'t', 'c', 'v', 5, 9, 8, 4, 5, 180, 90, 80, 1, 0, 0, :checksum, 'ranked', 'osu')"
            ),
            {"id": beatmap_id, "checksum": checksum},
        )


MAP_OF_REPLAY = "SELECT b.id FROM replays r JOIN beatmaps b ON b.checksum = r.beatmap_checksum"


async def test_a_replay_finds_its_map_by_checksum(
    db: AsyncEngine, engine: Engine, tmp_path: Path
) -> None:
    # Stored before the map is: no foreign key stops it, and the join finds the map later.
    await store(db, tmp_path, osr())
    assert rows(engine, MAP_OF_REPLAY) == []

    add_map(engine, 4767844, CHECKSUM)
    assert rows(engine, MAP_OF_REPLAY) == [(4767844,)]


async def test_an_updated_map_isnt_the_played_one(
    db: AsyncEngine, engine: Engine, tmp_path: Path
) -> None:
    # The map was changed after the play: its file (and checksum) differ, so the replay
    # must not be compared with the new version's hit objects.
    await store(db, tmp_path, osr())
    add_map(engine, 4767844, "0" * 32)

    assert rows(engine, MAP_OF_REPLAY) == []


# ---------- Real replays ----------


async def test_real_replays_are_stored(db: AsyncEngine, engine: Engine, tmp_path: Path) -> None:
    if not REPLAYS:
        pytest.skip("no .osr files in data/replays/")

    for path in REPLAYS:
        data = await asyncio.to_thread(path.read_bytes)
        result = await store(db, tmp_path, data)
        assert result.created, path.name

        [(frame_count, frames_path)] = rows(
            engine, f"SELECT frame_count, frames_path FROM replays WHERE id = {result.id}"
        )
        frames = load_frames(tmp_path, str(frames_path))
        assert frames.height == frame_count == len(parse_replay(data).frames)
        assert frames["time_ms"].is_sorted()

    assert rows(engine, "SELECT count(*) FROM replays") == [(len(REPLAYS),)]
