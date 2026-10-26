"""Celery app and background tasks."""

import asyncio
from dataclasses import asdict
from typing import Any

import httpx
from celery import Celery
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from osu_coach.config import get_settings
from osu_coach.osu.auth import TokenProvider
from osu_coach.osu.client import OsuClient
from osu_coach.osu.ratelimiter import RateLimiter
from osu_coach.sync import sync_player

settings = get_settings()

celery_app = Celery("osu_coach", broker=settings.redis_url, backend=settings.redis_url)


@celery_app.task(name="osu_sync")
def osu_sync(user: int | str) -> dict[str, Any]:
    """Sync one player (osu! id or username) from osu!. Returns a small JSON summary."""
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
        # Celery sends results through Redis as JSON: a dict works, a dataclass doesn't.
        return asdict(result)
    finally:
        # Close what we opened, even if the sync failed. The error itself still reaches
        # Celery, which marks the task as failed.
        await redis.aclose()
        await engine.dispose()
