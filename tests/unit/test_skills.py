"""Unit tests for osu_coach.skills: each play's value per skill, the pp-style weighting,
the 0-100 scales, and the profile they make. Plays are built by hand; loading them from
the database is in tests/integration/test_skills_live.py.
"""

from dataclasses import replace

import pytest

from osu_coach.skills import (
    MISS_FACTOR,
    SCALES,
    SKILLS,
    VALUES,
    WEIGHT_DECAY,
    Play,
    accuracy_value,
    aim_value,
    consistency_value,
    reading_value,
    skill_profile,
    speed_value,
    stamina_value,
    to_score,
    weighted_top,
)

PLAY = Play(
    accuracy=1.0,
    count_miss=0,
    max_combo=450,
    map_max_combo=450,
    aim_difficulty=2.0,
    speed_difficulty=1.5,
    speed_note_count=120,
    ar=9.0,
    od=8.0,
    mods=(),
)


def play(**changes: object) -> Play:
    return replace(PLAY, **changes)  # type: ignore[arg-type]


# ---------- Each play's value ----------


@pytest.mark.parametrize(
    ("changes", "value"),
    [
        ({}, 2.0),  # the map's aim difficulty
        ({"aim_difficulty": 3.5}, 3.5),
        ({"count_miss": 1}, 2.0 * 0.97),  # 3% off per miss
        ({"count_miss": 5}, 2.0 * 0.97**5),
        ({"accuracy": 0.9}, 2.0),  # aim doesn't care how accurate
    ],
)
def test_aim_value(changes: dict[str, object], value: float) -> None:
    assert aim_value(play(**changes)) == pytest.approx(value)


@pytest.mark.parametrize(
    ("changes", "value"),
    [
        ({}, 1.5),
        ({"count_miss": 2}, 1.5 * 0.97**2),
        ({"accuracy": 0.9}, 1.5 * 0.81),  # tapping shows in accuracy: squared
        ({"accuracy": 0.9, "count_miss": 1}, 1.5 * 0.81 * 0.97),
    ],
)
def test_speed_value(changes: dict[str, object], value: float) -> None:
    assert speed_value(play(**changes)) == pytest.approx(value)


@pytest.mark.parametrize(
    ("changes", "value"),
    [
        ({}, 1.0),  # OD 8: as it is
        ({"accuracy": 0.96}, 0.96),
        ({"accuracy": 0.96, "od": 10}, 0.97),  # +0.5% per OD over 8
        ({"accuracy": 0.96, "od": 6}, 0.95),  # and less under
        ({"accuracy": 0.96, "od": 11.11}, 0.96 + 0.005 * 3.11),  # DT's effective OD
    ],
)
def test_accuracy_value(changes: dict[str, object], value: float) -> None:
    assert accuracy_value(play(**changes)) == pytest.approx(value)


@pytest.mark.parametrize(
    ("changes", "value"),
    [
        ({}, 1.0),  # a full combo
        ({"max_combo": 225}, 0.5),  # held half the map's combo
        ({"max_combo": 225, "count_miss": 1}, 0.5 * 0.95),  # and missed once
        ({"count_miss": 3}, 0.95**3),
        ({"max_combo": 500}, 1.0),  # never more than the whole map
        ({"map_max_combo": 0}, 0.0),  # no combo to hold (shouldn't happen): nothing
    ],
)
def test_consistency_value(changes: dict[str, object], value: float) -> None:
    assert consistency_value(play(**changes)) == pytest.approx(value)


@pytest.mark.parametrize(
    ("misses", "value"),
    [(0, 120), (3, 120), (4, 0)],  # a map with more than a few misses wasn't cleared
)
def test_stamina_value(misses: int, value: float) -> None:
    assert stamina_value(play(count_miss=misses)) == value


@pytest.mark.parametrize(
    ("changes", "value"),
    [
        ({}, 9.0),
        ({"ar": 11.0}, 11.0),  # the AR seen, as given (DT's applied when loading)
        ({"mods": ("HD",)}, 9.5),  # HD: half an AR more
        ({"mods": ("FL",)}, 10.0),  # FL: a whole AR more
        ({"mods": ("HD", "FL", "DT")}, 10.5),
        ({"mods": ("HR",)}, 9.0),  # HR's AR is already in `ar`
    ],
)
def test_reading_value(changes: dict[str, object], value: float) -> None:
    assert reading_value(play(**changes)) == pytest.approx(value)


def test_every_skill_has_a_value_and_a_scale() -> None:
    assert set(VALUES) == set(SCALES) == set(SKILLS)
    assert len(SKILLS) == 6


def test_constants() -> None:
    assert (WEIGHT_DECAY, MISS_FACTOR) == (0.95, 0.97)


# ---------- Weighting ----------


@pytest.mark.parametrize(
    ("values", "result"),
    [
        ([], 0.0),
        ([3.0], 3.0),
        ([2.0, 2.0, 2.0], 2.0),  # all the same: that value
        ([10.0, 0.0], 10.0 / 1.95),  # weights 1 and 0.95
        ([0.0, 10.0], 10.0 / 1.95),  # sorted first: the order given doesn't matter
        ([4.0, 2.0, 1.0], (4 + 2 * 0.95 + 0.95**2) / (1 + 0.95 + 0.95**2)),
    ],
)
def test_weighted_top(values: list[float], result: float) -> None:
    assert weighted_top(values) == pytest.approx(result)


def test_the_best_plays_count_most() -> None:
    # Adding a weak play pulls the result down only a little; a strong one lifts it more.
    base = [3.0] * 10
    assert weighted_top([*base, 0.0]) > 2.7
    assert weighted_top([5.0, *base]) > weighted_top([*base, 5.0]) - 1e-9


def test_one_fluke_cant_set_the_score() -> None:
    assert weighted_top([10.0] + [2.0] * 49) < 3.0


# ---------- Scales ----------

SCALE = [(1.0, 0.0), (2.0, 50.0), (4.0, 100.0)]


@pytest.mark.parametrize(
    ("raw", "score"),
    [
        (0.0, 0.0),  # below the first point: flat
        (1.0, 0.0),
        (1.5, 25.0),  # straight lines between points
        (2.0, 50.0),
        (3.0, 75.0),
        (4.0, 100.0),
        (9.0, 100.0),  # above the last: flat
    ],
)
def test_to_score(raw: float, score: float) -> None:
    assert to_score(raw, SCALE) == pytest.approx(score)


@pytest.mark.parametrize("skill", SKILLS)
def test_each_scale_runs_0_to_100_upwards(skill: str) -> None:
    scale = SCALES[skill]
    assert (scale[0][1], scale[-1][1]) == (0, 100)
    xs, ys = [x for x, _ in scale], [y for _, y in scale]
    assert xs == sorted(xs) and len(set(xs)) == len(xs)  # strictly increasing
    assert ys == sorted(ys)


@pytest.mark.parametrize(
    ("skill", "raw", "score"),
    [
        ("aim", 2.5, 60),  # a 5-star map's aim
        ("speed", 2.0, 60),
        ("accuracy", 0.96, 60),
        ("consistency", 0.95, 85),
        ("stamina", 500, 85),
        ("reading", 10.5, 85),  # AR 10.5: DT on AR 9
    ],
)
def test_reference_points(skill: str, raw: float, score: float) -> None:
    assert to_score(raw, SCALES[skill]) == pytest.approx(score)


# ---------- The profile ----------


def test_a_profile() -> None:
    plays = [play(), play(aim_difficulty=3.0, count_miss=1), play(accuracy=0.95, od=9)]

    profile = skill_profile(plays, plays_skipped=2)

    assert set(profile.scores) == set(SKILLS)
    assert (profile.plays_used, profile.plays_skipped) == (3, 2)
    assert all(0 <= s <= 100 for s in profile.scores.values())
    # Each score is the weighted top of its values, on its scale, to one decimal.
    raw = weighted_top([aim_value(p) for p in plays])
    assert profile.scores["aim"] == round(to_score(raw, SCALES["aim"]), 1)


def test_no_plays_is_all_zeros() -> None:
    profile = skill_profile([], plays_skipped=5)

    assert profile.scores == dict.fromkeys(SKILLS, 0.0)
    assert (profile.plays_used, profile.plays_skipped) == (0, 5)


def test_strongest_and_weakest() -> None:
    # Good aim and accuracy on low-AR, short maps: reading and stamina are the gaps.
    plays = [play(aim_difficulty=4.0, ar=7.5, speed_note_count=20)] * 5

    profile = skill_profile(plays)

    assert profile.strongest in ("aim", "accuracy", "consistency")
    assert profile.weakest in ("reading", "stamina")
    assert profile.scores[profile.weakest] == min(profile.scores.values())
    assert profile.scores[profile.strongest] == max(profile.scores.values())


@pytest.mark.parametrize(
    ("skill", "better", "worse"),
    [
        ("aim", {"aim_difficulty": 3.0}, {"aim_difficulty": 2.0}),
        ("aim", {"count_miss": 0}, {"count_miss": 5}),
        ("speed", {"speed_difficulty": 2.5}, {"speed_difficulty": 1.5}),
        ("accuracy", {"accuracy": 0.99}, {"accuracy": 0.95}),
        ("consistency", {"count_miss": 0}, {"count_miss": 2}),
        ("stamina", {"speed_note_count": 400}, {"speed_note_count": 100}),
        ("reading", {"ar": 10.5}, {"ar": 9.0}),
    ],
)
def test_better_play_better_score(
    skill: str, better: dict[str, object], worse: dict[str, object]
) -> None:
    good = skill_profile([play(**better)] * 5).scores[skill]
    bad = skill_profile([play(**worse)] * 5).scores[skill]

    assert good > bad
