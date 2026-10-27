"""Integration tests for GET /players/{username} against a real Postgres.

Run with:  uv run pytest -m integration
Needs:     Postgres (docker compose up -d postgres). Skips with the reason if it isn't
           reachable. No worker, Redis or osu! needed.
Uses a separate `<db>_test` database (see conftest.py). The real queries run against it;
for the endpoint, the sync is replaced with one that runs the real sync_player with a fake
osu! (saved responses), so everything from HTTP down to the tables is real.
"""

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from sqlalchemy import Engine, insert, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from osu_coach.api import players
from osu_coach.db import get_session
from osu_coach.main import create_app
from osu_coach.models import Beatmap, Player, Score
from osu_coach.osu.client import OsuClient, OsuNotFoundError
from osu_coach.sync import beatmap_values, player_values, score_values, sync_player
from tests.support import FakeOsu, best_scores, failed_score, user

pytestmark = pytest.mark.integration

BASE_SCORE = score_values(best_scores()[0])
BASE_MAP = beatmap_values(best_scores()[0].beatmap, best_scores()[0].beatmapset)
T0 = datetime(2026, 1, 1, tzinfo=UTC)


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


@pytest.fixture
async def session(db: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(db)() as s:
        yield s


async def add_player(db: AsyncEngine, osu_id: int = 2, **changes: Any) -> None:
    values = {**player_values(user()), "osu_id": osu_id, **changes}
    async with db.begin() as conn:
        await conn.execute(insert(Player).values(**values))


async def add_scores(db: AsyncEngine, rows: list[dict[str, Any]]) -> None:
    rows = [{**BASE_SCORE, **row} for row in rows]
    async with db.begin() as conn:
        # Scores need their map stored first (foreign key); these all use BASE_SCORE's.
        await conn.execute(pg_insert(Beatmap).values(**BASE_MAP).on_conflict_do_nothing())
        await conn.execute(insert(Score).values(rows))


class SyncIntoTestDb:
    """The endpoint's sync, run in-process: the real sync_player with a fake osu!."""

    def __init__(self, db: AsyncEngine) -> None:
        self.db = db
        self.calls: list[str] = []
        self.osu = FakeOsu()
        self.error: Exception | None = None

    async def __call__(self, username: str) -> dict[str, Any]:
        self.calls.append(username)
        if self.error is not None:
            raise self.error
        async with async_sessionmaker(self.db)() as s:
            result = await sync_player(cast(OsuClient, self.osu), s, username)
        return {"osu_id": result.osu_id, "username": result.username, "scores": result.scores}


@pytest.fixture
def fake_sync(db: AsyncEngine) -> SyncIntoTestDb:
    return SyncIntoTestDb(db)


@pytest.fixture
async def api(db: AsyncEngine, fake_sync: SyncIntoTestDb) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()

    async def test_session() -> AsyncIterator[AsyncSession]:
        async with async_sessionmaker(db)() as s:
            yield s

    app.dependency_overrides[get_session] = test_session
    app.dependency_overrides[players.get_sync] = lambda: fake_sync
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ---------- find_player ----------


@pytest.mark.parametrize("name", ["peppy", "PEPPY", "Peppy", "pEpPy"])
async def test_find_player_ignores_case(db: AsyncEngine, session: AsyncSession, name: str) -> None:
    await add_player(db)

    player = await players.find_player(session, name)

    assert player is not None and player.osu_id == 2


async def test_find_player_unknown(db: AsyncEngine, session: AsyncSession) -> None:
    await add_player(db)

    assert await players.find_player(session, "someone-else") is None


async def test_find_player_prefers_the_latest_sync(db: AsyncEngine, session: AsyncSession) -> None:
    # Someone renamed and another player took the old name: both rows carry it.
    await add_player(db, osu_id=1, updated_at=T0)
    await add_player(db, osu_id=3, updated_at=T0 + timedelta(days=1))

    player = await players.find_player(session, "peppy")

    assert player is not None and player.osu_id == 3


# ---------- best_scores / recent_scores ----------


async def test_best_scores_top_50_by_pp_without_nulls(
    db: AsyncEngine, session: AsyncSession
) -> None:
    await add_player(db)
    await add_player(db, osu_id=3, username="other")
    await add_scores(db, [{"id": i, "pp": float(i)} for i in range(1, 61)])
    await add_scores(db, [{"id": 100 + i, "pp": None, "passed": False} for i in range(5)])
    await add_scores(db, [{"id": 999, "player_id": 3, "pp": 5000.0}])  # someone else's

    best = await players.best_scores(session, 2)

    pps = [s.pp for s in best]
    assert len(best) == players.BEST_LIMIT
    # NULLs would sort first under ORDER BY pp DESC; they must be left out.
    assert pps == [float(i) for i in range(60, 10, -1)]
    assert all(s.player_id == 2 for s in best)


async def test_recent_scores_newest_first_with_fails(
    db: AsyncEngine, session: AsyncSession
) -> None:
    await add_player(db)
    await add_scores(
        db,
        [
            {
                "id": i,
                "date": T0 + timedelta(hours=i),
                "pp": None if i % 3 == 0 else 10.0,
                "passed": i % 3 != 0,
            }
            for i in range(1, 26)
        ],
    )

    recent = await players.recent_scores(session, 2)

    assert len(recent) == players.RECENT_LIMIT
    assert [s.id for s in recent] == list(range(25, 5, -1))
    assert any(not s.passed for s in recent), "failed plays belong in recent"


async def test_no_scores(db: AsyncEngine, session: AsyncSession) -> None:
    await add_player(db)

    assert await players.best_scores(session, 2) == []
    assert await players.recent_scores(session, 2) == []


# ---------- The endpoint, end to end ----------


async def test_new_player_is_synced_and_served(
    api: httpx.AsyncClient, fake_sync: SyncIntoTestDb
) -> None:
    resp = await api.get("/players/peppy")

    assert resp.status_code == 200
    body = resp.json()
    assert body["player"]["osu_id"] == 2
    assert [s["id"] for s in body["best"]] == [s.id for s in best_scores()]
    assert fake_sync.calls == ["peppy"]


async def test_second_request_comes_from_the_database(
    api: httpx.AsyncClient, fake_sync: SyncIntoTestDb
) -> None:
    first = (await api.get("/players/peppy")).json()
    second = (await api.get("/players/PEPPY")).json()  # any letter case finds the row

    assert fake_sync.calls == ["peppy"], "fresh within the window: no second sync"
    assert second == first


async def test_stale_player_is_resynced_with_new_values(
    api: httpx.AsyncClient, fake_sync: SyncIntoTestDb, engine: Engine
) -> None:
    await api.get("/players/peppy")
    with engine.begin() as conn:
        conn.execute(text("UPDATE players SET updated_at = now() - interval '1 hour'"))
    changed = user().model_copy(
        update={"statistics": user().statistics.model_copy(update={"pp": 4321.5})}
    )
    fake_sync.osu.get_user = _returns(changed)  # type: ignore[method-assign]

    body = (await api.get("/players/peppy")).json()

    assert fake_sync.calls == ["peppy", "peppy"]
    # The session loaded the stale row before the sync; populate_existing makes the
    # response show what the sync just wrote.
    assert body["player"]["pp"] == 4321.5


async def test_failed_play_shows_in_recent(
    api: httpx.AsyncClient, fake_sync: SyncIntoTestDb
) -> None:
    fake_sync.osu.recent = [failed_score().model_copy(update={"user_id": 2})]

    body = (await api.get("/players/peppy")).json()

    assert [(s["pp"], s["passed"]) for s in body["recent"]] == [(None, False)] + [
        (s.pp, s.passed) for s in sorted(best_scores(), key=lambda s: s.created_at, reverse=True)
    ]
    assert all(s["pp"] is not None for s in body["best"])


async def test_unknown_player_is_404_and_stores_nothing(
    api: httpx.AsyncClient, fake_sync: SyncIntoTestDb, engine: Engine
) -> None:
    fake_sync.error = OsuNotFoundError("/users/nobody/osu")

    resp = await api.get("/players/nobody")

    assert resp.status_code == 404
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM players")).scalar() == 0


def _returns(value: Any) -> Any:
    async def method(*args: Any, **kwargs: Any) -> Any:
        return value

    return method
