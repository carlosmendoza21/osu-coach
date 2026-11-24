"""The coaching report: a player's strengths, their three weakest skills with evidence, a
practice plan, and practice maps with reasons.

Two parts:
1. Facts: everything the report may say, gathered from the database (the skill profile,
   the plays it was scored from, uploaded replays, the practice maps). Every number in
   a report comes from here.
2. The report: built from the facts. This module builds it from templates; an LLM will
   later write the same structure from the same facts (and fall back to this). The
   template report is also what the evals check an LLM's numbers against.

Replays are matched to the player by name: a replay records only the name it was
played under.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from statistics import mean
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach.analysis import results_by_pattern
from osu_coach.models import Beatmap, Player, Recommendation, Replay
from osu_coach.models import SkillProfile as SkillProfileRow
from osu_coach.skills import SKILLS, Play, load_plays

SKILL_NAMES = {
    "aim": "Aim",
    "speed": "Speed",
    "accuracy": "Accuracy",
    "consistency": "Consistency",
    "stamina": "Stamina",
    "reading": "Reading",
}
TOP_PLAYS = 10  # "your top plays" in the evidence: the best this many by each measure
STRENGTHS = 2
WEAKNESSES = 3


# ---------- The report (also the shape an LLM will fill in) ----------


class SkillNote(BaseModel):
    skill: str
    score: float  # 0-100, from the skill profile
    evidence: list[str]  # sentences with real numbers


class PlanStep(BaseModel):
    skill: str  # the weakness this step works on
    text: str


class ReportMap(BaseModel):
    beatmap_id: int
    name: str  # "Artist - Title [Difficulty]"
    star_rating: float
    reason: str


class Report(BaseModel):
    player_id: int
    username: str
    source: Literal["template", "llm"]
    summary: str
    strengths: list[SkillNote]
    weaknesses: list[SkillNote]  # weakest first
    plan: list[PlanStep]
    maps: list[ReportMap]


# ---------- Facts ----------


@dataclass(frozen=True)
class ReplayFacts:
    count: int  # analyzed replays under the player's name
    mean_ur: float | None  # unstable rate, lower is steadier
    miss_rates: dict[str, float]  # pattern -> share of its notes missed, over all replays
    notes: dict[str, int]  # pattern -> notes judged


@dataclass(frozen=True)
class CoachFacts:
    player_id: int
    username: str
    scores: dict[str, float]  # skill -> 0-100
    plays: list[Play]
    replays: ReplayFacts | None
    maps: list[ReportMap] = field(default_factory=list)


async def replay_facts(session: AsyncSession, username: str) -> ReplayFacts | None:
    """The player's analyzed replays, matched by name (any case). None if there are none."""
    replays = (
        await session.scalars(
            select(Replay).where(
                func.lower(Replay.player_name) == username.lower(),
                Replay.analyzed_at.is_not(None),
            )
        )
    ).all()
    if not replays:
        return None
    totals: dict[str, dict[str, int]] = {}
    for r in replays:
        for pattern, counts in (await results_by_pattern(session, r.id)).items():
            into = totals.setdefault(pattern, {"300": 0, "100": 0, "50": 0, "miss": 0})
            for result, n in counts.items():
                into[result] += n
    notes = {p: sum(c.values()) for p, c in totals.items()}
    urs = [r.unstable_rate for r in replays if r.unstable_rate is not None]
    return ReplayFacts(
        count=len(replays),
        mean_ur=round(mean(urs), 1) if urs else None,
        miss_rates={p: c["miss"] / notes[p] for p, c in totals.items() if notes[p]},
        notes=notes,
    )


async def report_maps(session: AsyncSession, osu_id: int) -> list[ReportMap]:
    rows = await session.execute(
        select(Recommendation, Beatmap)
        .join(Beatmap, Beatmap.id == Recommendation.beatmap_id)
        .where(Recommendation.player_id == osu_id)
        .order_by(Recommendation.rank)
    )
    return [
        ReportMap(
            beatmap_id=b.id,
            name=f"{b.artist} - {b.title} [{b.version}]",
            star_rating=round(b.star_rating, 2),
            reason=r.reason,
        )
        for r, b in rows.all()
    ]


async def gather_facts(session: AsyncSession, osu_id: int) -> CoachFacts | None:
    """Everything a report on this player may say. None for a player we don't have or
    haven't scored yet."""
    player = await session.get(Player, osu_id)
    profile = await session.get(SkillProfileRow, osu_id)
    if player is None or profile is None:
        return None
    plays, _ = await load_plays(session, osu_id)
    return CoachFacts(
        player_id=osu_id,
        username=player.username,
        scores={s: getattr(profile, s) for s in SKILLS},
        plays=plays,
        replays=await replay_facts(session, player.username),
        maps=await report_maps(session, osu_id),
    )


# ---------- Evidence: what the numbers say about each skill ----------


def top(plays: Sequence[Play], key: str, n: int = TOP_PLAYS) -> list[Play]:
    return sorted(plays, key=lambda p: getattr(p, key), reverse=True)[:n]


def pct(x: float) -> str:
    return f"{x:.0%}"


def play_evidence(skill: str, plays: Sequence[Play]) -> list[str]:
    """Sentences, with numbers, about one skill from the player's plays."""
    if not plays:
        return []
    misses = mean(p.count_miss for p in plays)
    match skill:
        case "aim":
            best = top(plays, "aim_difficulty")
            aim = mean(p.aim_difficulty for p in best)
            return [
                f"Your {len(best)} hardest-aim passes average {aim:.2f} aim difficulty "
                f"(the hardest: {best[0].aim_difficulty:.2f}).",
                f"Those plays average {mean(p.count_miss for p in best):.1f} misses.",
            ]
        case "speed":
            best = top(plays, "speed_difficulty")
            speed = mean(p.speed_difficulty for p in best)
            return [
                f"Your {len(best)} fastest passes average {speed:.2f} speed difficulty, "
                f"at {mean(p.accuracy for p in best):.1%} accuracy.",
            ]
        case "accuracy":
            best = top(plays, "accuracy")
            return [
                f"Your average accuracy is {mean(p.accuracy for p in plays):.1%} over "
                f"{len(plays)} plays (best {best[0].accuracy:.2%}).",
                f"The highest OD you've passed is {max(p.od for p in plays):.1f}.",
            ]
        case "consistency":
            held = [min(1.0, p.max_combo / p.map_max_combo) for p in plays if p.map_max_combo]
            full = sum(p.count_miss == 0 for p in plays)
            return [
                f"You hold {pct(mean(held)) if held else 'n/a'} of a map's combo on average, "
                f"with {misses:.1f} misses per play.",
                f"{full} of your {len(plays)} plays have no misses.",
            ]
        case "stamina":
            clean = [p for p in plays if p.count_miss <= 3]
            most = max((p.speed_note_count for p in clean), default=0.0)
            return [
                f"The most hard-to-tap notes you've cleared in one play (3 misses or fewer) "
                f"is {most:.0f}.",
            ]
        case "reading":
            hd = sum("HD" in p.mods for p in plays)
            return [
                f"The highest AR you've passed is {max(p.ar for p in plays):.1f}; your plays "
                f"average AR {mean(p.ar for p in plays):.1f}.",
                f"{hd} of your {len(plays)} plays use Hidden.",
            ]
    raise ValueError(f"unknown skill: {skill}")


# Which pattern's misses in replays speak to which skill.
REPLAY_PATTERN = {"aim": "jump", "speed": "stream"}


def replay_evidence(skill: str, replays: ReplayFacts | None) -> list[str]:
    """What uploaded replays add, each to the skill it's about: timing (UR) to accuracy,
    misses on jumps to aim and on streams to speed, all misses to consistency."""
    if replays is None:
        return []
    n = replays.count
    if skill == "accuracy" and replays.mean_ur is not None:
        return [
            f"Your {n} uploaded replay(s) average {replays.mean_ur:.1f} UR "
            "(unstable rate: lower is steadier timing)."
        ]
    if skill in REPLAY_PATTERN and REPLAY_PATTERN[skill] in replays.notes:
        pattern = REPLAY_PATTERN[skill]
        return [
            f"In your {n} uploaded replay(s) you miss {replays.miss_rates[pattern]:.1%} of "
            f"{replays.notes[pattern]} {pattern} notes."
        ]
    if skill == "consistency" and replays.notes:
        total = sum(replays.notes.values())
        missed = sum(replays.miss_rates[p] * replays.notes[p] for p in replays.notes)
        return [
            f"In your {n} uploaded replay(s) you miss {round(missed)} of {total} notes "
            f"({missed / total:.1%})."
        ]
    return []


# ---------- The template report ----------

# The first step: the weakest skill, with the practice maps (picked for it).
PLAN = {
    "aim": "Play the practice maps below for their jumps: aim for clean hits on the wide "
    "ones rather than accuracy, and replay a section you miss until it's clean.",
    "speed": "Play the practice maps below for their streams: keep tapping evenly through "
    "each one, even if your accuracy drops at first.",
    "stamina": "Play the practice maps below for their long streams, and try to hold your "
    "tapping to the end of each before worrying about accuracy.",
    "accuracy": "Play the practice maps below at your level and go for accuracy, not "
    "score: watch your hit timing and keep it centred.",
    "consistency": "Play the practice maps below aiming for full combos rather than "
    "score: finish every attempt instead of restarting at the first miss.",
    "reading": "Play the practice maps below for their high approach rates, and once "
    "they feel comfortable, try them with Hidden.",
}


# Later steps, and the first when there are no practice maps yet: without the maps.
PLAN_LATER = {
    "aim": "pick jump-heavy maps a little above your level and play them for clean hits "
    "on the wide jumps.",
    "speed": "pick stream maps a little above your level and keep your tapping even "
    "through each stream.",
    "stamina": "pick maps with long streams and hold your tapping to the end of each one.",
    "accuracy": "replay maps at your level going for accuracy rather than score.",
    "consistency": "go for full combos on maps at your level, finishing every attempt.",
    "reading": "play maps with a higher approach rate than you're used to, then try "
    "familiar ones with Hidden.",
}


def ranked(scores: dict[str, float]) -> list[str]:
    """Skills best first (ties in SKILLS order)."""
    return sorted(SKILLS, key=lambda s: -scores[s])


def note(skill: str, facts: CoachFacts) -> SkillNote:
    return SkillNote(
        skill=skill,
        score=facts.scores[skill],
        evidence=play_evidence(skill, facts.plays) + replay_evidence(skill, facts.replays),
    )


def template_report(facts: CoachFacts) -> Report:
    order = ranked(facts.scores)
    strengths = [note(s, facts) for s in order[:STRENGTHS]]
    weaknesses = [note(s, facts) for s in reversed(order[-WEAKNESSES:])]
    first = weaknesses[0].skill
    plan = [
        PlanStep(
            skill=first,
            text=PLAN[first]
            if facts.maps
            else PLAN_LATER[first][0].upper() + PLAN_LATER[first][1:],
        )
    ] + [
        PlanStep(
            skill=w.skill,
            text=f"Then {SKILL_NAMES[w.skill].lower()} ({w.score:.0f}/100): {PLAN_LATER[w.skill]}",
        )
        for w in weaknesses[1:]
    ]
    best, worst = strengths[0], weaknesses[0]
    summary = (
        f"{facts.username}'s strongest skill is {SKILL_NAMES[best.skill].lower()} "
        f"({best.score:.0f}/100) and the weakest is {SKILL_NAMES[worst.skill].lower()} "
        f"({worst.score:.0f}/100), from {len(facts.plays)} plays"
        + (f" and {facts.replays.count} uploaded replay(s)" if facts.replays else "")
        + "."
    )
    return Report(
        player_id=facts.player_id,
        username=facts.username,
        source="template",
        summary=summary,
        strengths=strengths,
        weaknesses=weaknesses,
        plan=plan,
        maps=facts.maps,
    )
