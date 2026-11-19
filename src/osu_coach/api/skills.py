"""GET /players/{osu_id}/skills: a player's six skills, 0-100, as last scored."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach.db import get_session
from osu_coach.models import Player, SkillProfile

router = APIRouter(tags=["skills"])


class SkillsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    player_id: int
    aim: float  # each 0-100
    speed: float
    accuracy: float
    consistency: float
    stamina: float
    reading: float
    strongest: str  # a skill's name, e.g. "aim"
    weakest: str
    plays_used: int  # the plays it was scored from
    plays_skipped: int  # plays without attributes yet, or with Relax and the like
    computed_at: datetime


@router.get("/players/{osu_id}/skills", response_model=SkillsOut)
async def get_skills(
    osu_id: int, session: Annotated[AsyncSession, Depends(get_session)]
) -> SkillProfile:
    profile = await session.get(SkillProfile, osu_id)
    if profile is not None:
        return profile
    if await session.get(Player, osu_id) is None:
        raise HTTPException(404, f"player {osu_id} not found")
    # Scored once their maps' attributes are fetched, a little after their first sync.
    raise HTTPException(409, "skill profile isn't ready yet")
