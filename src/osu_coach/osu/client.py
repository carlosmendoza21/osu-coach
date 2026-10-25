"""osu! API v2 client: token, shared rate limit, retries, and typed responses in one place."""

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import TypeAdapter

from osu_coach.osu.auth import TokenProvider
from osu_coach.osu.ratelimiter import RateLimiter
from osu_coach.osu.retry import DEFAULT_POLICY, RetryPolicy, send_with_retries
from osu_coach.osu.schemas import OsuScore, OsuUser

API_URL = "https://osu.ppy.sh/api/v2"
MAX_SCORES = 100  # the API caps /scores/best and /scores/recent at 100

_SCORES = TypeAdapter(list[OsuScore])


class OsuApiError(Exception):
    """osu! returned an error that retrying didn't fix."""


class OsuNotFoundError(OsuApiError):
    """User or score not found."""


class OsuClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        tokens: TokenProvider,
        limiter: RateLimiter,
        retry_policy: RetryPolicy = DEFAULT_POLICY,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self.http = http
        self.tokens = tokens
        self.limiter = limiter
        self.retry_policy = retry_policy
        self.sleep = sleep
        self.rand = rand

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """GET an API path with auth, rate limiting and retries. Returns a 2xx response."""
        url = f"{API_URL}{path}"

        async def send() -> httpx.Response:
            # One attempt. send_with_retries calls this again for each retry, so every
            # attempt waits for a rate-limit slot and uses the current token.
            await self.limiter.acquire()
            token = await self.tokens.get_token()
            headers = {"Authorization": f"Bearer {token}"}
            return await self.http.get(url, headers=headers, params=params)

        response = await send_with_retries(send, self.retry_policy, self.sleep, self.rand)

        if response.status_code == 401:
            # The cached token was rejected (expired or revoked early): drop it and try once
            # more; send() fetches a fresh one. A second 401 means the credentials are bad,
            # so don't loop.
            await self.tokens.invalidate()
            response = await send_with_retries(send, self.retry_policy, self.sleep, self.rand)

        if response.status_code == 404:
            raise OsuNotFoundError(f"osu! API returned 404 for {path}")
        if not response.is_success:
            raise OsuApiError(f"osu! API returned {response.status_code} for {path}")
        return response

    async def get_user(self, user: int | str) -> OsuUser:
        """Look up a player by id (int) or username (str)."""
        if isinstance(user, str):
            # Escape spaces and symbols, and say it's a username: otherwise osu! treats a
            # username made only of digits ("123") as a user id.
            path = f"/users/{quote(user, safe='')}/osu"
            params = {"key": "username"}
        else:
            path = f"/users/{user}/osu"
            params = {"key": "id"}
        response = await self._get(path, params)
        return OsuUser.model_validate_json(response.content)

    async def get_best_scores(self, user_id: int, limit: int = 50) -> list[OsuScore]:
        """The player's top plays (by pp), best first."""
        return await self._get_scores(user_id, "best", limit, {})

    async def get_recent_scores(
        self, user_id: int, limit: int = 50, include_fails: bool = True
    ) -> list[OsuScore]:
        """The player's plays from the last 24 hours, newest first."""
        extra = {"include_fails": 1 if include_fails else 0}
        return await self._get_scores(user_id, "recent", limit, extra)

    async def _get_scores(
        self, user_id: int, kind: str, limit: int, extra: dict[str, Any]
    ) -> list[OsuScore]:
        if not 1 <= limit <= MAX_SCORES:
            raise ValueError(f"limit must be between 1 and {MAX_SCORES}, got {limit}")
        params = {"mode": "osu", "limit": limit, **extra}
        response = await self._get(f"/users/{user_id}/scores/{kind}", params)
        return _SCORES.validate_json(response.content)
