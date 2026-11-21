"""GET /players/{osu_id}/recommendations: practice maps for a player's weakest skill."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach.db import get_session
from osu_coach.models import Beatmap, Player, Recommendation, SkillProfile

router = APIRouter(tags=["recommendations"])


class RecommendedMap(BaseModel):
    """A map to practise, with enough to show it and link to it."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    beatmapset_id: int  # for its cover image and page on osu!
    artist: str
    title: str
    version: str
    creator: str
    star_rating: float
    bpm: float
    ar: float
    od: float
    hit_length_s: int


class RecommendationOut(BaseModel):
    rank: int  # 1 is the best fit
    score: float  # 0-1: how close the map is to what the player needs
    reason: str  # its numbers for the skill, and its level against the player's
    beatmap: RecommendedMap


class RecommendationsOut(BaseModel):
    player_id: int
    target_skill: str | None  # the weakest skill; None until recommendations are made
    comfort_stars: float | None  # the level they're set from
    created_at: datetime | None
    maps: list[RecommendationOut]  # best first; empty until made (or if none fit)


@router.get("/players/{osu_id}/recommendations", response_model=RecommendationsOut)
async def get_recommendations(
    osu_id: int, session: Annotated[AsyncSession, Depends(get_session)]
) -> RecommendationsOut:
    rows = (
        await session.execute(
            select(Recommendation, Beatmap)
            .join(Beatmap, Beatmap.id == Recommendation.beatmap_id)
            .where(Recommendation.player_id == osu_id)
            .order_by(Recommendation.rank)
        )
    ).all()
    if not rows:
        if await session.get(Player, osu_id) is None:
            raise HTTPException(404, f"player {osu_id} not found")
        if await session.get(SkillProfile, osu_id) is None:
            # Made with the skill profile, a little after a player's first sync.
            raise HTTPException(409, "recommendations aren't ready yet")
        return RecommendationsOut(
            player_id=osu_id, target_skill=None, comfort_stars=None, created_at=None, maps=[]
        )
    first = rows[0][0]
    return RecommendationsOut(
        player_id=osu_id,
        target_skill=first.target_skill,
        comfort_stars=first.comfort_stars,
        created_at=first.created_at,
        maps=[
            RecommendationOut(
                rank=r.rank,
                score=r.score,
                reason=r.reason,
                beatmap=RecommendedMap.model_validate(b),
            )
            for r, b in rows
        ],
    )
