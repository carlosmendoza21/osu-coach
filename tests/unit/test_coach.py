"""Unit tests for osu_coach.coach's evidence and template report, on hand-made plays and
replay numbers. Gathering the facts from the database, and the endpoint, are in
tests/integration/test_coach_live.py.
"""

import pytest

from osu_coach.coach import (
    PLAN,
    PLAN_LATER,
    TOP_PLAYS,
    CoachFacts,
    ReplayFacts,
    ReportMap,
    play_evidence,
    ranked,
    replay_evidence,
    template_report,
    top,
)
from osu_coach.skills import SKILLS, Play


def play(acc: float = 0.98, miss: int = 0, combo: int = 500, map_combo: int = 500,
         aim: float = 2.5, speed: float = 2.0, notes: float = 100, ar: float = 9,
         od: float = 8, mods: tuple[str, ...] = ()) -> Play:  # fmt: skip
    return Play(acc, miss, combo, map_combo, aim, speed, notes, ar, od, mods)


PLAYS = [
    play(acc=0.99, miss=0, combo=500, aim=3.0, speed=2.5, notes=300, ar=9.7, od=9, mods=("HD",)),
    play(acc=0.95, miss=2, combo=250, aim=2.0, speed=1.5, notes=500, ar=9.0, od=8),
    play(acc=0.97, miss=5, combo=100, aim=2.5, speed=2.0, notes=900, ar=8.3, od=7),
]


# ---------- Evidence from plays ----------


@pytest.mark.parametrize(
    ("skill", "expected"),
    [
        ("aim", [
            "Your 3 hardest-aim passes average 2.50 aim difficulty (the hardest: 3.00).",
            "Those plays average 2.3 misses.",
        ]),
        ("speed", ["Your 3 fastest passes average 2.00 speed difficulty, at 97.0% accuracy."]),
        ("accuracy", [
            "Your average accuracy is 97.0% over 3 plays (best 99.00%).",
            "The highest OD you've passed is 9.0.",
        ]),
        # Combo held: 100%, 50%, 20% -> 57%; misses 0, 2, 5 -> 2.3; one play without a miss.
        ("consistency", [
            "You hold 57% of a map's combo on average, with 2.3 misses per play.",
            "1 of your 3 plays have no misses.",
        ]),
        # Edge: the 900-note play has 5 misses, more than 3: the 500-note play counts.
        ("stamina", [
            "The most hard-to-tap notes you've cleared in one play (3 misses or fewer) is 500.",
        ]),
        ("reading", [
            "The highest AR you've passed is 9.7; your plays average AR 9.0.",
            "1 of your 3 plays use Hidden.",
        ]),
    ],
)  # fmt: skip
def test_play_evidence(skill: str, expected: list[str]) -> None:
    assert play_evidence(skill, PLAYS) == expected


def test_no_plays_no_evidence() -> None:
    assert all(play_evidence(s, []) == [] for s in SKILLS)


def test_an_unknown_skill_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown skill"):
        play_evidence("rhythm", PLAYS)


def test_combo_held_is_capped_and_needs_a_map_combo() -> None:
    # Edge: a combo over the map's (it can't be, but data can say so) counts as 100%;
    # a map with no combo stored is left out rather than divided by.
    plays = [play(combo=600, map_combo=500), play(combo=50, map_combo=0)]

    assert play_evidence("consistency", plays)[0].startswith("You hold 100% of a map's combo")


def test_stamina_without_a_clean_play() -> None:
    # Edge: every play has more than 3 misses: none counts.
    assert play_evidence("stamina", [play(miss=4, notes=800)]) == [
        "The most hard-to-tap notes you've cleared in one play (3 misses or fewer) is 0."
    ]


def test_top_takes_the_best_few() -> None:
    plays = [play(aim=a) for a in range(1, 20)]

    best = top(plays, "aim_difficulty")

    assert len(best) == TOP_PLAYS
    assert [p.aim_difficulty for p in best] == list(range(19, 19 - TOP_PLAYS, -1))


# ---------- Evidence from replays ----------

REPLAYS = ReplayFacts(
    count=2,
    mean_ur=85.25,
    miss_rates={"jump": 0.02, "stream": 0.05, "slider": 0.0},
    notes={"jump": 100, "stream": 200, "slider": 300},
)


@pytest.mark.parametrize(
    ("skill", "expected"),
    [
        ("accuracy", ["Your 2 uploaded replay(s) average 85.2 UR "
                      "(unstable rate: lower is steadier timing)."]),
        ("aim", ["In your 2 uploaded replay(s) you miss 2.0% of 100 jump notes."]),
        ("speed", ["In your 2 uploaded replay(s) you miss 5.0% of 200 stream notes."]),
        # 2 + 10 + 0 misses of 600 notes.
        ("consistency", ["In your 2 uploaded replay(s) you miss 12 of 600 notes (2.0%)."]),
        ("stamina", []),  # replays don't show stamina
        ("reading", []),
    ],
)  # fmt: skip
def test_replay_evidence(skill: str, expected: list[str]) -> None:
    assert replay_evidence(skill, REPLAYS) == expected


def test_no_replays_no_evidence() -> None:
    assert all(replay_evidence(s, None) == [] for s in SKILLS)


def test_replays_without_the_pattern_or_a_ur() -> None:
    # Edge: no jumps in the replays (nothing to say about aim), and no UR.
    replays = ReplayFacts(count=1, mean_ur=None, miss_rates={"stream": 0.0}, notes={"stream": 50})

    assert replay_evidence("aim", replays) == []
    assert replay_evidence("accuracy", replays) == []
    assert replay_evidence("speed", replays) == [
        "In your 1 uploaded replay(s) you miss 0.0% of 50 stream notes."
    ]


# ---------- The report ----------

SCORES = {
    "aim": 90.0,
    "speed": 40.0,
    "accuracy": 75.0,
    "consistency": 30.0,
    "stamina": 55.0,
    "reading": 60.0,
}
MAP = ReportMap(beatmap_id=1, name="A - T [Insane]", star_rating=5.1, reason="62% jumps; ...")


def facts(maps: list[ReportMap] | None = None, replays: ReplayFacts | None = None,
          scores: dict[str, float] | None = None) -> CoachFacts:  # fmt: skip
    return CoachFacts(player_id=2, username="peppy", scores=scores or SCORES, plays=PLAYS,
                      replays=replays, maps=[MAP] if maps is None else maps)  # fmt: skip


def test_skills_ranked_best_first() -> None:
    assert ranked(SCORES) == ["aim", "accuracy", "reading", "stamina", "speed", "consistency"]


def test_ties_keep_the_skills_order() -> None:
    # Edge: all equal: aim, speed, accuracy... as SKILLS lists them.
    assert ranked(dict.fromkeys(SKILLS, 50.0)) == list(SKILLS)


def test_strengths_and_weaknesses() -> None:
    report = template_report(facts())

    assert [(n.skill, n.score) for n in report.strengths] == [("aim", 90.0), ("accuracy", 75.0)]
    # Weakest first.
    assert [(n.skill, n.score) for n in report.weaknesses] == [
        ("consistency", 30.0),
        ("speed", 40.0),
        ("stamina", 55.0),
    ]
    # Each with its evidence.
    assert report.weaknesses[0].evidence == play_evidence("consistency", PLAYS)


def test_evidence_from_plays_and_replays() -> None:
    report = template_report(facts(replays=REPLAYS))

    consistency = report.weaknesses[0]
    assert consistency.evidence == play_evidence("consistency", PLAYS) + replay_evidence(
        "consistency", REPLAYS
    )


def test_the_plan_starts_with_the_practice_maps() -> None:
    report = template_report(facts())

    assert [s.skill for s in report.plan] == ["consistency", "speed", "stamina"]
    assert report.plan[0].text == PLAN["consistency"]  # "Play the practice maps below..."
    assert report.plan[1].text == f"Then speed (40/100): {PLAN_LATER['speed']}"
    assert report.plan[2].text == f"Then stamina (55/100): {PLAN_LATER['stamina']}"
    # Only the first step points at the maps: they're picked for the weakest skill.
    assert all("practice maps" not in s.text for s in report.plan[1:])


def test_without_maps_no_step_points_at_them() -> None:
    report = template_report(facts(maps=[]))

    assert (
        report.plan[0].text == "Go for full combos on maps at your level, finishing every attempt."
    )
    assert all("practice maps" not in s.text for s in report.plan)
    assert report.maps == []


def test_every_skill_has_plan_text() -> None:
    assert set(PLAN) == set(PLAN_LATER) == set(SKILLS)


BEST_AND_WORST = "peppy's strongest skill is aim (90/100) and the weakest is consistency (30/100)"


@pytest.mark.parametrize(
    ("replays", "summary"),
    [
        (None, f"{BEST_AND_WORST}, from 3 plays."),
        (REPLAYS, f"{BEST_AND_WORST}, from 3 plays and 2 uploaded replay(s)."),
    ],
)  # fmt: skip
def test_summary(replays: ReplayFacts | None, summary: str) -> None:
    assert template_report(facts(replays=replays)).summary == summary


def test_the_rest_of_the_report() -> None:
    report = template_report(facts())

    assert (report.player_id, report.username, report.source) == (2, "peppy", "template")
    assert report.maps == [MAP]
