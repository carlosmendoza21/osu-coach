"""Sync a player's profile and scores from osu! into the database."""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach.models import Beatmap, Player, Score
from osu_coach.osu.client import OsuClient
from osu_coach.osu.schemas import OsuScore, OsuUser, ScoreBeatmap, ScoreBeatmapset


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


def beatmap_values(
    beatmap: ScoreBeatmap, beatmapset: ScoreBeatmapset, max_combo: int | None = None
) -> dict[str, Any]:
    """The beatmaps row for a map (updated_at is set by the database)."""

    return {
        "id": beatmap.id,
        "beatmapset_id": beatmap.beatmapset_id,
        "artist": beatmapset.artist,
        "title": beatmapset.title,
        "creator": beatmapset.creator,
        "version": beatmap.version,
        "star_rating": beatmap.difficulty_rating,
        "ar": beatmap.ar,
        "od": beatmap.od,
        "cs": beatmap.cs,
        "hp": beatmap.hp,
        "bpm": beatmap.bpm,
        "length_s": beatmap.total_length,
        "hit_length_s": beatmap.hit_length,
        "count_circles": beatmap.count_circles,
        "count_sliders": beatmap.count_sliders,
        "count_spinners": beatmap.count_spinners,
        "max_combo": max_combo,
        "checksum": beatmap.checksum,
        "status": beatmap.status,
        "mode": beatmap.mode,
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


async def upsert_beatmaps(session: AsyncSession, rows: list[dict[str, Any]]) -> int:
    """Insert or update maps in one statement. Returns how many distinct maps there were."""
    # Several scores can be on the same map, and Postgres refuses to update the same row
    # twice in one INSERT ... ON CONFLICT, so keep one row per map.
    unique = list({row["id"]: row for row in rows}.values())
    if not unique:
        return 0
    stmt = insert(Beatmap).values(unique)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Beatmap.id],
        set_={
            **{column: stmt.excluded[column] for column in unique[0] if column != "id"},
            # A score's map data has no max combo. Keep a known value instead of writing
            # NULL over it: the new value if there is one, otherwise the stored one.
            "max_combo": func.coalesce(stmt.excluded.max_combo, Beatmap.max_combo),
            "updated_at": func.now(),
        },
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

    scores = [*best, *recent]
    # Maps and the player first: scores reference both through foreign keys. The maps
    # come from the scores' own data, so this needs no extra requests to osu!.
    await upsert_beatmaps(session, [beatmap_values(s.beatmap, s.beatmapset) for s in scores])
    await upsert_player(session, profile)
    saved = await upsert_scores(session, scores)
    await session.commit()  # all or nothing, maps included

    return SyncResult(osu_id=profile.id, username=profile.username, scores=saved)
