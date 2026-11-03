"""Celery app and background tasks."""

import asyncio
from dataclasses import asdict
from typing import Any

import httpx
from celery import Celery
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from osu_coach.beatmaps import UnsupportedModeError, maps_to_fetch
from osu_coach.beatmaps import fetch_beatmap as fetch_one_beatmap
from osu_coach.config import get_settings
from osu_coach.osu.auth import TokenProvider
from osu_coach.osu.client import OsuClient, OsuNotFoundError
from osu_coach.osu.files import BeatmapFiles
from osu_coach.osu.ratelimiter import RateLimiter
from osu_coach.sync import sync_player

settings = get_settings()

celery_app = Celery("osu_coach", broker=settings.redis_url, backend=settings.redis_url)


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
