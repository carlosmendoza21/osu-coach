"""GET /players/{username}: a player's profile and plays, synced from osu! when stale."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from celery.exceptions import TimeoutError as CeleryTimeoutError
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach.db import get_session
from osu_coach.models import Player, Score
from osu_coach.osu.client import OsuApiError, OsuNotFoundError
from osu_coach.worker import osu_sync

router = APIRouter(tags=["players"])

STALE_AFTER = timedelta(minutes=10)  # sync a player from osu! at most this often
SYNC_TIMEOUT_S = 30
BEST_LIMIT = 50
RECENT_LIMIT = 20


# ---------- Response models: what the API sends, separate from the tables ----------


class PlayerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)  # build it straight from a Player row

    osu_id: int
    username: str
    country: str
    global_rank: int | None
    pp: float
    playcount: int
    accuracy: float  # a percent
    updated_at: datetime  # when we last synced this player from osu!


class ScoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    beatmap_id: int
    score: int
    max_combo: int
    count_300: int
    count_100: int
    count_50: int
    count_miss: int
    rank: str
    pp: float | None
    mods: list[str]
    date: datetime
    accuracy: float  # 0-1
    passed: bool


class PlayerResponse(BaseModel):
    player: PlayerOut
    best: list[ScoreOut]
    recent: list[ScoreOut]


# ---------- Database queries ----------


async def find_player(session: AsyncSession, username: str) -> Player | None:
    """The stored player with this username (any case), or None if we don't have them."""
    # osu! usernames aren't case-sensitive. If an old name now belongs to someone else,
    # both may be stored: take the most recently synced.
    stmt = (
        select(Player)
        .where(func.lower(Player.username) == username.lower())
        .order_by(Player.updated_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def best_scores(session: AsyncSession, osu_id: int) -> list[Score]:
    """The player's top plays by pp, highest first."""
    # Plays without pp are left out: ORDER BY pp DESC puts NULLs first in Postgres.
    stmt = (
        select(Score)
        .where(Score.player_id == osu_id, Score.pp.is_not(None))
        .order_by(Score.pp.desc())
        .limit(BEST_LIMIT)
    )
    return list((await session.scalars(stmt)).all())


async def recent_scores(session: AsyncSession, osu_id: int) -> list[Score]:
    """The player's latest plays, failed ones included, newest first."""
    stmt = (
        select(Score)
        .where(Score.player_id == osu_id)
        .order_by(Score.date.desc())
        .limit(RECENT_LIMIT)
    )
    return list((await session.scalars(stmt)).all())


def is_stale(player: Player) -> bool:
    return datetime.now(UTC) - player.updated_at > STALE_AFTER


# ---------- Syncing ----------

SyncFn = Callable[[str], Awaitable[dict[str, Any]]]


async def sync_via_worker(username: str) -> dict[str, Any]:
    """Run osu_sync on the worker and wait for its result."""
    task = osu_sync.delay(username)
    # task.get() blocks while it waits. In a thread, the server keeps answering other
    # requests meanwhile; called directly, it would freeze the whole event loop.
    result: dict[str, Any] = await asyncio.to_thread(task.get, timeout=SYNC_TIMEOUT_S)
    return result


def get_sync() -> SyncFn:
    """FastAPI dependency: how to sync a player. Tests override it with a fake."""
    return sync_via_worker


# ---------- Endpoint ----------


@router.get("/players/{username}", response_model=PlayerResponse)
async def get_player(
    username: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    sync: Annotated[SyncFn, Depends(get_sync)],
) -> PlayerResponse:
    player = await find_player(session, username)

    if player is None or is_stale(player):
        try:
            result = await sync(username)
        except OsuNotFoundError:
            raise HTTPException(404, f"osu! player '{username}' not found") from None
        except (OsuApiError, CeleryTimeoutError) as error:
            # osu! failed or took too long. Stale data beats an error page; with no
            # data at all, say which kind of failure it was.
            if player is None:
                if isinstance(error, CeleryTimeoutError):
                    raise HTTPException(504, "syncing from osu! timed out") from None
                raise HTTPException(502, "osu! request failed") from None
        else:
            # By id: the worker resolved the name (renames, other letter case).
            # populate_existing: replace the copy this session loaded before the sync.
            player = await session.get(Player, result["osu_id"], populate_existing=True)
            if player is None:  # the sync saved it, so this means something is wrong
                raise HTTPException(500, "player missing after sync")

    best = await best_scores(session, player.osu_id)
    recent = await recent_scores(session, player.osu_id)
    return PlayerResponse(
        player=PlayerOut.model_validate(player),
        best=[ScoreOut.model_validate(s) for s in best],
        recent=[ScoreOut.model_validate(s) for s in recent],
    )
