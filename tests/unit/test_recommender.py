"""Unit tests for osu_coach.recommender's choices that need no database: the star range,
what each skill needs from a map, choosing from the maps in range, and the reasons.
Maps are hand-made; tests/integration/test_recommender_live.py runs it against Postgres.
"""

import pytest

from osu_coach.models import Beatmap
from osu_coach.recommender import (
    COUNT,
    NEEDS,
    POOL,
    Pick,
    choose,
    one_per_mapset,
    pattern_note,
    reason,
    star_range,
)
from osu_coach.skills import SKILLS
from osu_coach.vectors import FIELDS, PatternVector


def vector(**fields: float) -> PatternVector:
    return PatternVector(tuple(fields.get(f, 0.0) for f in FIELDS))


def pick(map_id: int, distance: float = 1.0, set_id: int | None = None, stars: float = 5.0,
         ar: float = 9, od: float = 8, length: int = 120, bpm: float = 180,
         **fields: float) -> Pick:  # fmt: skip
    beatmap = Beatmap(id=map_id, beatmapset_id=map_id if set_id is None else set_id,
                      star_rating=stars, ar=ar, od=od, hit_length_s=length, bpm=bpm)  # fmt: skip
    return Pick(beatmap, vector(**fields), distance)


def ids(picks: list[Pick]) -> list[int]:
    return [p.beatmap.id for p in picks]


# ---------- The star range ----------


@pytest.mark.parametrize(
    ("comfort", "skill", "widenings", "expected"),
    [
        (5.0, "aim", 0, (5.2, 5.7)),  # a step up
        (5.0, "reading", 0, (5.2, 5.7)),
        (5.0, "accuracy", 0, (4.6, 5.1)),  # at the comfort level
        (5.0, "consistency", 0, (4.6, 5.1)),
        (5.0, "aim", 1, (4.7, 6.2)),  # each widening: 0.5 more each side
        (5.0, "accuracy", 3, (3.1, 6.6)),
        (3.33, "speed", 0, (3.53, 4.03)),  # rounded to 2 places, no float noise
    ],
)
def test_star_range(
    comfort: float, skill: str, widenings: int, expected: tuple[float, float]
) -> None:
    assert star_range(comfort, skill, widenings) == expected


# ---------- What each skill needs ----------


def test_every_skill_has_a_need() -> None:
    assert set(NEEDS) == set(SKILLS)


@pytest.mark.parametrize(
    ("skill", "p", "expected"),
    [
        ("aim", pick(1, jump=0.4, spacing_4_up=0.3), 0.7),  # jumps and wide gaps
        ("speed", pick(1, stream=0.5, jump=0.9), 0.5),  # streams only
        # Edge: long streams count as a share of all notes: 50% of 40% streams is 20%.
        ("stamina", pick(1, stream=0.4, long_stream=0.5), 0.2),
        ("stamina", pick(1, stream=0.0, long_stream=1.0), 0.0),  # no streams, no stamina
        ("reading", pick(1, ar=9.7), 9.7),
        ("accuracy", pick(1, od=9.2), 9.2),
        ("consistency", pick(1, length=300), 300),
    ],
)
def test_needs(skill: str, p: Pick, expected: float) -> None:
    assert NEEDS[skill](p) == pytest.approx(expected)


# ---------- Choosing ----------


def test_one_per_mapset_keeps_the_first_of_each() -> None:
    picks = [pick(1, set_id=10), pick(2, set_id=20), pick(3, set_id=10), pick(4, set_id=30)]

    assert ids(one_per_mapset(picks)) == [1, 2, 4]


def test_within_the_pool_the_nearest_the_style_come_first() -> None:
    # All three fit in the pool, so the most jump-heavy (2) doesn't lead: the nearest
    # the style does.
    picks = [pick(2, 0.5, jump=0.9), pick(3, 0.3, jump=0.6), pick(1, 0.1, jump=0.1)]

    assert ids(choose(picks, "aim")) == [1, 3, 2]
    assert ids(choose(picks, "aim", count=2)) == [1, 3]


def test_the_pool_is_cut_before_ordering_by_style() -> None:
    # POOL + 5 maps: the more jumps, the farther from the style. The nearest 5 have the
    # fewest jumps and fall outside the pool, so none of them is recommended.
    picks = [pick(i, distance=float(i), jump=i / 100) for i in range(1, POOL + 6)]

    chosen = choose(picks, "aim")

    assert len(chosen) == COUNT
    assert min(ids(chosen)) == 6  # maps 1-5 cut
    assert ids(chosen) == sorted(ids(chosen))  # nearest the style first


def test_maps_with_none_of_the_need_are_left_out() -> None:
    picks = [pick(1, 0.1, stream=0.5, long_stream=0.0), pick(2, 0.9, stream=0.5, long_stream=0.5)]

    assert ids(choose(picks, "stamina")) == [2]  # map 1 is nearer, but has no long streams


def test_one_per_mapset_when_choosing() -> None:
    picks = [
        pick(1, 0.1, set_id=7, jump=0.5),
        pick(2, 0.2, set_id=7, jump=0.9),
        pick(3, 0.3, jump=0.4),
    ]

    # Map 2 is the set's more jump-heavy difficulty, but map 1 (nearer the style) is kept.
    assert ids(choose(picks, "aim")) == [1, 3]


def test_at_most_count() -> None:
    picks = [pick(i, float(i), jump=0.5) for i in range(1, 30)]

    assert len(choose(picks, "aim")) == COUNT
    assert choose(picks, "aim", count=3) == choose(picks, "aim")[:3]


def test_nothing_to_choose_from() -> None:
    assert choose([], "reading") == []


def test_reading_takes_the_highest_ar() -> None:
    picks = [pick(i, distance=0.1 * i, ar=8 + i / 10) for i in range(1, POOL + 3)]

    chosen = choose(picks, "reading")

    # The 2 lowest-AR maps (1 and 2) are cut, though they're the nearest the style.
    assert 1 not in ids(chosen) and 2 not in ids(chosen)
    assert ids(chosen) == list(range(3, 3 + COUNT))


# ---------- Reasons ----------


@pytest.mark.parametrize(
    ("skill", "p", "expected"),
    [
        ("aim", pick(1, jump=0.62, spacing_4_up=0.31),
         "62% jumps, 31% of gaps over 4 circle widths"),
        ("speed", pick(1, bpm=222.4, stream=0.45), "45% stream notes at 222 BPM"),
        ("stamina", pick(1, bpm=180, stream=0.5, long_stream=0.6),
         "30% of its notes in streams of 16+ at 180 BPM"),
        ("reading", pick(1, ar=9.3), "AR 9.3"),
        ("reading", pick(1, ar=10.0), "AR 10"),  # no ".0"
        ("accuracy", pick(1, od=8.5), "OD 8.5"),
        ("consistency", pick(1, length=245), "4:05 of play to hold a combo through"),
        ("consistency", pick(1, length=59), "0:59 of play to hold a combo through"),
    ],
)  # fmt: skip
def test_pattern_note(skill: str, p: Pick, expected: str) -> None:
    assert pattern_note(skill, p) == expected


def test_an_unknown_skill_has_no_note() -> None:
    with pytest.raises(ValueError, match="unknown skill"):
        pattern_note("rhythm", pick(1))


@pytest.mark.parametrize(
    ("stars", "comfort", "level"),
    [
        (5.8, 5.4, "0.4★ above your comfort level (5.4★)"),
        (4.9, 5.4, "0.5★ below your comfort level (5.4★)"),
        (5.42, 5.4, "right at your comfort level (5.4★)"),  # edge: within 0.05
        (5.36, 5.4, "right at your comfort level (5.4★)"),
        (9.3, 12.49, "3.2★ below your comfort level (12.5★)"),  # a DT player above the pool
    ],
)
def test_reason(stars: float, comfort: float, level: str) -> None:
    text = reason("reading", pick(1, stars=stars, ar=9), comfort)

    assert text == f"AR 9; {stars:.1f}★, {level}"
