"""Integration tests for osu_coach.osu.auth against real Redis and the real osu! API.

Run with:  uv run pytest -m integration
Needs:     Redis (docker compose up -d redis) and OSU_CLIENT_ID / OSU_CLIENT_SECRET in .env.
Skips (with the reason) if either is missing. Deletes the cached token key before and
after each test, even if a check fails.
"""

from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr
from redis.asyncio import Redis

from osu_coach.config import get_settings
from osu_coach.osu.auth import CACHE_KEY, EXPIRY_MARGIN, OsuAuthError, TokenProvider

pytestmark = pytest.mark.integration

API_URL = "https://osu.ppy.sh/api/v2"
OSU_TOKEN_LIFETIME_S = 86400


class CountingClient:
    """An httpx client that counts the requests it sends."""

    def __init__(self) -> None:
        self.requests = 0
        self.http = httpx.AsyncClient(event_hooks={"request": [self._count]}, timeout=15)

    async def _count(self, _: httpx.Request) -> None:
        self.requests += 1


# ---------- Preconditions and fixtures ----------


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    url = get_settings().redis_url
    client: Redis = Redis.from_url(url)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        pytest.skip(f"Redis not reachable at {url}. Start it: docker compose up -d redis")

    await client.delete(CACHE_KEY)
    yield client
    await client.delete(CACHE_KEY)  # runs even if the test failed
    await client.aclose()


@pytest.fixture
def credentials() -> tuple[str, SecretStr]:
    s = get_settings()
    if not s.osu_client_id or not s.osu_client_secret.get_secret_value():
        pytest.skip("OSU_CLIENT_ID / OSU_CLIENT_SECRET not set in .env")
    return s.osu_client_id, s.osu_client_secret


@pytest.fixture
async def counting() -> AsyncIterator[CountingClient]:
    client = CountingClient()
    yield client
    await client.http.aclose()


@pytest.fixture
def provider(
    counting: CountingClient, redis: Redis, credentials: tuple[str, SecretStr]
) -> TokenProvider:
    client_id, secret = credentials
    return TokenProvider(counting.http, redis, client_id, secret)


# ---------- Real token ----------


async def test_real_token_is_cached_with_ttl(provider: TokenProvider, redis: Redis) -> None:
    token = await provider.get_token()

    assert token, "osu! returned an empty token"
    assert await redis.get(CACHE_KEY) == token.encode()
    ttl = await redis.ttl(CACHE_KEY)
    want = OSU_TOKEN_LIFETIME_S - EXPIRY_MARGIN
    # A few seconds of slack for the time the request and the check take.
    assert want - 5 <= ttl <= want, f"TTL {ttl}, expected about {want}"


async def test_second_call_uses_cache(provider: TokenProvider, counting: CountingClient) -> None:
    first = await provider.get_token()
    second = await provider.get_token()

    assert first == second
    assert counting.requests == 1, f"expected 1 request to osu!, got {counting.requests}"


async def test_token_is_accepted_by_the_api(provider: TokenProvider) -> None:
    token = await provider.get_token()

    async with httpx.AsyncClient(timeout=15) as http:
        # Beatmap 75 (Disco Prince) is one of the oldest ranked maps, so it won't vanish.
        resp = await http.get(
            f"{API_URL}/beatmaps/75", headers={"Authorization": f"Bearer {token}"}
        )

    assert resp.status_code == 200, f"osu! API rejected the token: {resp.status_code}"
    assert resp.json()["id"] == 75


# ---------- Rejections ----------


async def test_wrong_secret_is_rejected(
    counting: CountingClient, redis: Redis, credentials: tuple[str, SecretStr]
) -> None:
    client_id, _ = credentials
    wrong = "definitely-not-the-secret"
    provider = TokenProvider(counting.http, redis, client_id, SecretStr(wrong))

    with pytest.raises(OsuAuthError) as exc:
        await provider.get_token()

    assert wrong not in str(exc.value)
    assert await redis.get(CACHE_KEY) is None, "a rejected fetch must not cache anything"
