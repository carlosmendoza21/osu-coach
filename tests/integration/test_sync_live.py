"""Integration tests for osu_coach.sync against a real Postgres.

Run with:  uv run pytest -m integration
Needs:     Postgres (docker compose up -d postgres). The last test also needs Redis and
           osu! credentials, and skips without them. Skips with the reason otherwise.
Uses a separate `<db>_test` database (see conftest.py). Most tests feed the sync from
FakeOsu (saved real responses), so they're exact and need no network.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import Any, cast

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import Engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from osu_coach.config import get_settings
from osu_coach.osu.auth import TokenProvider
from osu_coach.osu.client import OsuApiError, OsuClient
from osu_coach.osu.ratelimiter import RateLimiter
from osu_coach.osu.schemas import OsuBeatmap, OsuScore
from osu_coach.sync import SyncResult, beatmap_values, sync_player, upsert_beatmaps
from tests.support import FakeOsu, best_scores, failed_score, load

pytestmark = pytest.mark.integration


# ---------- Fixtures ----------


@pytest.fixture
async def db(test_url: URL) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(test_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture(autouse=True)
def empty_tables(engine: Engine) -> Iterator[None]:
    yield
    with engine.begin() as conn:  # each test starts from empty tables
        conn.execute(text("TRUNCATE players, scores, beatmaps"))


async def sync(db: AsyncEngine, osu: FakeOsu | OsuClient, who: int | str = 2) -> SyncResult:
    async with async_sessionmaker(db)() as session:
        return await sync_player(cast(OsuClient, osu), session, who)


async def query(db: AsyncEngine, sql: str) -> list[Any]:
    async with db.connect() as conn:
        return list((await conn.execute(text(sql))).all())


async def counts(db: AsyncEngine) -> tuple[int, int]:
    rows = await query(db, "SELECT (SELECT count(*) FROM players), (SELECT count(*) FROM scores)")
    return int(rows[0][0]), int(rows[0][1])


async def map_count(db: AsyncEngine) -> int:
    return int((await query(db, "SELECT count(*) FROM beatmaps"))[0][0])


# ---------- Saving ----------


async def test_first_sync_saves_player_and_scores(db: AsyncEngine) -> None:
    result = await sync(db, FakeOsu())

    assert result == SyncResult(osu_id=2, username="peppy", scores=2)
    assert await counts(db) == (1, 2)
    (player,) = await query(db, "SELECT username, country, updated_at FROM players")
    assert (player.username, player.country) == ("peppy", "AU")
    assert player.updated_at is not None


async def test_sync_is_idempotent(db: AsyncEngine) -> None:
    await sync(db, FakeOsu())
    (first,) = await query(db, "SELECT updated_at FROM players")
    await asyncio.sleep(0.05)

    await sync(db, FakeOsu())

    # Running it twice gives the same rows as once: safe for retries and repeats.
    assert await counts(db) == (1, 2)
    (second,) = await query(db, "SELECT updated_at FROM players")
    assert second.updated_at > first.updated_at, "updated_at should move on every sync"


async def test_sync_updates_changed_values(db: AsyncEngine) -> None:
    await sync(db, FakeOsu())
    score = best_scores()[0]
    changed = score.model_copy(
        update={
            "pp": 999.5,
            "statistics": score.statistics.model_copy(update={"count_miss": 7}),
        }
    )

    await sync(db, FakeOsu(best=[changed, best_scores()[1]]))

    (row,) = await query(db, f"SELECT pp, count_miss FROM scores WHERE id = {score.id}")
    assert (row.pp, row.count_miss) == (999.5, 7)


async def test_failed_play_is_saved_without_pp(db: AsyncEngine) -> None:
    failed = failed_score().model_copy(update={"user_id": 2})  # make it peppy's

    await sync(db, FakeOsu(recent=[failed]))

    (row,) = await query(db, f"SELECT pp, passed, rank FROM scores WHERE id = {failed.id}")
    assert (row.pp, row.passed, row.rank) == (None, False, "F")


async def test_score_in_best_and_recent_is_saved_once(db: AsyncEngine) -> None:
    best = best_scores()

    result = await sync(db, FakeOsu(best=best, recent=[best[0]]))

    assert result.scores == 2
    assert await counts(db) == (1, 2)


async def test_mods_and_play_time_survive(db: AsyncEngine) -> None:
    score = best_scores()[0]

    await sync(db, FakeOsu())

    (row,) = await query(db, f"SELECT mods, date FROM scores WHERE id = {score.id}")
    assert row.mods == score.mods
    assert row.date == score.created_at


# ---------- All or nothing ----------


@pytest.mark.parametrize("failing", ["get_user", "get_best_scores", "get_recent_scores"])
async def test_osu_failure_saves_nothing(db: AsyncEngine, failing: str) -> None:
    with pytest.raises(OsuApiError):
        await sync(db, FakeOsu(fail=failing))

    assert await counts(db) == (0, 0)
    assert await map_count(db) == 0


async def test_database_failure_rolls_back_the_player_too(db: AsyncEngine) -> None:
    # A score for a player that isn't in the database breaks the foreign key while the
    # scores are written. The player written just before must not be kept either.
    stray: OsuScore = failed_score().model_copy(update={"user_id": 123456789})

    with pytest.raises(IntegrityError):
        await sync(db, FakeOsu(recent=[stray]))

    assert await counts(db) == (0, 0)
    assert await map_count(db) == 0, "the maps written first are rolled back too"


# ---------- Real osu! ----------


async def test_real_sync_from_osu(db: AsyncEngine) -> None:
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
            tokens = TokenProvider(http, redis, s.osu_client_id, s.osu_client_secret)
            osu = OsuClient(http, tokens, RateLimiter(redis))
            result = await sync(db, osu, "peppy")
    finally:
        await redis.aclose()

    assert (result.osu_id, result.username) == (2, "peppy")
    assert 50 <= result.scores <= 100
    assert await counts(db) == (1, result.scores)
    (row,) = await query(
        db,
        "SELECT (SELECT count(*) FROM beatmaps), (SELECT count(DISTINCT beatmap_id) FROM scores)",
    )
    assert row[0] == row[1], "every stored score's map is stored, once"


# ---------- Beatmaps ----------


async def test_sync_saves_the_scores_maps(db: AsyncEngine) -> None:
    await sync(db, FakeOsu())

    rows = await query(
        db, "SELECT id, title, version, star_rating, od, checksum, max_combo FROM beatmaps"
    )
    want = {s.beatmap.id: s for s in best_scores()}
    assert {r.id for r in rows} == set(want)
    for r in rows:
        score = want[r.id]
        assert (r.title, r.version) == (score.beatmapset.title, score.beatmap.version)
        assert (r.star_rating, r.od) == (score.beatmap.difficulty_rating, score.beatmap.od)
        assert r.checksum == score.beatmap.checksum
        assert r.max_combo is None, "a score's map data has no max combo"


async def test_every_score_has_its_map(db: AsyncEngine) -> None:
    best = best_scores()
    failed = failed_score().model_copy(update={"user_id": 2})

    await sync(db, FakeOsu(best=best, recent=[failed]))

    orphans = await query(
        db,
        "SELECT count(*) FROM scores s LEFT JOIN beatmaps b ON b.id = s.beatmap_id "
        "WHERE b.id IS NULL",
    )
    assert orphans[0][0] == 0
    assert await map_count(db) == 3, "the failed play's map is stored too"


async def test_a_map_played_several_times_is_stored_once(db: AsyncEngine) -> None:
    best = best_scores()
    retries = [
        best[0].model_copy(update={"id": 9_000_000_000 + i, "passed": False, "pp": None})
        for i in range(3)
    ]

    await sync(db, FakeOsu(best=best, recent=retries))

    assert await map_count(db) == 2
    assert (await counts(db))[1] == 5


async def test_maps_are_idempotent_and_updated(db: AsyncEngine) -> None:
    await sync(db, FakeOsu())
    (before,) = await query(db, "SELECT updated_at FROM beatmaps ORDER BY id LIMIT 1")
    score = best_scores()[0]
    rerated = score.model_copy(
        update={"beatmap": score.beatmap.model_copy(update={"difficulty_rating": 9.99})}
    )
    await asyncio.sleep(0.05)

    await sync(db, FakeOsu(best=[rerated, best_scores()[1]]))

    assert await map_count(db) == 2
    (row,) = await query(db, f"SELECT star_rating FROM beatmaps WHERE id = {score.beatmap.id}")
    assert row.star_rating == 9.99, "osu! re-rates maps; the new value wins"
    (after,) = await query(db, "SELECT updated_at FROM beatmaps ORDER BY id LIMIT 1")
    assert after.updated_at > before.updated_at


async def test_a_known_max_combo_survives_a_sync(db: AsyncEngine) -> None:
    # A full map fetch stored max_combo 450; a later sync only has the score's map data
    # (no max combo). COALESCE keeps 450 instead of writing NULL over it.
    full = OsuBeatmap.model_validate(load("beatmap.json"))
    async with async_sessionmaker(db)() as session:
        await upsert_beatmaps(session, [beatmap_values(full, full.beatmapset, full.max_combo)])
        await session.commit()

    await sync(db, FakeOsu())

    (row,) = await query(db, f"SELECT max_combo FROM beatmaps WHERE id = {full.id}")
    assert row.max_combo == 450


async def test_a_new_max_combo_replaces_an_old_one(db: AsyncEngine) -> None:
    full = OsuBeatmap.model_validate(load("beatmap.json"))
    async with async_sessionmaker(db)() as session:
        await upsert_beatmaps(session, [beatmap_values(full, full.beatmapset, 400)])
        await upsert_beatmaps(session, [beatmap_values(full, full.beatmapset, 450)])
        await session.commit()

    (row,) = await query(db, f"SELECT max_combo FROM beatmaps WHERE id = {full.id}")
    assert row.max_combo == 450, "COALESCE only keeps the old value when the new one is NULL"
