"""Unit tests for osu_coach.osu.ratelimiter.

fakeredis stands in for Redis, and a fake clock replaces real time: "sleeping" just moves
the clock forward and records how long, so a 60 s window runs instantly. Real Redis and
real processes are covered in tests/integration/test_ratelimiter_live.py.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest
from fakeredis import FakeAsyncRedis

from osu_coach.osu.ratelimiter import DEFAULT_KEY, RateLimiter

KEY = DEFAULT_KEY
# No test here needs more than a few sleeps. Far more means acquire() would wait forever;
# fail instead of hanging the suite (and CI).
MAX_SLEEPS = 1_000


class FakeClock:
    """A clock that only moves when something sleeps on it."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.record(seconds)
        self.now += seconds

    def record(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if len(self.sleeps) > MAX_SLEEPS:
            raise AssertionError(f"acquire() slept {MAX_SLEEPS} times without getting a slot")


@pytest.fixture
async def redis() -> AsyncIterator[FakeAsyncRedis]:
    client = FakeAsyncRedis()
    yield client
    await client.aclose()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def make(
    redis: FakeAsyncRedis, clock: FakeClock, limit: int = 3, window_s: float = 60.0
) -> RateLimiter:
    return RateLimiter(redis, limit=limit, window_s=window_s, clock=clock.time, sleep=clock.sleep)


# ---------- try_acquire: allowing and refusing ----------


@pytest.mark.parametrize(
    "limit",
    [
        1,  # smallest useful limit
        3,
        60,  # the real setting
    ],
)
async def test_allows_exactly_limit_then_refuses(
    redis: FakeAsyncRedis, clock: FakeClock, limit: int
) -> None:
    lim = make(redis, clock, limit=limit)

    results = [await lim.try_acquire() for _ in range(limit + 1)]

    assert results[:limit] == [0.0] * limit, f"first {limit} should be allowed: {results}"
    assert results[limit] > 0, f"request {limit + 1} should be refused: {results}"


async def test_refused_attempts_are_not_recorded(redis: FakeAsyncRedis, clock: FakeClock) -> None:
    lim = make(redis, clock, limit=3)
    for _ in range(3):
        await lim.try_acquire()

    for _ in range(10):
        assert await lim.try_acquire() > 0

    # If refused attempts stayed in the set, retries would keep the window full forever.
    assert await redis.zcard(KEY) == 3


@pytest.mark.parametrize(
    ("entry_ages", "want_wait"),
    [
        ([0, 0, 0], 60.0),  # all at once: wait the full window
        ([30, 20, 10], 30.0),  # oldest is 30 s old: 30 s left until it leaves
        ([59.5, 1, 1], 0.5),  # oldest is about to leave
    ],
)
async def test_wait_is_until_oldest_entry_leaves(
    redis: FakeAsyncRedis, clock: FakeClock, entry_ages: list[float], want_wait: float
) -> None:
    lim = make(redis, clock, limit=3)
    start = clock.now
    for age in entry_ages:
        clock.now = start - age
        assert await lim.try_acquire() == 0.0
    clock.now = start

    wait = await lim.try_acquire()

    assert wait == pytest.approx(want_wait), f"ages {entry_ages}: wait {wait}, expected {want_wait}"


@pytest.mark.parametrize(
    ("elapsed", "allowed"),
    [
        (59.999, False),  # still inside the window
        (60.0, True),  # exactly one window later: the old entry no longer counts
        (61.0, True),
    ],
)
async def test_window_boundary(
    redis: FakeAsyncRedis, clock: FakeClock, elapsed: float, allowed: bool
) -> None:
    lim = make(redis, clock, limit=1)
    await lim.try_acquire()
    clock.now += elapsed

    got = await lim.try_acquire()

    assert (got == 0.0) is allowed, f"after {elapsed} s: try_acquire returned {got}"


async def test_old_entries_are_removed(redis: FakeAsyncRedis, clock: FakeClock) -> None:
    lim = make(redis, clock, limit=3)
    for _ in range(3):
        await lim.try_acquire()
    clock.now += 120

    await lim.try_acquire()

    assert await redis.zcard(KEY) == 1, "entries from two windows ago should be gone"


async def test_same_moment_requests_are_all_counted(
    redis: FakeAsyncRedis, clock: FakeClock
) -> None:
    # Members must be unique: with the timestamp alone as the member, requests at the
    # same moment would overwrite each other and count as one.
    lim = make(redis, clock, limit=5)

    for _ in range(5):
        assert await lim.try_acquire() == 0.0

    assert await redis.zcard(KEY) == 5


async def test_concurrent_callers_share_the_limit(redis: FakeAsyncRedis, clock: FakeClock) -> None:
    # 20 callers at the same moment, each with its own limiter on the same key.
    limiters = [make(redis, clock, limit=3) for _ in range(20)]

    results = await asyncio.gather(*(lim.try_acquire() for lim in limiters))

    assert sum(1 for r in results if r == 0.0) == 3


async def test_different_keys_have_separate_limits(redis: FakeAsyncRedis, clock: FakeClock) -> None:
    a = RateLimiter(redis, limit=1, key="a", clock=clock.time, sleep=clock.sleep)
    b = RateLimiter(redis, limit=1, key="b", clock=clock.time, sleep=clock.sleep)

    assert await a.try_acquire() == 0.0
    assert await b.try_acquire() == 0.0, "a full limiter must not block another key"


# ---------- try_acquire: key expiry ----------


@pytest.mark.parametrize(
    ("window_s", "want_ttl"),
    [
        (60.0, 60),
        (2.5, 3),  # rounded up, so the key never expires while an entry still counts
        (0.5, 1),  # never 0, which Redis would reject
    ],
)
async def test_key_expires_after_window(
    redis: FakeAsyncRedis, clock: FakeClock, window_s: float, want_ttl: int
) -> None:
    lim = make(redis, clock, window_s=window_s)

    await lim.try_acquire()

    assert await redis.ttl(KEY) == want_ttl


# ---------- acquire ----------


async def test_acquire_under_limit_does_not_sleep(redis: FakeAsyncRedis, clock: FakeClock) -> None:
    lim = make(redis, clock, limit=3)

    for _ in range(3):
        await lim.acquire()

    assert clock.sleeps == []


async def test_acquire_spreads_requests_over_windows(
    redis: FakeAsyncRedis, clock: FakeClock
) -> None:
    lim = make(redis, clock, limit=2)
    start = clock.now
    times = []

    for _ in range(5):
        await lim.acquire()
        times.append(clock.now - start)

    assert times == [0, 0, 60, 60, 120]
    assert clock.sleeps == [60.0, 60.0]


async def test_acquire_records_slot_when_window_empties_while_waiting(
    redis: FakeAsyncRedis,
) -> None:
    # Regression: an earlier version returned from acquire() without recording its
    # request if the window emptied between the refusal and the wait calculation.
    clock = FakeClock()

    async def oversleep(seconds: float) -> None:
        clock.record(seconds)
        clock.now += seconds + 100  # the process was paused much longer than asked

    lim = RateLimiter(redis, limit=1, clock=clock.time, sleep=oversleep)
    await lim.acquire()

    await lim.acquire()

    in_window = await redis.zcount(KEY, clock.now - 60 + 1e-6, clock.now)
    assert in_window == 1, "acquire() returned without taking a slot"
