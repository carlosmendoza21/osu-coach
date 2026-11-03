"""Integration tests for osu_coach.beatmaps against a real Postgres.

Run with:  uv run pytest -m integration
Needs:     Postgres (docker compose up -d postgres). The last test also needs Redis and
           osu! credentials. Each skips with the reason if what it needs is missing.
Uses a separate `<db>_test` database (see conftest.py). Most tests use the fake client and
file store from tests/unit/test_beatmaps.py, so only the database is real.
"""

from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import Engine, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from osu_coach import beatmaps
from osu_coach.beatmaps import BeatmapMismatchError, FetchResult, fetch_beatmap, store_hit_objects
from osu_coach.config import get_settings
from osu_coach.osu.auth import TokenProvider
from osu_coach.osu.client import OsuClient
from osu_coach.osu.files import BeatmapFiles
from osu_coach.osu.ratelimiter import RateLimiter
from osu_coach.osufile import parse_osu
from osu_coach.patterns import object_features
from tests.support import FIXTURES
from tests.unit.test_beatmaps import FakeFiles, FakeOsu, beatmap

pytestmark = pytest.mark.integration


@pytest.fixture
async def db(test_url: URL) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(test_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture(autouse=True)
def empty_tables(engine: Engine) -> Iterator[None]:
    yield
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE players, scores, beatmaps, hit_objects"))


async def fetch(db: AsyncEngine, osu: Any = None, files: Any = None) -> FetchResult:
    async with async_sessionmaker(db)() as session:
        return await fetch_beatmap(
            cast(OsuClient, osu or FakeOsu()),
            cast(BeatmapFiles, files or FakeFiles()),
            session,
            22423,
        )


def query(engine: Engine, sql: str) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(text(sql)).all())


def counts(engine: Engine) -> tuple[int, int]:
    (row,) = query(
        engine, "SELECT (SELECT count(*) FROM beatmaps), (SELECT count(*) FROM hit_objects)"
    )
    return int(row[0]), int(row[1])


# ---------- Storing ----------


async def test_map_and_objects_are_stored(db: AsyncEngine, engine: Engine) -> None:
    result = await fetch(db)

    assert result.objects == 254
    assert counts(engine) == (1, 254)
    (row,) = query(engine, "SELECT version, max_combo, checksum, od FROM beatmaps")
    assert (row.version, row.max_combo, row.od) == ("Hard", 450, 7.0)
    patterns = dict(query(engine, "SELECT pattern, count(*) FROM hit_objects GROUP BY 1"))
    assert patterns == {"slider": 84, "spinner": 3, "stream": 18, "other": 149}


async def test_fetching_again_gives_the_same_rows(db: AsyncEngine, engine: Engine) -> None:
    await fetch(db)
    first = query(engine, "SELECT * FROM hit_objects ORDER BY idx")

    await fetch(db)

    assert query(engine, "SELECT * FROM hit_objects ORDER BY idx") == first
    assert counts(engine) == (1, 254)


async def test_old_objects_dont_survive_a_changed_map(db: AsyncEngine, engine: Engine) -> None:
    # Stored earlier from an older version of the map: more objects than it has now.
    await fetch(db)
    old = object_features(parse_osu((FIXTURES / "75.osu").read_text(encoding="utf-8")))
    async with async_sessionmaker(db)() as session:
        # 388 objects, numbered 0-387 like a real map's.
        bigger = [replace(f, idx=i) for i, f in enumerate(old * 2)]
        await store_hit_objects(session, 22423, bigger)
        await session.commit()
    assert counts(engine) == (1, 388)

    await fetch(db)

    assert counts(engine) == (1, 254), "the extra objects from the old version are gone"


async def test_other_maps_objects_are_left_alone(db: AsyncEngine, engine: Engine) -> None:
    await fetch(db)
    other = beatmap(id=75, checksum=None, count_circles=160, count_sliders=30, count_spinners=4)
    async with async_sessionmaker(db)() as session:
        await fetch_beatmap(
            cast(OsuClient, FakeOsu(other)),
            cast(BeatmapFiles, FakeFiles(FIXTURES / "75.osu")),
            session,
            75,
        )

    await fetch(db)  # re-fetching 22423 must not touch map 75's rows

    rows = dict(query(engine, "SELECT beatmap_id, count(*) FROM hit_objects GROUP BY 1"))
    assert rows == {22423: 254, 75: 194}


# ---------- All or nothing ----------


async def test_a_mismatch_stores_nothing(db: AsyncEngine, engine: Engine) -> None:
    with pytest.raises(BeatmapMismatchError):
        await fetch(db, FakeOsu(beatmap(count_circles=1)))

    assert counts(engine) == (0, 0)


async def test_a_failure_while_storing_rolls_back_the_map(
    db: AsyncEngine, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The map row is written first; if storing the objects then fails, it must go too.
    async def broken(*args: Any) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(beatmaps, "store_hit_objects", broken)

    with pytest.raises(RuntimeError):
        await fetch(db)

    assert counts(engine) == (0, 0)


# ---------- Real osu! ----------


async def test_real_fetch(db: AsyncEngine, engine: Engine, tmp_path: Path) -> None:
    s = get_settings()
    if not s.osu_client_id or not s.osu_client_secret.get_secret_value():
        pytest.skip("OSU_CLIENT_ID / OSU_CLIENT_SECRET not set in .env")
    redis: Redis = Redis.from_url(s.redis_url)
    try:
        await redis.ping()
    except Exception:
        await redis.aclose()
        pytest.skip(f"Redis not reachable at {s.redis_url}. Start it: docker compose up -d redis")

    try:
        async with httpx.AsyncClient(timeout=20) as http:
            limiter = RateLimiter(redis)
            tokens = TokenProvider(http, redis, s.osu_client_id, s.osu_client_secret)
            osu = OsuClient(http, tokens, limiter)
            files = BeatmapFiles(http, limiter, tmp_path / "beatmaps")
            result = await fetch(db, osu, files)
    finally:
        await redis.aclose()

    assert result == FetchResult(beatmap_id=22423, version="Hard", objects=254)
    assert counts(engine) == (1, 254)
