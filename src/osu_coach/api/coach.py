"""POST /players/{osu_id}/report: a coaching report on a player.

A POST, as the brief has it: a report is written on request (and, with an LLM, takes
time and may cost something), not a stored resource. With COACH_LLM set, a model writes
its words (falling back to the template, saying why); without it, it's the template.

Two ways to get it:
- JSON (the default): the report, once written.
- A stream (`Accept: text/event-stream`, server-sent events), for a page to show the
  coach working, since a report from a local model takes minutes. Each event is one
  `data:` line of JSON:
    {"type": "step", "text": "Looking up your skills"}   what it's doing, as it goes
    {"type": "report", "report": {...}}                  the report: the last event
    {"type": "error", "detail": "..."}                   if it failed unexpectedly
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach.coach import CoachFacts, Report, gather_facts, template_report
from osu_coach.coach_llm import OnStep, llm_report
from osu_coach.config import get_settings
from osu_coach.db import get_session
from osu_coach.llm import LLMClient, client_from_setting
from osu_coach.models import Player

router = APIRouter(tags=["coach"])

LLM_TIMEOUT_S = 600  # a local model on a laptop's CPU takes minutes for a report

# Makes the coach's model (or None for template reports) on an HTTP client it's given.
# A factory rather than a client: a stream outlives the request's dependencies, so it
# opens its own HTTP client for the model while it runs.
LLMFactory = Callable[[httpx.AsyncClient], LLMClient | None]


def get_llm_factory() -> LLMFactory:
    """The model COACH_LLM names. Tests replace this with a fake."""
    settings = get_settings()
    return lambda http: client_from_setting(settings.coach_llm, http, settings.ollama_url)


async def write_report(
    facts: CoachFacts, llm: LLMClient | None, on_step: OnStep | None = None
) -> Report:
    """The model's report, or the template's when there's no model."""
    if llm is None:
        return template_report(facts)
    if on_step is None:
        return await llm_report(facts, llm)
    return await llm_report(facts, llm, on_step)


def event(data: dict[str, Any]) -> str:
    """One server-sent event: a data line of JSON, then a blank line."""
    return f"data: {json.dumps(data)}\n\n"


async def report_events(facts: CoachFacts, make_llm: LLMFactory) -> AsyncIterator[str]:
    """The coach's steps as it works, then the report. Run as the response is sent."""
    steps: asyncio.Queue[str] = asyncio.Queue()
    async with httpx.AsyncClient(timeout=LLM_TIMEOUT_S) as http:
        task = asyncio.create_task(write_report(facts, make_llm(http), steps.put))
        try:
            # Steps as they come, until the report is done.
            while not task.done() or not steps.empty():
                next_step = asyncio.create_task(steps.get())
                await asyncio.wait({next_step, task}, return_when=asyncio.FIRST_COMPLETED)
                if next_step.done():
                    yield event({"type": "step", "text": next_step.result()})
                else:
                    next_step.cancel()
            try:
                report = task.result()
            except Exception as e:  # anything unexpected: say so, rather than cut off
                yield event({"type": "error", "detail": f"the report couldn't be written ({e!r})"})
                return
            yield event({"type": "report", "report": report.model_dump(mode="json")})
        finally:
            # The page was closed mid-report: stop the model's work too.
            task.cancel()


@router.post("/players/{osu_id}/report", response_model=Report)
async def post_report(
    osu_id: int,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    make_llm: Annotated[LLMFactory, Depends(get_llm_factory)],
) -> Report | StreamingResponse:
    # Everything from the database first: a stream runs after the session is gone.
    facts = await gather_facts(session, osu_id)
    if facts is None:
        if await session.get(Player, osu_id) is None:
            raise HTTPException(404, f"player {osu_id} not found")
        # The report is built on the skill profile, scored a little after a first sync.
        raise HTTPException(409, "this player's skills aren't scored yet")
    if "text/event-stream" in request.headers.get("accept", ""):
        return StreamingResponse(
            report_events(facts, make_llm),
            media_type="text/event-stream",
            # No caching, and no buffering by a proxy in front: each step as it happens.
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    async with httpx.AsyncClient(timeout=LLM_TIMEOUT_S) as http:
        return await write_report(facts, make_llm(http))
