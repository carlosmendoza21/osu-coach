"""Unit tests for osu_coach.osu.auth.

Nothing real is contacted: respx fakes the osu! token endpoint and fakeredis stands in
for Redis. See tests/integration/test_auth_live.py for the same checks against the
real services.
"""

from collections.abc import AsyncIterator, Iterator
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from fakeredis import FakeAsyncRedis
from pydantic import SecretStr

from osu_coach.osu.auth import (
    CACHE_KEY,
    EXPIRY_MARGIN,
    TOKEN_URL,
    OsuAuthError,
    TokenProvider,
)

CLIENT_ID = "12345"
SECRET = "client-secret-value"
TOKEN = "access-token-value"


def token_reply(token: str = TOKEN, expires_in: int = 86400) -> httpx.Response:
    return httpx.Response(
        200, json={"token_type": "Bearer", "expires_in": expires_in, "access_token": token}
    )


@pytest.fixture
def osu() -> Iterator[respx.MockRouter]:
    # assert_all_mocked: any request we didn't set up fails the test instead of
    # reaching the real osu! servers.
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield router


@pytest.fixture
async def redis() -> AsyncIterator[FakeAsyncRedis]:
    client = FakeAsyncRedis()
    yield client
    await client.aclose()


@pytest.fixture
async def provider(redis: FakeAsyncRedis) -> AsyncIterator[TokenProvider]:
    async with httpx.AsyncClient() as http:
        yield TokenProvider(http, redis, CLIENT_ID, SecretStr(SECRET))


def assert_no_secrets(err: BaseException) -> None:
    text = f"{err!s} {err!r}"
    assert SECRET not in text, f"client secret leaked into error: {text}"
    assert TOKEN not in text, f"access token leaked into error: {text}"


# ---------- get_token: cache ----------


async def test_cache_hit_does_not_call_osu(
    provider: TokenProvider, redis: FakeAsyncRedis, osu: respx.MockRouter
) -> None:
    await redis.set(CACHE_KEY, "cached-token")
    route = osu.post(TOKEN_URL).mock(return_value=token_reply())

    assert await provider.get_token() == "cached-token"
    assert route.call_count == 0


async def test_cache_miss_fetches_and_stores(
    provider: TokenProvider, redis: FakeAsyncRedis, osu: respx.MockRouter
) -> None:
    route = osu.post(TOKEN_URL).mock(return_value=token_reply())

    assert await provider.get_token() == TOKEN
    assert route.call_count == 1
    assert await redis.get(CACHE_KEY) == TOKEN.encode()


async def test_second_call_reuses_cached_token(
    provider: TokenProvider, osu: respx.MockRouter
) -> None:
    route = osu.post(TOKEN_URL).mock(return_value=token_reply())

    first = await provider.get_token()
    second = await provider.get_token()

    assert first == second == TOKEN
    assert route.call_count == 1, "second call should come from Redis, not osu!"


@pytest.mark.parametrize(
    "expires_in",
    [
        86400,  # what osu! sends today (24 h)
        3600,  # a shorter lifetime must shorten the cache too
        EXPIRY_MARGIN + 1,  # smallest value that still leaves a positive TTL
    ],
)
async def test_ttl_is_expires_in_minus_margin(
    provider: TokenProvider, redis: FakeAsyncRedis, osu: respx.MockRouter, expires_in: int
) -> None:
    osu.post(TOKEN_URL).mock(return_value=token_reply(expires_in=expires_in))

    await provider.get_token()

    ttl = await redis.ttl(CACHE_KEY)
    want = expires_in - EXPIRY_MARGIN
    # The margin keeps us from using a token that expires mid-request.
    assert ttl == want, f"expires_in={expires_in}: TTL {ttl}, expected {want}"


async def test_expired_cache_entry_triggers_new_fetch(
    provider: TokenProvider, redis: FakeAsyncRedis, osu: respx.MockRouter
) -> None:
    route = osu.post(TOKEN_URL).mock(
        side_effect=[token_reply("old-token"), token_reply("new-token")]
    )

    assert await provider.get_token() == "old-token"
    await redis.delete(CACHE_KEY)  # what Redis does when the TTL runs out
    assert await provider.get_token() == "new-token"
    assert route.call_count == 2


# ---------- _fetch_token: request ----------


async def test_request_is_client_credentials_form(
    provider: TokenProvider, osu: respx.MockRouter
) -> None:
    route = osu.post(TOKEN_URL).mock(return_value=token_reply())

    await provider.get_token()

    request = route.calls.last.request
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
    assert form == {
        "client_id": CLIENT_ID,
        # The real value, not SecretStr's "**********" mask.
        "client_secret": SECRET,
        "grant_type": "client_credentials",
        "scope": "public",
    }


# ---------- _fetch_token: error handling ----------


@pytest.mark.parametrize(
    "status",
    [
        400,  # osu! answers invalid_client with 400 for some bad-credential cases
        401,  # wrong client ID or secret
    ],
)
async def test_bad_credentials_raise_auth_error(
    provider: TokenProvider, redis: FakeAsyncRedis, osu: respx.MockRouter, status: int
) -> None:
    osu.post(TOKEN_URL).mock(return_value=httpx.Response(status, json={"error": "invalid_client"}))

    with pytest.raises(OsuAuthError) as exc:
        await provider.get_token()

    assert_no_secrets(exc.value)
    assert await redis.get(CACHE_KEY) is None, "a failed fetch must not cache anything"


@pytest.mark.parametrize(
    "status",
    [
        403,  # not a credentials problem we know how to fix
        429,  # rate limited: should be retried later, not reported as bad credentials
        500,
        502,
        503,  # osu! down: the caller (later, the retry layer) must see an HTTP error
    ],
)
async def test_other_http_errors_are_not_auth_errors(
    provider: TokenProvider, redis: FakeAsyncRedis, osu: respx.MockRouter, status: int
) -> None:
    osu.post(TOKEN_URL).mock(return_value=httpx.Response(status))

    with pytest.raises(httpx.HTTPStatusError) as exc:
        await provider.get_token()

    assert not isinstance(exc.value, OsuAuthError)
    assert exc.value.response.status_code == status
    assert await redis.get(CACHE_KEY) is None


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("timed out"),
    ],
)
async def test_network_errors_pass_through(
    provider: TokenProvider, osu: respx.MockRouter, error: httpx.TransportError
) -> None:
    osu.post(TOKEN_URL).mock(side_effect=error)

    with pytest.raises(type(error)):
        await provider.get_token()


@pytest.mark.parametrize(
    ("reply", "why"),
    [
        (httpx.Response(200, json={"token_type": "Bearer", "expires_in": 86400}), "no token"),
        (
            httpx.Response(200, json={"token_type": "Bearer", "access_token": TOKEN}),
            "no expiry (error text would include the token)",
        ),
        (
            httpx.Response(
                200,
                json={"token_type": "Bearer", "expires_in": "soon", "access_token": TOKEN},
            ),
            "expiry is not a number",
        ),
        (httpx.Response(200, text="<html>maintenance</html>"), "HTML instead of JSON"),
        (httpx.Response(200, text=""), "empty body"),
    ],
)
async def test_broken_reply_raises_auth_error_without_leaking(
    provider: TokenProvider,
    redis: FakeAsyncRedis,
    osu: respx.MockRouter,
    reply: httpx.Response,
    why: str,
) -> None:
    osu.post(TOKEN_URL).mock(return_value=reply)

    with pytest.raises(OsuAuthError) as exc:
        await provider.get_token()

    assert_no_secrets(exc.value)
    # `from None` hides pydantic's error (which quotes the reply) from tracebacks.
    assert exc.value.__cause__ is None and exc.value.__suppress_context__, why
    assert await redis.get(CACHE_KEY) is None, why
