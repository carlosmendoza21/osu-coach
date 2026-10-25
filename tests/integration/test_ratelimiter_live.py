"""Integration tests for osu_coach.osu.ratelimiter against real Redis, real time, and
real separate processes.

Run with:  uv run pytest -m integration
Needs:     Redis (docker compose up -d redis). Skips with the reason if it isn't reachable.
Each test uses its own random key and deletes it afterwards, even if a check fails.
"""

import asyncio
import multiprocessing as mp
import time
import uuid
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from osu_coach.config import get_settings
from osu_coach.osu.ratelimiter import RateLimiter

pytestmark = pytest.mark.integration


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
    yield client
    await client.aclose()


@pytest.fixture
async def key(redis: Redis) -> AsyncIterator[str]:
    name = f"test:ratelimit:{uuid.uuid4().hex}"
    yield name
    await redis.delete(name)  # runs even if the test failed


# ---------- Real time ----------


async def test_requests_are_spread_over_real_time(redis: Redis, key: str) -> None:
    lim = RateLimiter(redis, limit=3, window_s=1.0, key=key)
    start = time.monotonic()
    times = []

    for _ in range(5):
        await lim.acquire()
        times.append(time.monotonic() - start)

    # First three at once, the next two once the first ones are 1 s old.
    assert all(t < 0.3 for t in times[:3]), f"first three should be immediate: {times}"
    assert all(0.9 <= t < 1.5 for t in times[3:]), f"last two should wait about 1 s: {times}"


async def test_key_gets_an_expiry(redis: Redis, key: str) -> None:
    lim = RateLimiter(redis, limit=3, window_s=60.0, key=key)

    await lim.try_acquire()

    ttl = await redis.ttl(key)
    assert 58 <= ttl <= 60, f"TTL {ttl}, expected about 60"


# ---------- Shared limit across connections and processes ----------


async def test_many_connections_share_one_limit(redis: Redis, key: str) -> None:
    url = get_settings().redis_url
    clients = [Redis.from_url(url) for _ in range(20)]
    try:
        limiters = [RateLimiter(c, limit=3, window_s=60.0, key=key) for c in clients]
        results = await asyncio.gather(*(lim.try_acquire() for lim in limiters))
    finally:
        for c in clients:
            await c.aclose()

    allowed = sum(1 for r in results if r == 0.0)
    assert allowed == 3, f"20 connections at once: {allowed} allowed, limit is 3"
    assert await redis.zcard(key) == 3, "refused attempts must not stay in the set"


def _burst_worker(url: str, key: str, go_at: float, tries: int, out: "mp.Queue[int]") -> None:
    """Runs in a separate process: waits for the shared start time, then tries `tries` times."""

    async def run() -> int:
        client: Redis = Redis.from_url(url)
        lim = RateLimiter(client, limit=3, window_s=60.0, key=key)
        await client.ping()  # connect before the start time, so all fire together
        while time.time() < go_at:
            pass
        allowed = 0
        for _ in range(tries):
            if await lim.try_acquire() == 0.0:
                allowed += 1
        await client.aclose()
        return allowed

    out.put(asyncio.run(run()))


@pytest.mark.parametrize("processes", [4, 8])
def test_separate_processes_never_exceed_limit(processes: int) -> None:
    # The reason the count lives in Redis: the API server and each worker are separate
    # processes. An earlier check-then-add version let 4 through a limit of 3 here.
    url = get_settings().redis_url
    asyncio.run(_require_redis(url))

    for round_no in range(3):  # races are timing-dependent, so try a few times
        key = f"test:ratelimit:{uuid.uuid4().hex}"
        out: mp.Queue[int] = mp.Queue()
        go_at = time.time() + 2.0  # time for every process to start up
        procs = [
            mp.Process(target=_burst_worker, args=(url, key, go_at, 3, out))
            for _ in range(processes)
        ]
        try:
            for p in procs:
                p.start()
            allowed = sum(out.get(timeout=30) for _ in procs)
        finally:
            for p in procs:
                p.join(timeout=10)
                if p.is_alive():
                    p.terminate()
            asyncio.run(_delete(url, key))

        assert allowed == 3, (
            f"round {round_no + 1}: {processes} processes x 3 tries let {allowed} through, "
            "limit is 3"
        )


async def _require_redis(url: str) -> None:
    client: Redis = Redis.from_url(url)
    try:
        await client.ping()
    except Exception:
        pytest.skip(f"Redis not reachable at {url}. Start it: docker compose up -d redis")
    finally:
        await client.aclose()


async def _delete(url: str, key: str) -> None:
    client: Redis = Redis.from_url(url)
    await client.delete(key)
    await client.aclose()
