"""Shared rate limiter for osu! API calls: a sliding window log in a Redis sorted set."""

import asyncio
import math
import time
import uuid
from collections.abc import Awaitable, Callable

from redis.asyncio import Redis

DEFAULT_KEY = "osu:ratelimit"


class RateLimiter:
    """Allows at most `limit` requests per `window_s` seconds across all processes."""

    def __init__(
        self,
        redis: Redis,
        limit: int = 60,
        window_s: float = 60.0,
        key: str = DEFAULT_KEY,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.redis = redis
        self.limit = limit
        self.window_s = window_s
        self.key = key
        self.clock = clock
        self.sleep = sleep

    async def try_acquire(self) -> float:
        """Try to acquire a rate limit slot.

        Returns 0.0 if the slot was taken, otherwise the seconds to wait before trying again.
        """
        now = self.clock()
        window_start = now - self.window_s
        member = f"{now}:{uuid.uuid4().hex}"

        # Add first, then count, and undo below if over the limit. Adding before counting
        # is what keeps the limit safe across processes: everyone allowed in already sees
        # everyone allowed before them. The transaction keeps the count and the oldest
        # entry from the same snapshot (for the wait time) and sends it all in one trip.
        async with self.redis.pipeline(transaction=True) as pipe:
            # Remove old entries from the sorted set.
            pipe.zremrangebyscore(self.key, 0, window_start)
            # Add this request with the current timestamp as the score.
            pipe.zadd(self.key, {member: now})
            # Count the entries in the current window (including this one).
            pipe.zcard(self.key)
            # The oldest entry, to work out how long to wait if we're over the limit.
            pipe.zrange(self.key, 0, 0, withscores=True)
            # Let Redis delete the key if the app stops using it.
            pipe.expire(self.key, math.ceil(self.window_s))
            _, _, count, oldest, _ = await pipe.execute()

        if count <= self.limit:
            return 0.0

        # Over the limit: take our entry back out, so refused attempts don't count.
        await self.redis.zrem(self.key, member)
        oldest_timestamp = float(oldest[0][1])
        # A slot frees up when the oldest entry leaves the window. Never return 0 here,
        # since 0 means "allowed".
        return max(oldest_timestamp + self.window_s - now, 0.001)

    async def acquire(self) -> None:
        """Acquire a rate limit slot, waiting if necessary."""
        while (wait_time := await self.try_acquire()) > 0:
            await self.sleep(wait_time)
