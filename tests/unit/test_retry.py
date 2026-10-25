"""Unit tests for osu_coach.osu.retry.

No network and no real waiting: `send` is a scripted fake server, `sleep` only records
how long it was asked to wait, and `rand` is fixed so the jittered delays are exact.
"""

import math

import httpx
import pytest

from osu_coach.osu.retry import (
    DEFAULT_POLICY,
    RETRY_STATUSES,
    RetryPolicy,
    backoff_delay,
    retry_after_s,
    send_with_retries,
)

Reply = httpx.Response | Exception


class FakeServer:
    """Returns (or raises) the scripted replies in order, one per call."""

    def __init__(self, *replies: Reply) -> None:
        self.replies = list(replies)
        self.calls = 0

    async def send(self) -> httpx.Response:
        reply = self.replies[self.calls]
        self.calls += 1
        if isinstance(reply, Exception):
            raise reply
        return reply


class SleepRecorder:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def status(code: int, retry_after: str | None = None) -> httpx.Response:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return httpx.Response(code, headers=headers)


async def run(
    server: FakeServer, policy: RetryPolicy = DEFAULT_POLICY, rand: float = 1.0
) -> tuple[httpx.Response, SleepRecorder]:
    sleep = SleepRecorder()
    response = await send_with_retries(server.send, policy, sleep, lambda: rand)
    return response, sleep


# ---------- backoff_delay ----------


@pytest.mark.parametrize(
    ("attempt", "rand", "want"),
    [
        (0, 1.0, 1.0),  # first failure: one base delay
        (1, 1.0, 2.0),  # doubles each time
        (2, 1.0, 4.0),
        (3, 1.0, 8.0),
        (5, 1.0, 30.0),  # 32 s, capped at max_delay_s
        (50, 1.0, 30.0),  # huge attempt numbers must not overflow or exceed the cap
        (2, 0.5, 2.0),  # full jitter: rand scales the whole delay
        (3, 0.0, 0.0),  # rand 0 means retry immediately; allowed with full jitter
    ],
)
def test_backoff_delay(attempt: int, rand: float, want: float) -> None:
    got = backoff_delay(attempt, DEFAULT_POLICY, lambda: rand)

    assert got == want, f"attempt {attempt}, rand {rand}: {got}, expected {want}"


@pytest.mark.parametrize("attempt", [0, 1, 2, 3, 4, 5, 10])
@pytest.mark.parametrize("rand", [0.0, 0.25, 0.999])
def test_backoff_delay_stays_within_jitter_range(attempt: int, rand: float) -> None:
    cap = min(DEFAULT_POLICY.base_delay_s * 2**attempt, DEFAULT_POLICY.max_delay_s)

    got = backoff_delay(attempt, DEFAULT_POLICY, lambda: rand)

    # Full jitter spreads retries across the whole range, so workers that failed
    # together don't all retry together.
    assert 0.0 <= got <= cap


def test_backoff_delay_uses_policy_values() -> None:
    policy = RetryPolicy(base_delay_s=0.25, max_delay_s=1.0)

    assert [backoff_delay(a, policy, lambda: 1.0) for a in range(5)] == [
        0.25,
        0.5,
        1.0,
        1.0,  # capped
        1.0,
    ]


# ---------- retry_after_s ----------


@pytest.mark.parametrize(
    ("header", "want"),
    [
        (None, None),  # no header: use backoff
        ("5", 5.0),
        ("0", 0.0),  # "retry now" is valid
        ("1.5", 1.5),
        (" 7 ", 7.0),  # surrounding spaces are tolerated
        ("-5", None),  # a negative wait is invalid
        ("inf", None),  # float() accepts these; sleeping on them would hang or crash
        ("Infinity", None),
        ("nan", None),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),  # HTTP-date form: not supported, fall back
        ("", None),
        ("soon", None),
    ],
)
def test_retry_after_s(header: str | None, want: float | None) -> None:
    got = retry_after_s(status(429, header))

    assert got == want, f"Retry-After {header!r}: {got}, expected {want}"


# ---------- RetryPolicy ----------


def test_default_policy() -> None:
    assert (
        DEFAULT_POLICY.max_attempts,
        DEFAULT_POLICY.base_delay_s,
        DEFAULT_POLICY.max_delay_s,
    ) == (4, 1.0, 30.0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},  # would never send anything
        {"max_attempts": -1},
        {"base_delay_s": -1.0},
        {"max_delay_s": -1.0},
    ],
)
def test_policy_rejects_bad_settings(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)  # type: ignore[arg-type]


# ---------- send_with_retries: responses ----------


async def test_success_first_try_does_not_wait() -> None:
    server = FakeServer(status(200))

    response, sleep = await run(server)

    assert response.status_code == 200
    assert server.calls == 1
    assert sleep.waits == []


@pytest.mark.parametrize(
    "code",
    [
        200,
        201,
        204,
        400,  # our request is wrong: retrying sends the same wrong request
        401,  # bad credentials won't fix themselves
        403,
        404,  # player or map doesn't exist
        422,
    ],
)
async def test_permanent_statuses_are_returned_at_once(code: int) -> None:
    server = FakeServer(status(code), status(200))

    response, sleep = await run(server)

    assert response.status_code == code
    assert server.calls == 1, f"{code} should not be retried"
    assert sleep.waits == []


@pytest.mark.parametrize("code", sorted(RETRY_STATUSES))
async def test_temporary_statuses_are_retried(code: int) -> None:
    server = FakeServer(status(code), status(200))

    response, sleep = await run(server)

    assert response.status_code == 200
    assert server.calls == 2
    assert sleep.waits == [1.0]


def test_retry_statuses_are_exactly_the_temporary_ones() -> None:
    assert frozenset({429, 500, 502, 503, 504}) == RETRY_STATUSES


@pytest.mark.parametrize("max_attempts", [1, 2, 4, 6])
async def test_gives_up_after_max_attempts_without_a_final_wait(max_attempts: int) -> None:
    server = FakeServer(*[status(503)] * max_attempts)
    policy = RetryPolicy(max_attempts=max_attempts)

    response, sleep = await run(server, policy)

    assert response.status_code == 503, "the last response goes back to the caller"
    assert server.calls == max_attempts
    # One wait between each pair of attempts, none after the last: the caller would
    # only be waiting to receive the failure.
    want = [backoff_delay(a, policy, lambda: 1.0) for a in range(max_attempts - 1)]
    assert sleep.waits == want


# ---------- send_with_retries: Retry-After ----------


@pytest.mark.parametrize(
    ("header", "want_wait"),
    [
        ("7", 7.0),  # the server's value beats our backoff
        ("0", 0.0),
        ("99999", 30.0),  # capped: never trust a sleep time from the network
        ("inf", 1.0),  # invalid: fall back to backoff
        ("-5", 1.0),
        ("soon", 1.0),
    ],
)
async def test_retry_after_header(header: str, want_wait: float) -> None:
    server = FakeServer(status(429, header), status(200))

    response, sleep = await run(server)

    assert response.status_code == 200
    assert sleep.waits == [want_wait]


async def test_retry_after_on_every_attempt() -> None:
    server = FakeServer(status(429, "3"), status(503), status(429, "5"), status(200))

    _, sleep = await run(server)

    assert sleep.waits == [3.0, 2.0, 5.0]  # header, backoff for attempt 1, header


# ---------- send_with_retries: network errors ----------


NETWORK_ERRORS = [
    httpx.ConnectError("connection refused"),
    httpx.ConnectTimeout("connect timed out"),
    httpx.ReadTimeout("read timed out"),
    httpx.RemoteProtocolError("server closed the connection"),
]


@pytest.mark.parametrize("error", NETWORK_ERRORS, ids=lambda e: type(e).__name__)
async def test_network_error_is_retried_with_backoff(error: Exception) -> None:
    server = FakeServer(error, status(200))

    response, sleep = await run(server)

    assert response.status_code == 200
    assert sleep.waits == [1.0], "network errors must back off too, not retry instantly"


@pytest.mark.parametrize("error", NETWORK_ERRORS, ids=lambda e: type(e).__name__)
async def test_network_error_on_last_attempt_is_raised(error: Exception) -> None:
    server = FakeServer(*[error] * 4)

    with pytest.raises(type(error)):
        await run(server)

    assert server.calls == 4


async def test_mixed_failures_then_success() -> None:
    server = FakeServer(httpx.ConnectError("refused"), status(503), status(200))

    response, sleep = await run(server)

    assert response.status_code == 200
    assert server.calls == 3
    assert sleep.waits == [1.0, 2.0]


@pytest.mark.parametrize(
    "error",
    [
        ValueError("a bug in our own code"),  # must surface at once, not be retried
        httpx.TooManyRedirects("redirect loop"),  # an httpx error that won't fix itself
    ],
    ids=lambda e: type(e).__name__,
)
async def test_other_errors_are_not_retried(error: Exception) -> None:
    server = FakeServer(error, status(200))

    with pytest.raises(type(error)):
        await run(server)

    assert server.calls == 1


# ---------- send_with_retries: waiting ----------


async def test_every_wait_is_within_the_cap() -> None:
    policy = RetryPolicy(max_attempts=10, base_delay_s=5.0, max_delay_s=12.0)
    server = FakeServer(*[status(503)] * 10)

    _, sleep = await run(server, policy)

    assert len(sleep.waits) == 9
    assert all(0 <= w <= 12.0 for w in sleep.waits), sleep.waits
    assert all(math.isfinite(w) for w in sleep.waits)
