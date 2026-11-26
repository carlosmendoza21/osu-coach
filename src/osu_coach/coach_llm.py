"""The coaching report written by an LLM, on the facts the template report is built from.

Code owns the facts; the model writes the words:
- Code decides everything checkable: which skills are strengths and weaknesses, their
  order, every score and number, the evidence and the practice maps. That's the template
  report (osu_coach.coach), already correct.
- The model writes the summary, a short explanation of each weakness and the plan's
  steps. It looks up what it wants through tools over the same facts, then answers in a
  fixed JSON shape.
- A check: every number in the model's words must be one in the facts. Anything else
  (a made-up number, the wrong shape, a model that can't be reached) and the template
  report is sent instead, saying why.

This keeps a small local model dependable, and makes every report checkable.
"""

import json
import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from osu_coach.coach import (
    SKILL_NAMES,
    CoachFacts,
    Report,
    play_evidence,
    replay_evidence,
    template_report,
)
from osu_coach.llm import LLMClient, LLMError, Message, Tool, tool_result

MAX_TOOL_ROUNDS = 4  # turns the model may spend looking things up before writing
SMALL_NUMBERS = 10  # whole numbers up to this ("3 weaknesses", "2 replays") needn't be facts
NUMBER_TOLERANCE = 0.051  # "38.3" for 38.3, and rounding of a fact by the last digit

SYSTEM = """You are a coach for osu! (the rhythm game), writing a report for one player.
Use the tools to look up their skills, replays, misses and practice maps.
Rules:
- Every number you write must come from a tool result, exactly as given. Never invent,
  estimate or convert numbers.
- Be specific and encouraging; speak to the player as "you".
- Keep each part short: a sentence or two."""


# ---------- Tools over the facts ----------


def skill_profile(facts: CoachFacts) -> dict[str, Any]:
    return {
        "player": facts.username,
        "scores_0_to_100": facts.scores,
        "evidence": {
            s: play_evidence(s, facts.plays) + replay_evidence(s, facts.replays)
            for s in facts.scores
        },
    }


def replay_analysis(facts: CoachFacts) -> dict[str, Any]:
    r = facts.replays
    if r is None:
        return {"replays": 0, "note": "No uploaded replays for this player."}
    return {
        "replays": r.count,
        "mean_unstable_rate": r.mean_ur,
        "notes_by_pattern": r.notes,
    }


def miss_patterns(facts: CoachFacts) -> dict[str, Any]:
    r = facts.replays
    if r is None:
        return {"note": "No uploaded replays, so no misses by pattern."}
    ranked = sorted(r.miss_rates, key=lambda p: -r.miss_rates[p])
    return {
        "miss_rate_by_pattern": {p: f"{r.miss_rates[p]:.1%}" for p in ranked},
        "notes_by_pattern": {p: r.notes[p] for p in ranked},
    }


def practice_maps(facts: CoachFacts) -> dict[str, Any]:
    if not facts.maps:
        return {"note": "No practice maps picked yet."}
    return {
        "maps": [{"name": m.name, "stars": m.star_rating, "reason": m.reason} for m in facts.maps]
    }


TOOLS: dict[str, tuple[Tool, Callable[[CoachFacts], dict[str, Any]]]] = {
    "get_skill_profile": (
        Tool(
            "get_skill_profile",
            "The player's six skills (0-100) and evidence for each, with numbers.",
        ),
        skill_profile,
    ),
    "get_replay_analysis": (
        Tool(
            "get_replay_analysis",
            "The player's uploaded replays: how many, their timing (UR) and notes per pattern.",
        ),
        replay_analysis,
    ),
    "get_miss_patterns": (
        Tool(
            "get_miss_patterns",
            "Where the player misses in their uploaded replays: miss rate by pattern.",
        ),
        miss_patterns,
    ),
    "search_beatmaps": (
        Tool(
            "search_beatmaps",
            "Practice maps picked for the player's weakest skill, with why each was picked.",
        ),
        practice_maps,
    ),
}


def run_tool(name: str, facts: CoachFacts) -> str:
    if name not in TOOLS:
        return json.dumps({"error": f"no tool named {name}"})
    return json.dumps(TOOLS[name][1](facts))


# ---------- The words the model writes ----------


class WrittenWeakness(BaseModel):
    skill: str
    explanation: str


class WrittenStep(BaseModel):
    skill: str
    text: str


class Written(BaseModel):
    summary: str
    weaknesses: list[WrittenWeakness]
    plan: list[WrittenStep]


def writing_schema(skills: list[str]) -> dict[str, Any]:
    """The JSON the model must answer with: one explanation and one plan step for each
    of these skills (the weaknesses, as code ranked them)."""
    item = {"type": "string", "enum": skills}
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "weaknesses": {
                "type": "array",
                "minItems": len(skills),
                "maxItems": len(skills),
                "items": {
                    "type": "object",
                    "properties": {"skill": item, "explanation": {"type": "string"}},
                    "required": ["skill", "explanation"],
                },
            },
            "plan": {
                "type": "array",
                "minItems": len(skills),
                "maxItems": len(skills),
                "items": {
                    "type": "object",
                    "properties": {"skill": item, "text": {"type": "string"}},
                    "required": ["skill", "text"],
                },
            },
        },
        "required": ["summary", "weaknesses", "plan"],
    }


def writing_prompt(report: Report) -> str:
    weak = ", ".join(f"{SKILL_NAMES[w.skill]} ({w.score:g}/100)" for w in report.weaknesses)
    strong = ", ".join(f"{SKILL_NAMES[s.skill]} ({s.score:g}/100)" for s in report.strengths)
    return (
        f"Now write {report.username}'s report as JSON. Their strengths: {strong}. Their "
        f"weaknesses, weakest first: {weak}.\n"
        "- summary: one or two sentences on where they stand.\n"
        "- weaknesses: for each weakness, an explanation of what the evidence shows.\n"
        "- plan: for each weakness, weakest first, one practice step; the first step "
        "should use the practice maps if there are any.\n"
        "Only use numbers from the tool results."
    )


# ---------- The number check ----------

NUMBER = re.compile(r"\d+(?:\.\d+)?")


def numbers_in(text: str) -> list[float]:
    return [float(n) for n in NUMBER.findall(text.replace(",", ""))]


def fact_numbers(report: Report) -> set[float]:
    """Every number the template report states: scores, evidence, plan, maps."""
    texts = [report.summary, *(s.text for s in report.plan)]
    for note in report.strengths + report.weaknesses:
        texts += [str(note.score), f"{note.score:.0f}", *note.evidence]
    for m in report.maps:
        texts += [m.name, str(m.star_rating), m.reason]
    return {n for t in texts for n in numbers_in(t)} | {100.0}


def tool_numbers(facts: CoachFacts) -> set[float]:
    """Every number any tool can tell the model (raw, like a UR of 85.25, where the
    evidence says 85.2): the model may quote what a tool gave it."""
    return {n for name in TOOLS for n in numbers_in(run_tool(name, facts))}


def unknown_numbers(written: Written, allowed: set[float]) -> list[float]:
    """Numbers in the model's words that aren't facts. Small counts written as whole
    numbers ("3 weaknesses") are fine; a number with a decimal ("AR 8.0") reads as a
    measurement, so it must be a fact."""
    texts = [
        written.summary,
        *(w.explanation for w in written.weaknesses),
        *(s.text for s in written.plan),
    ]
    unknown: list[float] = []
    for token in (n for t in texts for n in NUMBER.findall(t.replace(",", ""))):
        if "." not in token and int(token) <= SMALL_NUMBERS:
            continue
        n = float(token)
        if not any(abs(n - a) <= NUMBER_TOLERANCE for a in allowed):
            unknown.append(n)
    return unknown


# ---------- The report ----------


class Rejected(Exception):
    """The model's report can't be used; the message says why."""


def merge(report: Report, written: Written, allowed: set[float] | None = None) -> Report:
    """The template report with the model's words in place of its own. `allowed`: the
    numbers the words may use (default: the report's own)."""
    skills = [w.skill for w in report.weaknesses]
    explained = {w.skill: w.explanation for w in written.weaknesses}
    steps = {s.skill: s.text for s in written.plan}
    missing = [s for s in skills if s not in explained or s not in steps]
    if missing:
        raise Rejected(f"no words for {', '.join(missing)}")
    unknown = unknown_numbers(written, fact_numbers(report) if allowed is None else allowed)
    if unknown:
        raise Rejected(f"numbers not in the facts: {', '.join(f'{n:g}' for n in unknown)}")
    return report.model_copy(
        update={
            "source": "llm",
            "summary": written.summary,
            "weaknesses": [
                w.model_copy(update={"explanation": explained[w.skill]}) for w in report.weaknesses
            ],
            "plan": [p.model_copy(update={"text": steps[p.skill]}) for p in report.plan],
        }
    )


async def llm_report(facts: CoachFacts, client: LLMClient) -> Report:
    """The model's report, or the template one (with the reason) if it can't be used."""
    template = template_report(facts)
    tools = [tool for tool, _ in TOOLS.values()]
    messages: list[Message] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"Coach {facts.username}. Look up what you need first."},
    ]
    used: list[str] = []
    try:
        # Looking things up: the model calls tools until it has what it wants.
        for _ in range(MAX_TOOL_ROUNDS):
            reply = await client.chat(messages, tools=tools)
            if not reply.tool_calls:
                break
            messages.append(reply.message)
            for call in reply.tool_calls:
                used.append(call.name)
                messages.append(tool_result(call, run_tool(call.name, facts)))
        # Writing: the report's words, in a fixed shape.
        skills = [w.skill for w in template.weaknesses]
        messages.append({"role": "user", "content": writing_prompt(template)})
        reply = await client.chat(messages, schema=writing_schema(skills))
        written = Written.model_validate_json(reply.text)
        report = merge(template, written, fact_numbers(template) | tool_numbers(facts))
    except LLMError as e:
        return fallback(template, client, used, f"the model couldn't be reached ({e})")
    except ValidationError:
        return fallback(template, client, used, "the model's answer wasn't in the report's shape")
    except Rejected as e:
        return fallback(template, client, used, str(e))
    return report.model_copy(update={"model": client.name, "tools_used": used})


def fallback(template: Report, client: LLMClient, used: list[str], reason: str) -> Report:
    return template.model_copy(
        update={"model": client.name, "tools_used": used, "fallback_reason": reason}
    )
