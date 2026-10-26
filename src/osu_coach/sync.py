"""Sync a player's profile and scores from osu! into the database."""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach.models import Player, Score
from osu_coach.osu.client import OsuClient
from osu_coach.osu.schemas import OsuScore, OsuUser


@dataclass(frozen=True)
class SyncResult:
    osu_id: int
    username: str
    scores: int  # how many distinct scores were saved


def player_values(user: OsuUser) -> dict[str, Any]:
    """The players row for an osu! user (updated_at is set by the database)."""
    return {
        "osu_id": user.id,
        "username": user.username,
        "country": user.country_code,
        "global_rank": user.statistics.global_rank,
        "pp": user.statistics.pp,
        "playcount": user.statistics.play_count,
        "accuracy": user.statistics.hit_accuracy,
    }


def score_values(score: OsuScore) -> dict[str, Any]:
    """The scores row for an osu! score."""
    return {
        "id": score.id,
        "player_id": score.user_id,
        "beatmap_id": score.beatmap.id,
        "score": score.score,
        "max_combo": score.max_combo,
        "count_300": score.statistics.count_300,
        "count_100": score.statistics.count_100,
        "count_50": score.statistics.count_50,
        "count_miss": score.statistics.count_miss,
        "rank": score.rank,
        "pp": score.pp,
        "mods": score.mods,
        "date": score.created_at,
        "accuracy": score.accuracy,
        "passed": score.passed,
    }


async def upsert_player(session: AsyncSession, user: OsuUser) -> None:
    """Insert the player, or update every column if they already exist."""
    values = player_values(user)
    stmt = insert(Player).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Player.osu_id],
        # EXCLUDED = the row we tried to insert. updated_at must be set by hand: the
        # model's onupdate only fires for UPDATE statements, not INSERT ... ON CONFLICT.
        set_={
            **{column: stmt.excluded[column] for column in values if column != "osu_id"},
            "updated_at": func.now(),
        },
    )
    await session.execute(stmt)


async def upsert_scores(session: AsyncSession, scores: list[OsuScore]) -> int:
    """Insert or update scores in one statement. Returns how many distinct scores there were."""
    # A play can be in both the best and the recent list. Postgres refuses to update the
    # same row twice in one INSERT ... ON CONFLICT, so keep one copy per id.
    unique = list({score.id: score for score in scores}.values())
    if not unique:
        return 0
    rows = [score_values(score) for score in unique]
    stmt = insert(Score).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Score.id],
        set_={column: stmt.excluded[column] for column in rows[0] if column != "id"},
    )
    await session.execute(stmt)
    return len(unique)


async def sync_player(
    osu: OsuClient,
    session: AsyncSession,
    user: int | str,
    best_limit: int = 50,
    recent_limit: int = 50,
) -> SyncResult:
    """Fetch a player and their best and recent scores, and save them in one transaction."""
    # Fetch everything before writing: an osu! error then leaves the database untouched,
    # and the transaction stays short.
    profile = await osu.get_user(user)
    best = await osu.get_best_scores(profile.id, best_limit)
    recent = await osu.get_recent_scores(profile.id, recent_limit)

    # The player first: scores reference them through the foreign key.
    await upsert_player(session, profile)
    saved = await upsert_scores(session, [*best, *recent])
    await session.commit()  # all or nothing

    return SyncResult(osu_id=profile.id, username=profile.username, scores=saved)
