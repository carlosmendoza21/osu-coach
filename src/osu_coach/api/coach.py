"""POST /players/{osu_id}/report: a coaching report on a player.

A POST, as the brief has it: a report is written on request (and, with an LLM, costs
something to write), not a stored resource. For now it's built from templates.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach.coach import Report, gather_facts, template_report
from osu_coach.db import get_session
from osu_coach.models import Player

router = APIRouter(tags=["coach"])


@router.post("/players/{osu_id}/report", response_model=Report)
async def post_report(
    osu_id: int, session: Annotated[AsyncSession, Depends(get_session)]
) -> Report:
    facts = await gather_facts(session, osu_id)
    if facts is None:
        if await session.get(Player, osu_id) is None:
            raise HTTPException(404, f"player {osu_id} not found")
        # The report is built on the skill profile, scored a little after a first sync.
        raise HTTPException(409, "this player's skills aren't scored yet")
    return template_report(facts)
