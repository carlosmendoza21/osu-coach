"""Celery app and background tasks."""

import asyncio
from dataclasses import asdict
from typing import Any

import httpx
from celery import Celery
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from osu_coach.analysis import MapNotStoredError, ReplayNotFoundError
from osu_coach.analysis import analyze_replay as analyze_one_replay
from osu_coach.attributes import fetch_player_attributes
from osu_coach.beatmaps import UnsupportedModeError, maps_to_fetch
from osu_coach.beatmaps import fetch_beatmap as fetch_one_beatmap
from osu_coach.candidates import collect_candidates as collect_candidate_maps
from osu_coach.config import get_settings
from osu_coach.difficulty import UnsupportedModsError
from osu_coach.osu.auth import TokenProvider
from osu_coach.osu.client import OsuClient, OsuNotFoundError
from osu_coach.osu.files import BeatmapFiles
from osu_coach.osu.ratelimiter import RateLimiter
from osu_coach.replays import MapChangedError
from osu_coach.replays import fetch_replay_map as fetch_one_replay_map
from osu_coach.skills import update_profile
from osu_coach.sync import sync_player
from osu_coach.vectors import build_missing_vectors

settings = get_settings()

celery_app = Celery("osu_coach", broker=settings.redis_url, backend=settings.redis_url)

# Syncs have a queue (and worker) of their own: a player search waits on its sync, so it
# must never queue behind background work like a few hundred map downloads. Every other
# task goes to Celery's default queue.
SYNC_QUEUE = "sync"
celery_app.conf.task_routes = {"osu_sync": {"queue": SYNC_QUEUE}}


# throws=: these errors are expected outcomes (a player or map that doesn't exist, a map
# for another game mode), not crashes. Celery still marks the task as failed and passes
# the error on, but logs one line instead of a full traceback.
@celery_app.task(name="osu_sync", throws=(OsuNotFoundError,))
def osu_sync(user: int | str) -> dict[str, Any]:
    """Sync one player (osu! id or username) from osu!, then queue a fetch_beatmap for
    each of their maps that has no hit objects yet. Returns a small JSON summary."""
    return asyncio.run(_osu_sync(user), loop_factory=asyncio.SelectorEventLoop)


async def _osu_sync(user: int | str) -> dict[str, Any]:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    redis: Redis = Redis.from_url(settings.redis_url)

    try:
        async with (
            httpx.AsyncClient(timeout=20) as http,
            async_sessionmaker(engine)() as session,
        ):
            tokens = TokenProvider(http, redis, settings.osu_client_id, settings.osu_client_secret)
            osu = OsuClient(http, tokens, RateLimiter(redis))
            result = await sync_player(osu, session, user)
            missing = await maps_to_fetch(session, result.osu_id)
        # Each map is its own task: a map that fails (e.g. osu! is down) doesn't fail the
        # sync, the worker can fetch several at once, and the shared rate limiter keeps
        # them all under osu!'s limit. Maps already stored aren't fetched again.
        for beatmap_id in missing:
            fetch_beatmap.delay(beatmap_id)
        # The plays' difficulty (aim, speed...) for the skill profile: one task for the
        # player, which only asks osu! for the pairs not stored yet.
        fetch_attributes.delay(result.osu_id)
        # Celery sends results through Redis as JSON: a dict works, a dataclass doesn't.
        return {**asdict(result), "maps_queued": len(missing)}
    finally:
        # Close what we opened, even if the sync failed. The error itself still reaches
        # Celery, which marks the task as failed.
        await redis.aclose()
        await engine.dispose()


@celery_app.task(name="fetch_beatmap", throws=(OsuNotFoundError, UnsupportedModeError))
def fetch_beatmap(beatmap_id: int) -> dict[str, Any]:
    """Fetch one map: its details, its .osu file, and its hit objects. Returns a summary."""
    return asyncio.run(_fetch_beatmap(beatmap_id), loop_factory=asyncio.SelectorEventLoop)


async def _fetch_beatmap(beatmap_id: int) -> dict[str, Any]:
    # Same setup as _osu_sync: a fresh engine and clients for this run, closed at the end.
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    redis: Redis = Redis.from_url(settings.redis_url)

    try:
        async with (
            httpx.AsyncClient(timeout=20) as http,
            async_sessionmaker(engine)() as session,
        ):
            limiter = RateLimiter(redis)  # one shared limit for API calls and file downloads
            tokens = TokenProvider(http, redis, settings.osu_client_id, settings.osu_client_secret)
            osu = OsuClient(http, tokens, limiter)
            files = BeatmapFiles(http, limiter, settings.data_dir / "beatmaps")
            result = await fetch_one_beatmap(osu, files, session, beatmap_id)
        return asdict(result)
    finally:
        await redis.aclose()
        await engine.dispose()


@celery_app.task(
    name="fetch_replay_map",
    throws=(OsuNotFoundError, UnsupportedModeError, MapChangedError),
)
def fetch_replay_map(checksum: str) -> dict[str, Any]:
    """Make sure the map an uploaded replay was played on is stored, by its file's MD5."""
    return asyncio.run(_fetch_replay_map(checksum), loop_factory=asyncio.SelectorEventLoop)


async def _fetch_replay_map(checksum: str) -> dict[str, Any]:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    redis: Redis = Redis.from_url(settings.redis_url)

    try:
        async with (
            httpx.AsyncClient(timeout=20) as http,
            async_sessionmaker(engine)() as session,
        ):
            limiter = RateLimiter(redis)
            tokens = TokenProvider(http, redis, settings.osu_client_id, settings.osu_client_secret)
            osu = OsuClient(http, tokens, limiter)
            files = BeatmapFiles(http, limiter, settings.data_dir / "beatmaps")
            result = await fetch_one_replay_map(osu, files, session, checksum)
        return asdict(result)
    finally:
        await redis.aclose()
        await engine.dispose()


@celery_app.task(
    name="analyze_replay",
    throws=(ReplayNotFoundError, MapNotStoredError, UnsupportedModsError),
)
def analyze_replay(replay_id: int) -> dict[str, Any]:
    """Judge a stored replay against its map and store the results. Returns a summary."""
    return asyncio.run(_analyze_replay(replay_id), loop_factory=asyncio.SelectorEventLoop)


async def _analyze_replay(replay_id: int) -> dict[str, Any]:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    redis: Redis = Redis.from_url(settings.redis_url)

    try:
        async with (
            httpx.AsyncClient(timeout=20) as http,
            async_sessionmaker(engine)() as session,
        ):
            # Only needed if the map's file is missing: normally it's on disk already.
            files = BeatmapFiles(http, RateLimiter(redis), settings.data_dir / "beatmaps")
            return await analyze_one_replay(session, files, settings.data_dir, replay_id)
    finally:
        await redis.aclose()
        await engine.dispose()


@celery_app.task(name="fetch_attributes")
def fetch_attributes(osu_id: int) -> dict[str, Any]:
    """Fetch osu!'s difficulty attributes for a player's plays that don't have them yet."""
    return asyncio.run(_fetch_attributes(osu_id), loop_factory=asyncio.SelectorEventLoop)


async def _fetch_attributes(osu_id: int) -> dict[str, Any]:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    redis: Redis = Redis.from_url(settings.redis_url)

    try:
        async with (
            httpx.AsyncClient(timeout=20) as http,
            async_sessionmaker(engine)() as session,
        ):
            tokens = TokenProvider(http, redis, settings.osu_client_id, settings.osu_client_secret)
            osu = OsuClient(http, tokens, RateLimiter(redis))
            result = await fetch_player_attributes(osu, session, osu_id)
            # The attributes are what the profile is scored from: score it again now.
            profile = await update_profile(session, osu_id)
        return {
            "osu_id": osu_id,
            **asdict(result),
            "profile": None
            if profile is None
            else {**profile.scores, "plays_used": profile.plays_used},
        }
    finally:
        await redis.aclose()
        await engine.dispose()


@celery_app.task(name="build_map_vectors")
def build_map_vectors() -> dict[str, Any]:
    """Build the pattern vector of every stored map that lacks one (or has an outdated
    one). New maps get theirs from fetch_beatmap; this fills in the rest."""
    return asyncio.run(_build_map_vectors(), loop_factory=asyncio.SelectorEventLoop)


async def _build_map_vectors() -> dict[str, Any]:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine)() as session:
            return {"built": await build_missing_vectors(session)}
    finally:
        await engine.dispose()


@celery_app.task(name="collect_candidates")
def collect_candidates() -> dict[str, Any]:
    """Find well-known ranked maps at every level for the recommender, then queue a
    fetch_beatmap (objects and pattern vector) for each one not stored yet."""
    return asyncio.run(_collect_candidates(), loop_factory=asyncio.SelectorEventLoop)


async def _collect_candidates() -> dict[str, Any]:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    redis: Redis = Redis.from_url(settings.redis_url)

    try:
        async with (
            httpx.AsyncClient(timeout=20) as http,
            async_sessionmaker(engine)() as session,
        ):
            tokens = TokenProvider(http, redis, settings.osu_client_id, settings.osu_client_secret)
            osu = OsuClient(http, tokens, RateLimiter(redis))
            result = await collect_candidate_maps(osu, session)
        # One task per map, as after a sync. They go to the default queue, so the
        # hundreds of them a first run queues never hold up a player's sync.
        for beatmap_id in result.to_fetch:
            fetch_beatmap.delay(beatmap_id)
        return {"maps": result.maps, "maps_queued": len(result.to_fetch)}
    finally:
        await redis.aclose()
        await engine.dispose()
