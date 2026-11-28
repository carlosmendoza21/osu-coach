"""Unit tests for osu_coach.evals: saving and loading cases, the automatic checks, and the
command line. Cases are built by hand here; tests/integration/test_evals_live.py exports
them from a real Postgres.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from osu_coach.coach import CoachFacts, ReplayFacts, Report, ReportMap, template_report
from osu_coach.evals.__main__ import main, summary
from osu_coach.evals.cases import (
    Case,
    Practice,
    PracticeMap,
    load_facts,
    read_cases,
    safe_name,
    save_facts,
    write_case,
)
from osu_coach.evals.checks import (
    CaseResult,
    Check,
    allowed_range,
    check_case,
    check_numbers,
    check_practice,
    check_structure,
)
from osu_coach.skills import Play
from osu_coach.vectors import FIELDS

SCORES = {"aim": 41.0, "speed": 32.8, "accuracy": 68.0, "consistency": 80.4, "stamina": 38.2,
          "reading": 7.4}  # fmt: skip
PLAYS = [
    Play(1.0, 0, 450, 450, 1.86, 1.6, 33.7, 7.0, 7.0, ("PF",)),
    Play(0.95, 2, 300, 420, 1.7, 1.9, 223.0, 9.0, 8.0, ()),
]
# A UR of 95.25: the evidence writes 95.2, while the replay tool gives the raw value.
REPLAYS = ReplayFacts(count=1, mean_ur=95.25, miss_rates={"stream": 0.014}, notes={"stream": 556})


def practice_map(map_id: int, stars: float = 4.1, ar: float = 9.0, played: bool = False,
                 set_id: int | None = None) -> PracticeMap:  # fmt: skip
    return PracticeMap(beatmap_id=map_id, beatmapset_id=set_id or map_id, rank=map_id - 100,
                       star_rating=stars, ar=ar, od=8, bpm=170, hit_length_s=90,
                       vector=dict.fromkeys(FIELDS, 0.1), played=played,
                       reason=f"AR {ar:g}; {stars:.1f}★")  # fmt: skip


def maps(n: int = 3) -> list[PracticeMap]:
    return [practice_map(101 + i, stars=4.0 + i / 10) for i in range(n)]


def facts(
    replays: ReplayFacts | None = REPLAYS, practice: list[PracticeMap] | None = None
) -> CoachFacts:
    chosen = maps() if practice is None else practice
    report_maps = [
        ReportMap(
            beatmap_id=m.beatmap_id,
            name=f"A - T{m.beatmap_id} [Insane]",
            star_rating=m.star_rating,
            reason=m.reason,
        )
        for m in chosen
    ]
    return CoachFacts(player_id=2, username="peppy", scores=SCORES, plays=PLAYS, replays=replays,
                      maps=report_maps)  # fmt: skip


def case(practice: list[PracticeMap] | None = None, target: str | None = "reading",
         comfort: float | None = 3.7, hardest: float | None = 9.0,
         scores: dict[str, float] | None = None) -> Case:  # fmt: skip
    chosen = maps() if practice is None else practice
    saved = save_facts(facts(practice=chosen))
    if scores is not None:
        saved = saved.model_copy(update={"scores": scores})
    return Case(
        name="peppy",
        exported_at=datetime(2026, 11, 27, tzinfo=UTC),
        facts=saved,
        practice=Practice(
            target_skill=target, comfort_stars=comfort, hardest_in_pool=hardest, maps=chosen
        ),
    )


def by_name(checks: list[Check]) -> dict[str, Check]:
    return {c.name: c for c in checks}


# ---------- Saving and loading ----------


@pytest.mark.parametrize("replays", [REPLAYS, None], ids=["with-replays", "without"])
def test_facts_survive_saving(replays: ReplayFacts | None) -> None:
    original = facts(replays=replays)

    loaded = load_facts(save_facts(original))

    assert loaded == original  # plays, mods as tuples, replays, maps: all as they were


def test_a_case_survives_json(tmp_path: Path) -> None:
    written = write_case(case(), tmp_path)

    assert written == tmp_path / "peppy.json"
    json.loads(written.read_text(encoding="utf-8"))  # plain JSON, readable by anything
    assert read_cases(tmp_path) == [case()]


def test_cases_are_read_by_name(tmp_path: Path) -> None:
    # Sorted by name exactly (capitals first), whatever order the file system lists them
    # in: Windows lists A, b, C.
    for name in ("b", "C", "A"):
        write_case(case().model_copy(update={"name": name}), tmp_path)

    assert [c.name for c in read_cases(tmp_path)] == ["A", "C", "b"]


def test_no_cases(tmp_path: Path) -> None:
    assert read_cases(tmp_path) == []


@pytest.mark.parametrize(
    ("username", "name"),
    [
        ("peppy", "peppy"),
        ("bored yes", "bored_yes"),  # a space
        ("-Rogue-", "-Rogue-"),  # dashes are fine
        ("[Toy]", "Toy"),  # brackets go, and the underscores they leave at the ends
        ("a/b\\c", "a_b_c"),  # edge: no path separators in a file name
        ("..", "player"),  # edge: nothing usable left
        ("ŠtěpánK", "t_p_nK"),  # letters outside A-Z become _
    ],
)
def test_safe_name(username: str, name: str) -> None:
    assert safe_name(username) == name


# ---------- Structure ----------


def test_a_template_report_has_the_right_structure() -> None:
    checks = check_structure(case(), template_report(facts()))

    assert all(c.passed for c in checks), [c for c in checks if not c.passed]
    assert len(checks) == 5


def tampered(**changes: object) -> Report:
    return template_report(facts()).model_copy(update=changes)


def test_weaknesses_out_of_order_fail() -> None:
    report = template_report(facts())
    swapped = tampered(weaknesses=list(reversed(report.weaknesses)))

    check = by_name(check_structure(case(), swapped))["structure: the three weakest, weakest first"]

    assert not check.passed
    assert (
        check.detail
        == "got ['stamina', 'speed', 'reading'], expected ['reading', 'speed', 'stamina']"
    )


def test_a_wrong_score_fails() -> None:
    report = template_report(facts())
    wrong = report.weaknesses[0].model_copy(update={"score": 50.0})

    checks = by_name(check_structure(case(), tampered(weaknesses=[wrong, *report.weaknesses[1:]])))

    assert not checks["structure: the profile's scores"].passed
    assert checks["structure: the three weakest, weakest first"].passed  # only the score


def test_wrong_strengths_plan_and_maps_fail() -> None:
    report = template_report(facts())

    strengths = by_name(check_structure(case(), tampered(strengths=report.strengths[:1])))
    plan = by_name(check_structure(case(), tampered(plan=report.plan[:2])))
    no_maps = by_name(check_structure(case(), tampered(maps=[])))

    assert not strengths["structure: the two strongest"].passed
    assert not plan["structure: a plan step per weakness"].passed
    assert not no_maps["structure: the case's maps"].passed


# ---------- Numbers ----------


def llm_words(summary: str, explanation: str | None = "You rarely see high AR.") -> Report:
    report = template_report(facts())
    return report.model_copy(update={
        "source": "llm",
        "summary": summary,
        "weaknesses": [w.model_copy(update={"explanation": explanation})
                       for w in report.weaknesses],
    })  # fmt: skip


def test_a_template_has_no_numbers_to_check() -> None:
    check = check_numbers(case(), template_report(facts()))

    assert check.passed
    assert check.detail == "template words: nothing to check"


@pytest.mark.parametrize(
    ("summary", "passed", "detail"),
    [
        ("Your highest AR is 9.0, over 2 plays.", True, ""),
        ("Your UR of 95.3 is steady.", True, ""),  # only the replay tool's 95.25, rounded
        ("Aim for AR 9.7 next.", False, "not facts: 9.7"),
        ("Reach 99.5% and 1,000 notes.", False, "not facts: 99.5, 1000"),
    ],
)
def test_a_models_numbers(summary: str, passed: bool, detail: str) -> None:
    check = check_numbers(case(), llm_words(summary))

    assert (check.passed, check.detail) == (passed, detail)


def test_a_missing_explanation_is_no_numbers() -> None:
    # Edge: a model report whose weaknesses have no explanation (None) still checks.
    assert check_numbers(case(), llm_words("Keep going.", explanation=None)).passed


# ---------- Practice maps ----------


def test_good_practice_maps_pass() -> None:
    checks = check_practice(case())

    assert all(c.passed for c in checks), [c for c in checks if not c.passed]
    assert [c.name for c in checks] == [
        "maps: some were picked",
        "maps: for the weakest skill",
        "maps: one per mapset",
        "maps: none played",
        "maps: within the allowed star range",
        "maps: in the first star range",
        "maps: each has some reading need",
    ]


@pytest.mark.parametrize(
    ("practice", "target"),
    [([], "reading"), (maps(), None)],
    ids=["no-maps", "no-target"],
)
def test_no_practice_maps_fail(practice: list[PracticeMap], target: str | None) -> None:
    checks = check_practice(case(practice=practice, target=target))

    assert [(c.name, c.passed, c.detail) for c in checks] == [
        ("maps: some were picked", False, "no practice maps")]  # fmt: skip


def test_maps_for_another_skill_fail() -> None:
    check = by_name(check_practice(case(target="speed")))["maps: for the weakest skill"]

    assert (check.passed, check.detail) == (False, "for speed, weakest is reading")


def test_two_from_one_mapset_fail() -> None:
    twins = [practice_map(101, set_id=7), practice_map(102, set_id=7)]

    assert not by_name(check_practice(case(practice=twins)))["maps: one per mapset"].passed


def test_a_played_map_fails() -> None:
    check = by_name(check_practice(case(practice=[practice_map(101, played=True)])))[
        "maps: none played"
    ]

    assert (check.passed, check.detail) == (False, "played: [101]")


def test_the_allowed_range() -> None:
    # Reading steps up: comfort 3.7 + 0.2 to + 0.7, and 0.5 more each side per widening.
    assert allowed_range(case(), 0) == (3.9, 4.4)
    assert allowed_range(case(), 3) == (2.4, 5.9)
    # A comfort level above the pool: centred on the hardest map (9.0 - 0.45).
    assert allowed_range(case(comfort=12.0), 0) == (8.75, 9.25)
    # Accuracy is practised at the comfort level.
    assert allowed_range(case(target="accuracy"), 0) == (3.3, 3.8)


@pytest.mark.parametrize("missing", ["target", "comfort", "hardest"])
def test_no_allowed_range_without_its_inputs(missing: str) -> None:
    c = case(target=None if missing == "target" else "reading",
             comfort=None if missing == "comfort" else 3.7,
             hardest=None if missing == "hardest" else 9.0)  # fmt: skip

    assert allowed_range(c, 0) is None


@pytest.mark.parametrize(
    ("stars", "inside"),
    [(2.4, True), (5.89, True), (2.39, False), (5.9, False)],  # the top end is left out
)
def test_maps_outside_the_allowed_range_fail(stars: float, inside: bool) -> None:
    check = by_name(check_practice(case(practice=[practice_map(101, stars=stars)])))[
        "maps: within the allowed star range"]  # fmt: skip

    assert check.passed is inside


def test_the_share_in_the_first_range() -> None:
    # 3.9-4.4: two of the four maps are in it.
    chosen = [practice_map(101, stars=s) for s in (3.0, 4.0, 4.3, 5.5)]
    chosen = [m.model_copy(update={"beatmap_id": 101 + i, "beatmapset_id": 101 + i})
              for i, m in enumerate(chosen)]  # fmt: skip

    check = by_name(check_practice(case(practice=chosen)))["maps: in the first star range"]

    assert check.passed  # a measurement, not a requirement
    assert check.value == 0.5
    assert check.detail == "50% in 3.90-4.40"


def test_a_map_without_the_skills_need_fails() -> None:
    # Stamina needs long streams; a map with none has nothing to train it.
    flat = practice_map(101).model_copy(update={"vector": dict.fromkeys(FIELDS, 0.0)})
    scores = {**SCORES, "reading": 60.0, "stamina": 5.0}

    check = by_name(check_practice(case(practice=[flat], target="stamina", scores=scores)))[
        "maps: each has some stamina need"]  # fmt: skip

    assert (check.passed, check.detail) == (False, "none: [101]")


# ---------- A case ----------


def test_a_case_result() -> None:
    result = check_case(case(), template_report(facts()))

    assert (result.case, result.source, result.fallback_reason) == ("peppy", "template", None)
    assert result.passed
    assert len(result.checks) == 5 + 1 + 7


def test_one_failed_check_fails_the_case() -> None:
    result = CaseResult("x", "llm", None, [Check("a", True), Check("b", False, "why")])

    assert not result.passed


def test_the_summary() -> None:
    results = [
        CaseResult("one", "llm", None, [Check("a", True), Check("m", True, value=0.25)]),
        CaseResult("two", "template", "numbers not in the facts: 8",
                   [Check("a", False, "broken"), Check("m", True, value=0.75)]),
    ]  # fmt: skip

    text = summary(results)

    assert "a                                                1/2" in text
    assert "m                                                2/2   (mean 50%)" in text
    assert "reports with the model's words: 1/2" in text
    assert "two: fell back: numbers not in the facts: 8" in text
    assert "failed:\n  two: a: broken" in text


# ---------- The command line ----------


def test_check_passes_on_good_cases(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_case(case(), tmp_path)

    code = main(["check", "--cases", str(tmp_path)])

    assert code == 0
    out = capsys.readouterr().out
    assert "1 cases, reports by templates" in out
    assert "maps: none played" in out


def test_check_fails_when_a_check_does(tmp_path: Path) -> None:
    write_case(case(practice=[practice_map(101, played=True)]), tmp_path)

    assert main(["check", "--cases", str(tmp_path)]) == 1


def test_check_without_cases(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", "--cases", str(tmp_path)]) == 1
    assert "no cases" in capsys.readouterr().out


def test_check_limit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for name in ("a", "b", "c"):
        write_case(case().model_copy(update={"name": name}), tmp_path)

    main(["check", "--cases", str(tmp_path), "--limit", "2"])

    assert "2 cases" in capsys.readouterr().out


def test_export_needs_players() -> None:
    with pytest.raises(SystemExit):
        main(["export"])


def test_a_command_is_needed() -> None:
    with pytest.raises(SystemExit):
        main([])
