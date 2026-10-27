"""Integration tests for osu_coach.osu.client against real osu! and real Redis.

Run with:  uv run pytest -m integration
Needs:     Redis (docker compose up -d redis) and OSU_CLIENT_ID / OSU_CLIENT_SECRET in
           .env. Skips with the reason if either is missing.
Uses its own rate-limit key, and deletes it and the cached token afterwards, even if a
check fails.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
import pytest
from redis.asyncio import Redis

from osu_coach.config import get_settings
from osu_coach.osu.auth import CACHE_KEY, TokenProvider
from osu_coach.osu.client import OsuClient, OsuNotFoundError
from osu_coach.osu.ratelimiter import RateLimiter
from tests.support import load

pytestmark = pytest.mark.integration


@dataclass
class Live:
    osu: OsuClient
    redis: Redis
    rate_key: str
    paths: list[str] = field(default_factory=list)  # every request sent, in order

    def api_requests(self) -> int:
        return sum(1 for p in self.paths if p.startswith("/api/v2/"))


# ---------- Preconditions and fixtures ----------


@pytest.fixture
async def live() -> AsyncIterator[Live]:
    s = get_settings()
    if not s.osu_client_id or not s.osu_client_secret.get_secret_value():
        pytest.skip("OSU_CLIENT_ID / OSU_CLIENT_SECRET not set in .env")
    redis: Redis = Redis.from_url(s.redis_url)
    try:
        await redis.ping()
    except Exception:
        await redis.aclose()
        pytest.skip(f"Redis not reachable at {s.redis_url}. Start it: docker compose up -d redis")

    rate_key = f"test:client:ratelimit:{uuid.uuid4().hex}"
    paths: list[str] = []

    async def record(request: httpx.Request) -> None:
        paths.append(request.url.path)

    async with httpx.AsyncClient(timeout=20, event_hooks={"request": [record]}) as http:
        tokens = TokenProvider(http, redis, s.osu_client_id, s.osu_client_secret)
        osu = OsuClient(http, tokens, RateLimiter(redis, key=rate_key))
        yield Live(osu, redis, rate_key, paths)

    await redis.delete(rate_key, CACHE_KEY)  # runs even if the test failed
    await redis.aclose()


# ---------- Lookups ----------


async def test_user_by_name_and_id_match(live: Live) -> None:
    by_name = await live.osu.get_user("peppy")
    by_id = await live.osu.get_user(2)

    assert by_name == by_id
    assert by_id.id == 2


async def test_best_scores(live: Live) -> None:
    best = await live.osu.get_best_scores(2)
    top5 = await live.osu.get_best_scores(2, limit=5)

    assert len(best) == 50
    pps = [s.pp for s in best if s.pp is not None]
    assert len(pps) == 50, "every best score should have pp"
    assert pps == sorted(pps, reverse=True), "best scores should come best first"
    assert [s.id for s in top5] == [s.id for s in best[:5]]


async def test_recent_scores_of_active_players(live: Live) -> None:
    ranking = await live.osu._get("/rankings/osu/performance")
    ids = [row["user"]["id"] for row in ranking.json()["ranking"][:10]]

    total = sum([len(await live.osu.get_recent_scores(i)) for i in ids])

    # Recent plays expire after 24 h, so any count is fine: what matters is they validate.
    assert total >= 0


@pytest.mark.parametrize(
    "username",
    [
        "no-such-player-osu-coach-test",
        "2",  # all digits: looked up as a username, and nobody is named "2"
    ],
)
async def test_unknown_username_is_not_found(live: Live, username: str) -> None:
    with pytest.raises(OsuNotFoundError):
        await live.osu.get_user(username)


# ---------- Token and rate limit ----------


async def test_recovers_from_a_rejected_cached_token(live: Live) -> None:
    await live.redis.set(CACHE_KEY, "garbage", ex=60)

    user = await live.osu.get_user(2)

    assert user.id == 2
    # Rejected, new token fetched, same request again.
    assert live.paths == ["/api/v2/users/2/osu", "/oauth/token", "/api/v2/users/2/osu"]


async def test_every_request_takes_a_rate_limit_slot(live: Live) -> None:
    await live.osu.get_user(2)
    await live.osu.get_best_scores(2, limit=5)
    await live.osu.get_recent_scores(2, limit=5)

    assert await live.redis.zcard(live.rate_key) == live.api_requests() == 3


# ---------- Beatmaps ----------


async def test_get_beatmap_matches_the_saved_sample(live: Live) -> None:
    saved = load("beatmap.json")

    beatmap = await live.osu.get_beatmap(22423)

    # Ranked maps don't change, so today's response should match the saved fixture.
    assert (beatmap.version, beatmap.max_combo, beatmap.checksum) == (
        saved["version"],
        saved["max_combo"],
        saved["checksum"],
    )
    assert (beatmap.od, beatmap.hp) == (saved["accuracy"], saved["drain"])


async def test_lookup_finds_the_same_map_by_checksum(live: Live) -> None:
    beatmap = await live.osu.get_beatmap(22423)
    assert beatmap.checksum is not None

    found = await live.osu.lookup_beatmap(beatmap.checksum)
    found_upper = await live.osu.lookup_beatmap(beatmap.checksum.upper())

    assert found.id == found_upper.id == 22423


async def test_lookup_for_a_scores_map(live: Live) -> None:
    # The replay flow: something hands us a checksum, and we need the map behind it.
    score = (await live.osu.get_best_scores(2, limit=1))[0]
    full = await live.osu.get_beatmap(score.beatmap.id)
    assert full.checksum is not None

    found = await live.osu.lookup_beatmap(full.checksum)

    assert found.id == score.beatmap.id
    assert (found.od, found.ar, found.cs) == (score.beatmap.od, score.beatmap.ar, score.beatmap.cs)


@pytest.mark.parametrize(
    ("method", "arg"),
    [
        ("get_beatmap", 999999999),
        ("lookup_beatmap", "0" * 32),  # well-formed, but no such map
    ],
)
async def test_unknown_beatmap_is_not_found(live: Live, method: str, arg: object) -> None:
    with pytest.raises(OsuNotFoundError):
        await getattr(live.osu, method)(arg)


async def test_bad_checksum_never_reaches_osu(live: Live) -> None:
    with pytest.raises(ValueError):
        await live.osu.lookup_beatmap("not-a-hash")

    assert live.api_requests() == 0
