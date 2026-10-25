"""Retry osu! requests that fail for temporary reasons, with backoff and jitter."""

import asyncio
import math
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4  # 1 try + 3 retries
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0

    def __post_init__(self) -> None:
        # Refuse settings that would make send_with_retries never send or never wait.
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts must be at least 1, got {self.max_attempts}")
        if self.base_delay_s < 0 or self.max_delay_s < 0:
            raise ValueError("delays must not be negative")


DEFAULT_POLICY = RetryPolicy()


def backoff_delay(attempt: int, policy: RetryPolicy, rand: Callable[[], float]) -> float:
    """Return the delay in seconds before the next retry attempt.

    `attempt` is the attempt that just failed: the first attempt is 0, the second is 1, etc.
    The backoff doubles each time and is capped at `policy.max_delay_s`; full jitter then
    picks a random point between 0 and that, so many workers don't all retry together.
    """
    backoff = min(policy.base_delay_s * 2.0**attempt, policy.max_delay_s)
    return rand() * backoff


def retry_after_s(response: httpx.Response) -> float | None:
    """Return the number of seconds to wait before retrying, or None if not specified."""
    header = response.headers.get("Retry-After")
    if header is None:
        return None
    try:
        seconds = float(header)
    except ValueError:
        return None  # e.g. the HTTP-date form, which we don't need
    # float() accepts "inf" and "nan"; neither, nor a negative value, is a real wait.
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


async def send_with_retries(
    send: Callable[[], Awaitable[httpx.Response]],
    policy: RetryPolicy = DEFAULT_POLICY,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rand: Callable[[], float] = random.random,
) -> httpx.Response:
    """Call `send()` until it succeeds, fails permanently, or attempts run out.

    Returns the last response (the caller decides whether it's an error). Network errors
    on the last attempt are raised.
    """
    for attempt in range(policy.max_attempts):
        last_attempt = attempt == policy.max_attempts - 1
        try:
            response = await send()
        except httpx.TransportError:
            # Network failure (connection refused, timeout, ...).
            if last_attempt:
                raise
            delay = backoff_delay(attempt, policy, rand)
        else:
            # Done if it worked, if retrying can't help, or if we're out of attempts.
            # Never sleep after the last attempt: the caller would wait for nothing.
            if response.status_code not in RETRY_STATUSES or last_attempt:
                return response
            retry_after = retry_after_s(response)
            delay = retry_after if retry_after is not None else backoff_delay(attempt, policy, rand)

        # Both failure paths wait here. The cap also limits a server's Retry-After.
        await sleep(min(delay, policy.max_delay_s))

    # Unreachable: the last attempt always returns or raises above.
    raise AssertionError("send_with_retries ran out of attempts without returning")
