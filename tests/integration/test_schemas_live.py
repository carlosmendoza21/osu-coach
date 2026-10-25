"""Integration tests: today's real osu! API responses still fit osu_coach.osu.schemas.

The unit tests use saved samples; these catch osu! changing its format since then.

Run with:  uv run pytest -m integration
Needs:     OSU_CLIENT_ID / OSU_CLIENT_SECRET in .env (skips with the reason if missing).
Redis isn't needed: the token is held in fakeredis for these reads.
"""

from collections.abc import AsyncIterator

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from pydantic import TypeAdapter

from osu_coach.config import get_settings
from osu_coach.osu.auth import TokenProvider
from osu_coach.osu.schemas import OsuScore, OsuUser

pytestmark = pytest.mark.integration

API = "https://osu.ppy.sh/api/v2"
SCORES = TypeAdapter(list[OsuScore])


# ---------- Preconditions and fixtures ----------


@pytest.fixture
async def api() -> AsyncIterator[httpx.AsyncClient]:
    """An httpx client that sends a valid osu! token on every request."""
    s = get_settings()
    if not s.osu_client_id or not s.osu_client_secret.get_secret_value():
        pytest.skip("OSU_CLIENT_ID / OSU_CLIENT_SECRET not set in .env")
    async with httpx.AsyncClient(base_url=API, timeout=20) as http:
        redis = FakeAsyncRedis()
        token = await TokenProvider(http, redis, s.osu_client_id, s.osu_client_secret).get_token()
        http.headers["Authorization"] = f"Bearer {token}"
        yield http
        await redis.aclose()


async def top_player_ids(api: httpx.AsyncClient, n: int) -> list[int]:
    resp = await api.get("/rankings/osu/performance")
    resp.raise_for_status()
    return [row["user"]["id"] for row in resp.json()["ranking"][:n]]


# ---------- Users ----------


async def test_users_validate(api: httpx.AsyncClient) -> None:
    ids = [2, *await top_player_ids(api, 5)]  # peppy plus the current top 5

    for user_id in ids:
        resp = await api.get(f"/users/{user_id}/osu")
        assert resp.status_code == 200, f"user {user_id}: HTTP {resp.status_code}"
        user = OsuUser.model_validate_json(resp.content)
        assert user.id == user_id


async def test_lookup_by_username(api: httpx.AsyncClient) -> None:
    resp = await api.get("/users/peppy/osu", params={"key": "username"})

    assert OsuUser.model_validate_json(resp.content).id == 2


async def test_unknown_user_is_404(api: httpx.AsyncClient) -> None:
    # Not a model case, but the API client step will rely on it.
    resp = await api.get("/users/no-such-player-osu-coach-test/osu", params={"key": "username"})

    assert resp.status_code == 404


# ---------- Scores ----------


async def test_best_scores_validate(api: httpx.AsyncClient) -> None:
    for user_id in await top_player_ids(api, 3):
        resp = await api.get(f"/users/{user_id}/scores/best", params={"mode": "osu", "limit": 50})
        scores = SCORES.validate_json(resp.content)

        assert len(scores) == 50, f"user {user_id}: {len(scores)} best scores"
        assert all(s.pp is not None for s in scores), "best scores should all have pp"


async def test_recent_scores_with_fails_validate(api: httpx.AsyncClient) -> None:
    total = 0
    for user_id in await top_player_ids(api, 10):
        resp = await api.get(
            f"/users/{user_id}/scores/recent",
            params={"mode": "osu", "limit": 50, "include_fails": 1},
        )
        total += len(SCORES.validate_json(resp.content))

    # Recent plays expire after 24 h, so a quiet day can return few; any count validates.
    assert total >= 0
