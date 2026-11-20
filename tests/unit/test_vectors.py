"""Unit tests for osu_coach.vectors: a map's pattern vector, from hand-made objects and
from map 22423's real file. The stored side is in tests/integration/test_vectors_live.py.
"""

from dataclasses import dataclass

import pytest
from pgvector.sqlalchemy import Vector

from osu_coach.models import MapVector
from osu_coach.osufile import parse_osu
from osu_coach.patterns import circle_radius, object_features
from osu_coach.vectors import (
    DIMENSIONS,
    FIELDS,
    LONG_STREAM_NOTES,
    PatternVector,
    long_stream_share,
    pattern_vector,
    share,
    spacing_bands,
)
from tests.support import FIXTURES


@dataclass(frozen=True)
class Obj:
    pattern: str
    spacing: float | None = None


CS4 = 2 * circle_radius(4)  # one circle diameter at CS 4, in osu! pixels (72.48)


def objs(*patterns: str) -> list[Obj]:
    return [Obj(p) for p in patterns]


# ---------- The fields ----------


def test_fields() -> None:
    assert DIMENSIONS == len(FIELDS) == 11
    assert len(set(FIELDS)) == len(FIELDS)
    # The table's column holds exactly this many numbers.
    column = MapVector.__table__.c.vector.type
    assert isinstance(column, Vector)
    assert column.dim == DIMENSIONS


# ---------- Shares ----------


@pytest.mark.parametrize(
    ("part", "whole", "expected"),
    [
        (1, 4, 0.25),
        (0, 4, 0.0),
        (4, 4, 1.0),
        (0, 0, 0.0),  # edge: nothing to share (an empty map) is 0, not a division error
    ],
)
def test_share(part: int, whole: int, expected: float) -> None:
    assert share(part, whole) == expected


L = LONG_STREAM_NOTES  # 16

LONG_STREAM_CASES = [
    # (patterns, expected share of stream notes in long runs)
    pytest.param([], 0.0, id="empty-map"),
    pytest.param(["jump"] * 10, 0.0, id="no-streams"),  # edge: no stream notes at all
    pytest.param(["stream"] * (L - 1), 0.0, id="one-short-of-long"),
    pytest.param(["stream"] * L, 1.0, id="exactly-long"),  # edge: the threshold counts
    pytest.param(["stream"] * L + ["jump"] + ["stream"] * 4, 16 / 20, id="long-and-burst"),
    # Edge: two bursts split by one jump aren't one long stream.
    pytest.param(["stream"] * 10 + ["jump"] + ["stream"] * 10, 0.0, id="split-by-a-jump"),
    # Edge: a run that goes on to the last note still counts (no note after it ends it).
    pytest.param(["jump"] + ["stream"] * 20, 1.0, id="run-to-the-end"),
    pytest.param(["stream"] * 20 + ["slider"] * 3 + ["stream"] * 20, 1.0, id="two-long"),
]


@pytest.mark.parametrize(("patterns", "expected"), LONG_STREAM_CASES)
def test_long_stream_share(patterns: list[str], expected: float) -> None:
    assert long_stream_share(patterns) == pytest.approx(expected)


SPACING_CASES = [
    # (spacings in diameters, expected shares: <1, 1-2, 2-3, 3-4, 4+)
    pytest.param([], [0, 0, 0, 0, 0], id="none-measured"),  # edge: a map of one object
    pytest.param([0.5], [1, 0, 0, 0, 0], id="overlapping"),
    pytest.param([0.0], [1, 0, 0, 0, 0], id="stacked"),  # edge: no distance at all
    pytest.param([1.0], [0, 1, 0, 0, 0], id="edge-1-goes-up"),  # edge: each band starts at its edge
    pytest.param([0.999], [1, 0, 0, 0, 0], id="just-under-1"),
    pytest.param([4.0], [0, 0, 0, 0, 1], id="edge-4-goes-up"),
    pytest.param([7.0], [0, 0, 0, 0, 1], id="across-the-screen"),
    pytest.param([0.5, 1.5, 2.5, 3.5], [0.25, 0.25, 0.25, 0.25, 0], id="one-per-band"),
    pytest.param([2.2, 2.4, 2.6, 4.5], [0, 0, 0.75, 0, 0.25], id="mostly-2-3"),
]


@pytest.mark.parametrize(("diameters", "expected"), SPACING_CASES)
def test_spacing_bands(diameters: list[float], expected: list[float]) -> None:
    assert spacing_bands([d * CS4 for d in diameters], CS4) == pytest.approx(expected)


def test_spacing_bands_are_in_diameters() -> None:
    # The same 100 px gap is 2+ diameters at CS 7 (small circles) but under 1.5 at CS 2.
    small, big = 2 * circle_radius(7), 2 * circle_radius(2)

    assert spacing_bands([100], small) == [0, 0, 1, 0, 0]
    assert spacing_bands([100], big) == [0, 1, 0, 0, 0]


# ---------- The whole vector ----------


def test_a_hand_made_map() -> None:
    objects = [
        Obj("other"),  # the first object: no spacing
        Obj("jump", 3.5 * CS4),
        Obj("jump", 4.5 * CS4),
        Obj("slider", 1.5 * CS4),
        Obj("stream", 0.5 * CS4),
        Obj("stream", 0.5 * CS4),
        Obj("spinner"),  # no spacing to or from a spinner
        Obj("other", 2.5 * CS4),
    ]

    vector = pattern_vector(objects, cs=4, bpm=180, star_rating=5.5)

    assert vector.as_dict() == {
        "jump": 0.25,  # 2 of 8
        "stream": 0.25,
        "slider": 0.125,
        "long_stream": 0.0,  # a 2-note burst
        # 6 measured gaps: two under 1, one each 1-2, 2-3, 3-4 and 4+.
        "spacing_under_1": 0.3333,
        "spacing_1_2": 0.1667,
        "spacing_2_3": 0.1667,
        "spacing_3_4": 0.1667,
        "spacing_4_up": 0.1667,
        "bpm": 0.6,  # 180 / 300
        "stars": 0.55,
    }


@pytest.mark.parametrize(
    ("bpm", "stars", "expected"),
    [
        (150, 5.0, (0.5, 0.5)),
        (300, 10.0, (1.0, 1.0)),  # edge: exactly at the cap
        (400, 13.4, (1.0, 1.0)),  # edge: above it, held at 1 so one number can't dominate
        (0, 0.0, (0.0, 0.0)),
        (-5, -1.0, (0.0, 0.0)),  # edge: never below 0, even for nonsense input
    ],
)
def test_bpm_and_stars_are_scaled_and_capped(
    bpm: float, stars: float, expected: tuple[float, float]
) -> None:
    vector = pattern_vector(objs("jump"), cs=4, bpm=bpm, star_rating=stars)

    assert (vector["bpm"], vector["stars"]) == expected


def test_an_empty_map_is_all_zeros_but_bpm_and_stars() -> None:
    vector = pattern_vector([], cs=4, bpm=150, star_rating=5.0)

    assert vector.values == (0, 0, 0, 0, 0, 0, 0, 0, 0, 0.5, 0.5)


def test_values_are_rounded_to_4_places() -> None:
    vector = pattern_vector(objs("jump", "other", "other"), cs=4, bpm=100, star_rating=1)

    assert vector["jump"] == 0.3333
    assert vector["bpm"] == 0.3333


def test_every_value_is_between_0_and_1() -> None:
    objects = [Obj("stream", 0.1 * CS4)] * 40 + [Obj("jump", 9 * CS4)] * 10

    vector = pattern_vector(objects, cs=0, bpm=999, star_rating=99)

    assert len(vector.values) == DIMENSIONS
    assert all(0 <= v <= 1 for v in vector.values)


def test_a_field_by_name() -> None:
    vector = PatternVector(tuple(float(i) for i in range(DIMENSIONS)))

    assert vector["jump"] == 0.0
    assert vector["stars"] == 10.0
    assert list(vector.as_dict()) == list(FIELDS)
    with pytest.raises(ValueError):
        vector["aim"]  # not a field


# ---------- A real map ----------


def test_map_22423() -> None:
    # Tori no Uta [Hard]: 140 BPM, 3.57 stars, CS 4; 254 objects.
    osu = parse_osu((FIXTURES / "22423.osu").read_text(encoding="utf-8"))
    features = object_features(osu)

    vector = pattern_vector(features, osu.difficulty.cs, bpm=140, star_rating=3.565)

    assert vector.values == (
        0.0,  # no jumps: a Hard map
        0.0709,  # 18 stream notes of 254
        0.3307,  # 84 sliders
        0.0,  # its streams are short bursts
        0.1371,
        0.4032,  # most gaps are 1-2 diameters
        0.2218,
        0.1411,
        0.0968,
        0.4667,  # 140 / 300
        0.3565,
    )
    assert sum(vector.values[4:9]) == pytest.approx(1, abs=1e-3)  # every gap in a band
