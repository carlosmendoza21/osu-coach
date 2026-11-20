"""Integration tests for osu_coach.vectors against a real Postgres with pgvector: storing
a map's vector, finding maps without one, building them from stored objects, and the
nearest-map search the recommender will use.

Run with:  uv run pytest -m integration
Needs:     Postgres with pgvector (docker compose up -d postgres). Skips with the reason
           if it's missing. Uses a separate `<db>_test` database (see conftest.py).
"""

from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from osu_coach.vectors import (
    DIMENSIONS,
    VECTOR_VERSION,
    PatternVector,
    build_missing_vectors,
    build_vector,
    maps_without_vectors,
    pattern_vector,
    store_vector,
)
from tests.support import ALL_TABLES

pytestmark = pytest.mark.integration


@pytest.fixture
async def db(test_url: URL) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(test_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


# Maps 1-3 have objects (in this order: written out of order to check the sorting);
# map 4 has none.
OBJECTS = {
    1: [(0, "other", None), (1, "jump", 200.0), (2, "jump", 300.0), (3, "stream", 30.0)],
    2: [(0, "other", None), (1, "stream", 20.0), (2, "stream", 20.0), (3, "stream", 20.0)],
    3: [(0, "other", None), (1, "slider", 100.0)],
}


@pytest.fixture(autouse=True)
def rows(engine: Engine) -> Iterator[None]:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO beatmaps (id, beatmapset_id, artist, title, creator, version, "
                "star_rating, ar, od, cs, hp, bpm, length_s, hit_length_s, count_circles, "
                "count_sliders, count_spinners, status, mode) VALUES (:id, 1, 'a', 't', 'c', "
                "'v', :stars, 9, 8, 4, 5, :bpm, 90, 80, 1, 0, 0, 'ranked', 'osu')"
            ),
            [
                {"id": 1, "stars": 6.0, "bpm": 180},
                {"id": 2, "stars": 5.0, "bpm": 200},
                {"id": 3, "stars": 3.0, "bpm": 120},
                {"id": 4, "stars": 4.0, "bpm": 150},
            ],
        )
        conn.execute(
            text(
                "INSERT INTO hit_objects (beatmap_id, idx, kind, time_ms, end_time_ms, x, y, "
                "spacing, angle, time_since_prev_ms, pattern) "
                "VALUES (:map, :idx, 'circle', :t, :t, 0, 0, :spacing, NULL, NULL, :pattern)"
            ),
            [
                {"map": m, "idx": i, "t": i * 100, "spacing": s, "pattern": p}
                for m, objs in OBJECTS.items()
                for i, p, s in reversed(objs)
            ],
        )
    yield
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {ALL_TABLES}"))


def stored(engine: Engine) -> dict[int, tuple[list[float], int]]:
    """Each stored vector, by map: (its numbers, its version)."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT beatmap_id, vector::text, version FROM map_vectors ORDER BY beatmap_id")
        )
        return {m: ([float(x) for x in v.strip("[]").split(",")], ver) for m, v, ver in rows}


def a_vector(first: float = 0.5) -> PatternVector:
    return PatternVector((first,) + (0.1,) * (DIMENSIONS - 1))


async def store(db: AsyncEngine, beatmap_id: int, vector: PatternVector) -> None:
    async with async_sessionmaker(db)() as session:
        await store_vector(session, beatmap_id, vector)
        await session.commit()


# ---------- Storing ----------


async def test_a_vector_is_stored_with_its_version(db: AsyncEngine, engine: Engine) -> None:
    await store(db, 1, a_vector())

    values, version = stored(engine)[1]
    assert values == pytest.approx(list(a_vector().values))
    assert version == VECTOR_VERSION


async def test_storing_again_replaces_it(db: AsyncEngine, engine: Engine) -> None:
    await store(db, 1, a_vector(0.5))
    with engine.connect() as conn:
        first = conn.execute(text("SELECT built_at FROM map_vectors")).scalar_one()

    await store(db, 1, a_vector(0.9))

    assert list(stored(engine)) == [1]  # still one row
    assert stored(engine)[1][0][0] == pytest.approx(0.9)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT built_at FROM map_vectors")).scalar_one() > first


async def test_a_vector_needs_its_map(db: AsyncEngine) -> None:
    with pytest.raises(IntegrityError):
        await store(db, 999, a_vector())


async def test_deleting_a_map_deletes_its_vector(db: AsyncEngine, engine: Engine) -> None:
    await store(db, 1, a_vector())
    await store(db, 2, a_vector())

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM beatmaps WHERE id = 1"))

    assert list(stored(engine)) == [2]


async def test_the_column_holds_exactly_the_vectors_size(engine: Engine) -> None:
    # pgvector checks the dimensions: a vector one number short is refused.
    with pytest.raises(Exception, match="expected 11 dimensions"), engine.begin() as conn:
        conn.execute(
            text("INSERT INTO map_vectors (beatmap_id, vector, version) VALUES (1, :v, 1)"),
            {"v": str([0.1] * (DIMENSIONS - 1))},
        )


# ---------- Which maps need one ----------


async def missing(db: AsyncEngine) -> list[int]:
    async with async_sessionmaker(db)() as session:
        return await maps_without_vectors(session)


async def test_maps_with_objects_need_a_vector(db: AsyncEngine) -> None:
    # Map 4 has no objects yet (not fetched), so there's nothing to build from.
    assert await missing(db) == [1, 2, 3]


async def test_a_current_vector_isnt_built_again(db: AsyncEngine) -> None:
    await store(db, 2, a_vector())

    assert await missing(db) == [1, 3]


async def test_an_outdated_vector_is_built_again(db: AsyncEngine, engine: Engine) -> None:
    await store(db, 1, a_vector())
    await store(db, 2, a_vector())
    with engine.begin() as conn:
        conn.execute(text("UPDATE map_vectors SET version = :v WHERE beatmap_id = 1"),
                     {"v": VECTOR_VERSION - 1})  # fmt: skip

    assert await missing(db) == [1, 3]


# ---------- Building from stored objects ----------


async def test_a_vector_is_built_from_the_maps_objects(db: AsyncEngine, engine: Engine) -> None:
    async with async_sessionmaker(db)() as session:
        vector = await build_vector(session, 1)
        await session.commit()

    # The same as from the objects directly, in map order, with the map's CS, BPM, stars.
    class Obj:
        def __init__(self, pattern: str, spacing: float | None) -> None:
            self.pattern, self.spacing = pattern, spacing

    expected = pattern_vector([Obj(p, s) for _, p, s in OBJECTS[1]], cs=4, bpm=180, star_rating=6)
    assert vector == expected
    assert (vector["jump"], vector["stream"], vector["bpm"]) == (0.5, 0.25, 0.6)
    assert stored(engine)[1][0] == pytest.approx(list(expected.values))


async def test_objects_are_read_in_map_order(db: AsyncEngine, engine: Engine) -> None:
    # Map 4: two 16-note streams split by a jump at idx 16, so both are long (1.0). Its
    # rows are written odd idx first, then even: read in that order, the streams would
    # run 24 notes, jump, 8 notes, and only 24 of 32 would be in a long run (0.75).
    patterns = ["stream"] * 16 + ["jump"] + ["stream"] * 16
    order = [*range(1, 33, 2), *range(0, 33, 2)]
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO hit_objects (beatmap_id, idx, kind, time_ms, end_time_ms, x, y, "
                "spacing, angle, time_since_prev_ms, pattern) "
                "VALUES (4, :idx, 'circle', :t, :t, 0, 0, 20, NULL, NULL, :pattern)"
            ),
            [{"idx": i, "t": i * 100, "pattern": patterns[i]} for i in order],
        )

    async with async_sessionmaker(db)() as session:
        vector = await build_vector(session, 4)

    assert vector["long_stream"] == 1.0


async def test_building_every_missing_vector(db: AsyncEngine, engine: Engine) -> None:
    await store(db, 2, a_vector())  # already current: left as it is

    async with async_sessionmaker(db)() as session:
        built = await build_missing_vectors(session)

    assert built == 2
    vectors = stored(engine)  # committed: read on another connection
    assert list(vectors) == [1, 2, 3]
    assert vectors[2][0] == pytest.approx(list(a_vector().values))
    assert vectors[3][0][2] == pytest.approx(0.5)  # map 3: one slider of two objects


async def test_building_again_builds_nothing(db: AsyncEngine) -> None:
    async with async_sessionmaker(db)() as session:
        assert await build_missing_vectors(session) == 3
        assert await build_missing_vectors(session) == 0


# ---------- The nearest maps ----------


async def test_nearest_maps_by_pgvector_distance(db: AsyncEngine, engine: Engine) -> None:
    async with async_sessionmaker(db)() as session:
        await build_missing_vectors(session)

    # The recommender's search: maps ordered by distance to a target vector. A target
    # all streams finds the stream map first, the jump map next, the slider map last.
    target = [0.0, 1.0] + [0.0] * (DIMENSIONS - 2)
    with engine.connect() as conn:
        order = conn.execute(
            text("SELECT beatmap_id FROM map_vectors ORDER BY vector <-> CAST(:t AS vector)"),
            {"t": str(target)},
        ).scalars()
        assert list(order) == [2, 1, 3]
