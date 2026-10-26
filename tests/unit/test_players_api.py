"""Unit tests for GET /players/{username} (osu_coach.api.players).

No database, worker or osu!: the sync is a fake (the get_sync dependency), the database
queries are replaced with ones that return rows built from the saved fixtures, and the
session only answers the "reload by id" call. The real queries and the endpoint against
Postgres are in tests/integration/test_players_live.py.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from celery.exceptions import TimeoutError as CeleryTimeoutError

from osu_coach.api import players
from osu_coach.db import get_session
from osu_coach.main import create_app
from osu_coach.models import Player, Score
from osu_coach.osu.client import OsuApiError, OsuNotFoundError
from osu_coach.sync import player_values, score_values
from osu_coach.worker import osu_sync
from tests.support import best_scores, failed_score, user

NOW = datetime.now(UTC)
FRESH = NOW - timedelta(minutes=1)
STALE = NOW - timedelta(hours=1)


def player_row(updated_at: datetime = FRESH, pp: float | None = None) -> Player:
    values = player_values(user())
    if pp is not None:
        values["pp"] = pp
    return Player(**values, updated_at=updated_at)


def score_rows() -> list[Score]:
    return [Score(**score_values(s)) for s in best_scores()]


class FakeSync:
    """Stands in for the worker. Either returns the summary or raises `error`."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    async def __call__(self, username: str) -> dict[str, Any]:
        self.calls.append(username)
        if self.error is not None:
            raise self.error
        return {"osu_id": 2, "username": "peppy", "scores": 2}


class FakeSession:
    """Only the reload by id after a sync goes through the session."""

    def __init__(self, reloaded: Player | None) -> None:
        self.reloaded = reloaded
        self.gets: list[tuple[Any, ...]] = []

    async def get(self, model: type, key: int, populate_existing: bool = False) -> Player | None:
        self.gets.append((model, key, populate_existing))
        return self.reloaded


class Env:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.stored: Player | None = None
        self.sync = FakeSync()
        self.session = FakeSession(player_row(FRESH, pp=2000.0))
        self.lookups: list[str] = []
        self.score_queries: list[tuple[str, int]] = []

        async def find_player(session: Any, username: str) -> Player | None:
            self.lookups.append(username)
            return self.stored

        async def best(session: Any, osu_id: int) -> list[Score]:
            self.score_queries.append(("best", osu_id))
            return score_rows()

        async def recent(session: Any, osu_id: int) -> list[Score]:
            self.score_queries.append(("recent", osu_id))
            return [Score(**score_values(failed_score()))]

        monkeypatch.setattr(players, "find_player", find_player)
        monkeypatch.setattr(players, "best_scores", best)
        monkeypatch.setattr(players, "recent_scores", recent)

    async def get(self, path: str) -> httpx.Response:
        app = create_app()
        app.dependency_overrides[players.get_sync] = lambda: self.sync

        async def session() -> AsyncIterator[FakeSession]:
            yield self.session

        app.dependency_overrides[get_session] = session
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(monkeypatch)


# ---------- is_stale ----------


@pytest.mark.parametrize(
    ("age", "stale"),
    [
        (timedelta(seconds=0), False),
        (timedelta(minutes=9, seconds=59), False),
        (timedelta(minutes=10, seconds=1), True),  # just past STALE_AFTER
        (timedelta(days=30), True),
    ],
)
def test_is_stale(age: timedelta, stale: bool) -> None:
    assert players.is_stale(player_row(datetime.now(UTC) - age)) is stale


# ---------- When to sync ----------


async def test_fresh_player_is_served_without_a_sync(env: Env) -> None:
    env.stored = player_row(FRESH)

    resp = await env.get("/players/peppy")

    assert resp.status_code == 200
    assert env.sync.calls == [], "fresh data: no osu! requests"
    assert resp.json()["player"]["pp"] == env.stored.pp


async def test_stale_player_is_synced_and_reloaded(env: Env) -> None:
    env.stored = player_row(STALE, pp=1000.0)

    resp = await env.get("/players/peppy")

    assert resp.status_code == 200
    assert env.sync.calls == ["peppy"]
    # Reloaded by the id the worker returned, replacing the copy loaded before the sync.
    assert env.session.gets == [(Player, 2, True)]
    assert resp.json()["player"]["pp"] == 2000.0, "the synced values, not the stale ones"


async def test_unknown_player_is_synced(env: Env) -> None:
    resp = await env.get("/players/peppy")

    assert resp.status_code == 200
    assert env.sync.calls == ["peppy"]


async def test_username_is_passed_on_as_typed(env: Env) -> None:
    await env.get("/players/PePpY")

    assert env.lookups == ["PePpY"]
    assert env.sync.calls == ["PePpY"]  # osu! resolves the case; we reload by id


async def test_scores_are_queried_by_the_reloaded_id(env: Env) -> None:
    await env.get("/players/someone")

    assert env.score_queries == [("best", 2), ("recent", 2)]


# ---------- Errors ----------


async def test_player_osu_doesnt_know_is_404(env: Env) -> None:
    env.sync = FakeSync(OsuNotFoundError("/users/nobody/osu"))

    resp = await env.get("/players/nobody")

    assert resp.status_code == 404
    assert resp.json() == {"detail": "osu! player 'nobody' not found"}


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (OsuApiError("osu! API returned 503"), 502),  # the service behind us failed
        (CeleryTimeoutError("timed out"), 504),  # it took too long
    ],
)
async def test_failure_with_no_data_is_an_error(env: Env, error: Exception, status: int) -> None:
    env.sync = FakeSync(error)

    resp = await env.get("/players/peppy")

    assert resp.status_code == status
    assert "osu!" in resp.json()["detail"]


@pytest.mark.parametrize(
    "error", [OsuApiError("osu! API returned 503"), CeleryTimeoutError("timed out")]
)
async def test_failure_with_stale_data_serves_the_stale_data(env: Env, error: Exception) -> None:
    # Graceful degradation: an hour-old profile beats an error page.
    env.stored = player_row(STALE, pp=1000.0)
    env.sync = FakeSync(error)

    resp = await env.get("/players/peppy")

    assert resp.status_code == 200
    assert resp.json()["player"]["pp"] == 1000.0
    assert env.session.gets == [], "nothing to reload: the sync failed"


async def test_player_missing_after_sync_is_500(env: Env) -> None:
    env.session = FakeSession(reloaded=None)

    resp = await env.get("/players/peppy")

    assert resp.status_code == 500


# ---------- Response ----------


async def test_response_shape(env: Env) -> None:
    env.stored = player_row(FRESH)

    body = (await env.get("/players/peppy")).json()

    assert set(body) == {"player", "best", "recent"}
    assert set(body["player"]) == {
        "osu_id",
        "username",
        "country",
        "global_rank",
        "pp",
        "playcount",
        "accuracy",
        "updated_at",
    }
    assert set(body["best"][0]) == {
        "id",
        "beatmap_id",
        "score",
        "max_combo",
        "count_300",
        "count_100",
        "count_50",
        "count_miss",
        "rank",
        "pp",
        "mods",
        "date",
        "accuracy",
        "passed",
    }
    assert "player_id" not in body["best"][0], "only the response model's fields are public"


async def test_response_values(env: Env) -> None:
    env.stored = player_row(FRESH)

    body = (await env.get("/players/peppy")).json()

    first = best_scores()[0]
    assert body["player"]["username"] == "peppy"
    assert body["best"][0]["id"] == first.id
    assert body["best"][0]["mods"] == first.mods
    assert datetime.fromisoformat(body["best"][0]["date"]) == first.created_at
    failed = body["recent"][0]
    assert (failed["pp"], failed["passed"], failed["rank"]) == (None, False, "F")


# ---------- sync_via_worker ----------


async def test_sync_via_worker_waits_for_the_task(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []

    class Task:
        def get(self, timeout: float) -> dict[str, Any]:
            waits.append(timeout)
            return {"osu_id": 2, "username": "peppy", "scores": 50}

    sent: list[str] = []

    def delay(username: str) -> Task:
        sent.append(username)
        return Task()

    monkeypatch.setattr(osu_sync, "delay", delay)

    result = await players.sync_via_worker("peppy")

    assert result["osu_id"] == 2
    assert sent == ["peppy"]
    assert waits == [players.SYNC_TIMEOUT_S]
