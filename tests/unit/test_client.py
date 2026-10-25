"""Unit tests for osu_coach.osu.client.

Nothing real is contacted: respx fakes osu! (token endpoint and API, and blocks anything
unmocked), fakeredis stands in for Redis, the rate limiter runs on a fake clock, and
retry waits are only recorded. API replies are the real samples in tests/fixtures/osu/.
tests/integration/test_client_live.py runs the client against real osu! and Redis.
"""

import json
import ssl
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fakeredis import FakeAsyncRedis
from pydantic import SecretStr, ValidationError

from osu_coach.osu.auth import CACHE_KEY, TOKEN_URL, TokenProvider
from osu_coach.osu.client import (
    API_URL,
    MAX_SCORES,
    OsuApiError,
    OsuClient,
    OsuNotFoundError,
)
from osu_coach.osu.ratelimiter import DEFAULT_KEY as RATE_KEY
from osu_coach.osu.ratelimiter import RateLimiter

FIXTURES = Path(__file__).parent.parent / "fixtures" / "osu"
SECRET = "client-secret-value"
TOKEN = "token-1"
# Loading the system certificates takes ~0.2 s on Windows; do it once, not per test.
SSL_CONTEXT = ssl.create_default_context()


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def token_reply(token: str = TOKEN) -> httpx.Response:
    return httpx.Response(
        200, json={"token_type": "Bearer", "expires_in": 86400, "access_token": token}
    )


class FakeClock:
    """Time for the rate limiter: only moves when the limiter sleeps."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if len(self.sleeps) > 1_000:
            raise AssertionError("the rate limiter waited forever")
        self.now += seconds


class Env:
    """Everything a test needs: the client and the fakes behind it."""

    def __init__(self, router: respx.MockRouter, redis: FakeAsyncRedis) -> None:
        self.router = router
        self.redis = redis
        self.clock = FakeClock()
        self.retry_waits: list[float] = []
        self.token_route = router.post(TOKEN_URL).mock(return_value=token_reply())

    async def retry_sleep(self, seconds: float) -> None:
        self.retry_waits.append(seconds)

    def client(self, http: httpx.AsyncClient, rate_limit: int = 60) -> OsuClient:
        tokens = TokenProvider(http, self.redis, "12345", SecretStr(SECRET))
        limiter = RateLimiter(
            self.redis, limit=rate_limit, clock=self.clock.time, sleep=self.clock.sleep
        )
        return OsuClient(http, tokens, limiter, sleep=self.retry_sleep, rand=lambda: 1.0)

    def api(self, path: str) -> respx.Route:
        return self.router.get(f"{API_URL}{path}")


@pytest.fixture
async def env() -> AsyncIterator[Env]:
    redis = FakeAsyncRedis()
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield Env(router, redis)
    await redis.aclose()


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(verify=SSL_CONTEXT) as client:
        yield client


@pytest.fixture
def osu(env: Env, http: httpx.AsyncClient) -> OsuClient:
    return env.client(http)


def query(route: respx.Route, call: int = -1) -> dict[str, str]:
    return dict(route.calls[call].request.url.params)


# ---------- Requests ----------


async def test_get_user_by_username(env: Env, osu: OsuClient) -> None:
    route = env.api("/users/peppy/osu").mock(
        return_value=httpx.Response(200, json=load("user.json"))
    )

    user = await osu.get_user("peppy")

    assert (user.id, user.username) == (2, "peppy")
    assert query(route) == {"key": "username"}


async def test_get_user_by_id(env: Env, osu: OsuClient) -> None:
    route = env.api("/users/2/osu").mock(return_value=httpx.Response(200, json=load("user.json")))

    user = await osu.get_user(2)

    assert user.id == 2
    assert query(route) == {"key": "id"}


@pytest.mark.parametrize(
    ("username", "raw_path"),
    [
        ("Mr Ekk", b"/api/v2/users/Mr%20Ekk/osu"),  # spaces
        ("[Ryuk]", b"/api/v2/users/%5BRyuk%5D/osu"),  # brackets are allowed in names
        ("a/b", b"/api/v2/users/a%2Fb/osu"),  # a slash must not become a new path segment
        ("123", b"/api/v2/users/123/osu"),  # all digits: still a username (see key below)
    ],
)
async def test_usernames_are_escaped(
    env: Env, osu: OsuClient, username: str, raw_path: bytes
) -> None:
    route = env.router.get(url__startswith=f"{API_URL}/users/").mock(
        return_value=httpx.Response(200, json=load("user.json"))
    )

    await osu.get_user(username)

    request = route.calls.last.request
    assert request.url.raw_path.split(b"?")[0] == raw_path
    # Without key=username, osu! would treat "123" as user id 123.
    assert request.url.params["key"] == "username"


async def test_requests_carry_the_token(env: Env, osu: OsuClient) -> None:
    route = env.api("/users/2/osu").mock(return_value=httpx.Response(200, json=load("user.json")))

    await osu.get_user(2)

    assert route.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"


async def test_token_is_fetched_once_for_many_calls(env: Env, osu: OsuClient) -> None:
    env.api("/users/2/osu").mock(return_value=httpx.Response(200, json=load("user.json")))

    for _ in range(5):
        await osu.get_user(2)

    assert env.token_route.call_count == 1, "the cached token should be reused"


async def test_get_best_scores(env: Env, osu: OsuClient) -> None:
    route = env.api("/users/2/scores/best").mock(
        return_value=httpx.Response(200, json=load("scores_best.json"))
    )

    scores = await osu.get_best_scores(2)

    assert [s.id for s in scores] == [s["id"] for s in load("scores_best.json")]
    assert query(route) == {"mode": "osu", "limit": "50"}


@pytest.mark.parametrize(
    ("include_fails", "want"),
    [
        (True, "1"),  # the default: failed plays matter for coaching
        (False, "0"),
    ],
)
async def test_get_recent_scores(env: Env, osu: OsuClient, include_fails: bool, want: str) -> None:
    route = env.api("/users/2/scores/recent").mock(
        return_value=httpx.Response(200, json=[load("score_recent_failed.json")])
    )

    scores = await osu.get_recent_scores(2, limit=10, include_fails=include_fails)

    assert scores[0].passed is False
    assert query(route) == {"mode": "osu", "limit": "10", "include_fails": want}


async def test_empty_score_list(env: Env, osu: OsuClient) -> None:
    # No plays in the last 24 hours is normal, not an error.
    env.api("/users/2/scores/recent").mock(return_value=httpx.Response(200, json=[]))

    assert await osu.get_recent_scores(2) == []


@pytest.mark.parametrize("limit", [1, MAX_SCORES])
async def test_limit_bounds_are_accepted(env: Env, osu: OsuClient, limit: int) -> None:
    route = env.api("/users/2/scores/best").mock(return_value=httpx.Response(200, json=[]))

    await osu.get_best_scores(2, limit=limit)

    assert query(route)["limit"] == str(limit)


@pytest.mark.parametrize("limit", [0, -1, MAX_SCORES + 1])
@pytest.mark.parametrize("kind", ["best", "recent"])
async def test_bad_limit_is_rejected_before_any_request(
    env: Env, osu: OsuClient, limit: int, kind: str
) -> None:
    route = env.api(f"/users/2/scores/{kind}").mock(return_value=httpx.Response(200, json=[]))
    method = osu.get_best_scores if kind == "best" else osu.get_recent_scores

    with pytest.raises(ValueError):
        await method(2, limit=limit)

    assert route.call_count == 0
    assert env.token_route.call_count == 0


# ---------- Rate limit and retries ----------


async def test_every_attempt_takes_a_rate_limit_slot(env: Env, osu: OsuClient) -> None:
    env.api("/users/2/osu").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, json=load("user.json")),
        ]
    )

    await osu.get_user(2)

    # Retries count toward the limit too, or a burst of 503s would exceed it.
    assert await env.redis.zcard(RATE_KEY) == 3
    assert env.retry_waits == [1.0, 2.0]


async def test_rate_limit_makes_calls_wait(env: Env, http: httpx.AsyncClient) -> None:
    osu = env.client(http, rate_limit=2)
    env.api("/users/2/osu").mock(return_value=httpx.Response(200, json=load("user.json")))

    for _ in range(3):
        await osu.get_user(2)

    assert env.clock.sleeps == [60.0], "the third call should wait for the window"


async def test_temporary_errors_are_retried(env: Env, osu: OsuClient) -> None:
    route = env.api("/users/2/osu").mock(
        side_effect=[
            httpx.ConnectError("refused"),
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(200, json=load("user.json")),
        ]
    )

    user = await osu.get_user(2)

    assert user.id == 2
    assert route.call_count == 3
    assert env.retry_waits == [1.0, 3.0]


# ---------- Expired or revoked token (401) ----------


async def test_401_refreshes_the_token_once(env: Env, osu: OsuClient) -> None:
    env.token_route.mock(side_effect=[token_reply("old-token"), token_reply("new-token")])
    route = env.api("/users/2/osu").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json=load("user.json"))]
    )

    user = await osu.get_user(2)

    assert user.id == 2
    assert [c.request.headers["Authorization"] for c in route.calls] == [
        "Bearer old-token",
        "Bearer new-token",
    ]
    assert await env.redis.get(CACHE_KEY) == b"new-token", "the new token replaces the old"


async def test_401_with_a_revoked_cached_token(env: Env, osu: OsuClient) -> None:
    await env.redis.set(CACHE_KEY, "revoked-token")
    route = env.api("/users/2/osu").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json=load("user.json"))]
    )

    await osu.get_user(2)

    assert route.calls[0].request.headers["Authorization"] == "Bearer revoked-token"
    assert route.calls[1].request.headers["Authorization"] == f"Bearer {TOKEN}"


async def test_second_401_is_an_error_not_a_loop(env: Env, osu: OsuClient) -> None:
    route = env.api("/users/2/osu").mock(return_value=httpx.Response(401))

    with pytest.raises(OsuApiError) as exc:
        await osu.get_user(2)

    assert not isinstance(exc.value, OsuNotFoundError)
    # Exactly one refresh: a brand-new token being rejected means bad credentials.
    assert route.call_count == 2
    assert env.token_route.call_count == 2


# ---------- Errors ----------


async def test_404_is_not_found(env: Env, osu: OsuClient) -> None:
    route = env.api("/users/nobody/osu").mock(
        return_value=httpx.Response(404, json={"error": None})
    )

    with pytest.raises(OsuNotFoundError) as exc:
        await osu.get_user("nobody")

    assert isinstance(exc.value, OsuApiError), "callers can catch all osu! errors at once"
    assert "/users/nobody/osu" in str(exc.value)
    assert route.call_count == 1, "404 is permanent: no retries"


@pytest.mark.parametrize(
    "status",
    [
        400,
        403,  # e.g. a restricted account
        422,
    ],
)
async def test_other_client_errors_raise_api_error(env: Env, osu: OsuClient, status: int) -> None:
    route = env.api("/users/2/osu").mock(return_value=httpx.Response(status))

    with pytest.raises(OsuApiError) as exc:
        await osu.get_user(2)

    assert not isinstance(exc.value, OsuNotFoundError)
    assert str(status) in str(exc.value)
    assert route.call_count == 1


async def test_server_errors_after_all_retries_raise_api_error(env: Env, osu: OsuClient) -> None:
    route = env.api("/users/2/osu").mock(return_value=httpx.Response(503))

    with pytest.raises(OsuApiError, match="503"):
        await osu.get_user(2)

    assert route.call_count == 4  # DEFAULT_POLICY.max_attempts


async def test_network_error_after_all_retries_is_raised(env: Env, osu: OsuClient) -> None:
    env.api("/users/2/osu").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(httpx.ConnectError):
        await osu.get_user(2)


async def test_unexpected_reply_fails_validation(env: Env, osu: OsuClient) -> None:
    env.api("/users/2/osu").mock(return_value=httpx.Response(200, json={"id": 2}))

    with pytest.raises(ValidationError):
        await osu.get_user(2)


@pytest.mark.parametrize("status", [401, 404, 500])
async def test_errors_never_contain_secrets(env: Env, osu: OsuClient, status: int) -> None:
    env.api("/users/2/osu").mock(return_value=httpx.Response(status))

    with pytest.raises(OsuApiError) as exc:
        await osu.get_user(2)

    text = f"{exc.value!s} {exc.value!r}"
    assert TOKEN not in text
    assert SECRET not in text
