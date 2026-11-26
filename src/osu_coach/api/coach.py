"""POST /players/{osu_id}/report: a coaching report on a player.

A POST, as the brief has it: a report is written on request (and, with an LLM, takes
time and may cost something), not a stored resource. With COACH_LLM set, a model writes
its words (falling back to the template, saying why); without it, it's the template.
"""

from collections.abc import AsyncIterator
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach.coach import Report, gather_facts, template_report
from osu_coach.coach_llm import llm_report
from osu_coach.config import get_settings
from osu_coach.db import get_session
from osu_coach.llm import LLMClient, client_from_setting
from osu_coach.models import Player

router = APIRouter(tags=["coach"])

LLM_TIMEOUT_S = 600  # a local model on a laptop's CPU takes minutes for a report


async def get_llm() -> AsyncIterator[LLMClient | None]:
    """The coach's model from COACH_LLM, or None for template reports."""
    settings = get_settings()
    async with httpx.AsyncClient(timeout=LLM_TIMEOUT_S) as http:
        yield client_from_setting(settings.coach_llm, http, settings.ollama_url)


@router.post("/players/{osu_id}/report", response_model=Report)
async def post_report(
    osu_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    llm: Annotated[LLMClient | None, Depends(get_llm)],
) -> Report:
    facts = await gather_facts(session, osu_id)
    if facts is None:
        if await session.get(Player, osu_id) is None:
            raise HTTPException(404, f"player {osu_id} not found")
        # The report is built on the skill profile, scored a little after a first sync.
        raise HTTPException(409, "this player's skills aren't scored yet")
    if llm is None:
        return template_report(facts)
    return await llm_report(facts, llm)
