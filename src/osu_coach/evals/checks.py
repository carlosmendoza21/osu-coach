"""The automatic checks: what can be judged without a model, on a case and its report.

- Structure: the report's facts are the profile's (the three weakest skills, weakest
  first; the two strongest; a plan step per weakness; the case's maps).
- Numbers: every number in a model's words is a fact or a tool's value.
- Practice maps: some were made, for the weakest skill, one per mapset, none played,
  in the star range the recommender allows, each with some of what the skill needs.

Each check passes or fails, with a detail saying why. Some also measure something (how
many maps sit in the first, unwidened star range) as a value.
"""

from dataclasses import dataclass, field

from osu_coach.coach import Report, template_report
from osu_coach.coach_llm import Written, fact_numbers, tool_numbers, unknown_numbers
from osu_coach.evals.cases import Case, PracticeMap, load_facts
from osu_coach.models import Beatmap
from osu_coach.recommender import NEEDS, STEP_UP, Pick, star_range
from osu_coach.skills import SKILLS
from osu_coach.vectors import FIELDS, PatternVector


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str = ""
    value: float | None = None  # a measurement, where the check has one (0-1)


@dataclass(frozen=True)
class CaseResult:
    case: str
    source: str  # "llm" or "template": who wrote the report's words
    fallback_reason: str | None
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)


def ranked(scores: dict[str, float]) -> list[str]:
    """Skills best first (ties in SKILLS order), as the report ranks them."""
    return sorted(SKILLS, key=lambda s: -scores[s])


# ---------- Structure ----------


def check_structure(case: Case, report: Report) -> list[Check]:
    order = ranked(case.facts.scores)
    weakest = list(reversed(order[-3:]))
    weak = [w.skill for w in report.weaknesses]
    strong = [s.skill for s in report.strengths]
    scores_ok = all(
        n.score == case.facts.scores[n.skill] for n in report.weaknesses + report.strengths
    )
    return [
        Check("structure: the three weakest, weakest first", weak == weakest,
              f"got {weak}, expected {weakest}"),
        Check("structure: the two strongest", strong == order[:2],
              f"got {strong}, expected {order[:2]}"),
        Check("structure: the profile's scores", scores_ok, "" if scores_ok else "a score differs"),
        Check("structure: a plan step per weakness", [p.skill for p in report.plan] == weakest,
              f"plan for {[p.skill for p in report.plan]}"),
        Check("structure: the case's maps", [m.beatmap_id for m in report.maps]
              == [m.beatmap_id for m in case.practice.maps]),
    ]  # fmt: skip


# ---------- Numbers ----------


def check_numbers(case: Case, report: Report) -> Check:
    """Every number in a model's words is a fact. A template report's are by design."""
    if report.source != "llm":
        return Check("numbers: all facts", True, "template words: nothing to check")
    facts = load_facts(case.facts)
    words = Written(
        summary=report.summary,
        weaknesses=[
            {"skill": w.skill, "explanation": w.explanation or ""} for w in report.weaknesses
        ],
        plan=[{"skill": p.skill, "text": p.text} for p in report.plan],
    )
    unknown = unknown_numbers(words, fact_numbers(template_report(facts)) | tool_numbers(facts))
    return Check(
        "numbers: all facts",
        not unknown,
        "" if not unknown else f"not facts: {', '.join(f'{n:g}' for n in unknown)}",
    )


# ---------- Practice maps ----------


def as_pick(m: PracticeMap) -> Pick:
    beatmap = Beatmap(id=m.beatmap_id, beatmapset_id=m.beatmapset_id, star_rating=m.star_rating,
                      ar=m.ar, od=m.od, bpm=m.bpm, hit_length_s=m.hit_length_s)  # fmt: skip
    vector = PatternVector(tuple(m.vector.get(f, 0.0) for f in FIELDS))
    return Pick(beatmap, vector, 0.0)


def allowed_range(case: Case, widenings: int) -> tuple[float, float] | None:
    """The star range the recommender works in for this case, widened `widenings` times
    (None without practice maps)."""
    p = case.practice
    if p.target_skill is None or p.comfort_stars is None or p.hardest_in_pool is None:
        return None
    level = min(p.comfort_stars, p.hardest_in_pool - sum(STEP_UP) / 2)
    return star_range(level, p.target_skill, widenings)


def check_practice(case: Case, max_widenings: int = 3) -> list[Check]:
    p = case.practice
    if not p.maps or p.target_skill is None:
        return [Check("maps: some were picked", False, "no practice maps")]
    weakest = ranked(case.facts.scores)[-1]
    sets = [m.beatmapset_id for m in p.maps]
    played = [m.beatmap_id for m in p.maps if m.played]
    low, high = allowed_range(case, max_widenings) or (0.0, 0.0)
    outside = [m.beatmap_id for m in p.maps if not low <= m.star_rating < high]
    first_low, first_high = allowed_range(case, 0) or (0.0, 0.0)
    in_first = sum(first_low <= m.star_rating < first_high for m in p.maps) / len(p.maps)
    need = NEEDS[p.target_skill]
    without = [m.beatmap_id for m in p.maps if need(as_pick(m)) <= 0]
    return [
        Check("maps: some were picked", True, f"{len(p.maps)} maps"),
        Check("maps: for the weakest skill", p.target_skill == weakest,
              f"for {p.target_skill}, weakest is {weakest}"),
        Check("maps: one per mapset", len(sets) == len(set(sets))),
        Check("maps: none played", not played, f"played: {played}" if played else ""),
        Check("maps: within the allowed star range", not outside,
              f"{low:.2f}-{high:.2f}; outside: {outside}" if outside else f"{low:.2f}-{high:.2f}"),
        Check("maps: in the first star range", True,
              f"{in_first:.0%} in {first_low:.2f}-{first_high:.2f}", value=in_first),
        Check(f"maps: each has some {p.target_skill} need", not without,
              f"none: {without}" if without else ""),
    ]  # fmt: skip


# ---------- A case ----------


def check_case(case: Case, report: Report) -> CaseResult:
    return CaseResult(
        case=case.name,
        source=report.source,
        fallback_reason=report.fallback_reason,
        checks=[*check_structure(case, report), check_numbers(case, report), *check_practice(case)],
    )
