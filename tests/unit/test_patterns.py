"""Unit tests for osu_coach.patterns: per-object timing, spacing, angle and labels.

The two real maps in tests/fixtures/osu/ check the slider timing against the maps
themselves (sliders end on the beat grid and never run into the next note); small
hand-built maps check each rule on its own.
"""

import math
from itertools import pairwise

import pytest

from osu_coach.osufile import Difficulty, HitObject, OsuFile, TimingPoint, parse_osu
from osu_coach.patterns import (
    ObjectFeatures,
    angle_at,
    circle_radius,
    classify,
    end_time_ms,
    object_features,
    slider_duration_ms,
    timing_at,
)
from tests.support import FIXTURES

MAP_22423 = parse_osu((FIXTURES / "22423.osu").read_text(encoding="utf-8"))  # 140 BPM
MAP_75 = parse_osu((FIXTURES / "75.osu").read_text(encoding="utf-8"))  # 120 BPM

BEAT = 500.0  # 120 BPM, for the hand-built maps
DIFF = Difficulty(hp=5, cs=4, od=8, ar=9, slider_multiplier=1.0, slider_tick_rate=1)
DIAMETER = 2 * circle_radius(4)  # 72.96 osu! pixels


def circle(x: float, y: float, t: int, combo: bool = False) -> HitObject:
    return HitObject("circle", x, y, t, combo)


def a_map(*objects: HitObject, points: tuple[TimingPoint, ...] | None = None) -> OsuFile:
    timing = points if points is not None else (TimingPoint(0, BEAT, True),)
    return OsuFile(14, DIFF, timing, tuple(objects))


# ---------- circle_radius ----------


@pytest.mark.parametrize(
    ("cs", "radius"),
    [(0, 54.4), (4, 36.48), (5, 32.0), (7, 23.04), (10, 9.6)],
)
def test_circle_radius(cs: float, radius: float) -> None:
    # osu!'s formula: higher CS, smaller circles.
    assert circle_radius(cs) == pytest.approx(radius)


# ---------- timing_at ----------

SECTIONS = (
    TimingPoint(1000, 500, True),  # 120 BPM from 1 s
    TimingPoint(2000, -50, False),  # 2x slider speed from 2 s
    TimingPoint(3000, 400, True),  # 150 BPM from 3 s: resets the speed
    TimingPoint(4000, -200, False),  # 0.5x from 4 s
)


@pytest.mark.parametrize(
    ("time_ms", "want"),
    [
        (0, (500, 1.0)),  # before the first point: osu! uses the first one
        (1000, (500, 1.0)),  # exactly on a point counts
        (1500, (500, 1.0)),
        (2000, (500, 2.0)),  # the inherited point doubles the slider speed
        (2999, (500, 2.0)),
        (3000, (400, 1.0)),  # a new BPM section resets the speed
        (4500, (400, 0.5)),
    ],
)
def test_timing_at(time_ms: float, want: tuple[float, float]) -> None:
    assert timing_at(SECTIONS, time_ms) == pytest.approx(want)


@pytest.mark.parametrize(
    ("inherited_beat_length", "speed"),
    [
        (-100, 1.0),
        (-50, 2.0),
        (-5, 10.0),  # 20x would be possible in the file; osu! caps it at 10x
        (-2000, 0.1),  # 0.05x, capped at 0.1x
    ],
)
def test_slider_speed_is_capped(inherited_beat_length: float, speed: float) -> None:
    points = (TimingPoint(0, 500, True), TimingPoint(100, inherited_beat_length, False))

    assert timing_at(points, 200)[1] == pytest.approx(speed)


def test_inherited_point_from_an_earlier_section_doesnt_carry_over() -> None:
    points = (
        TimingPoint(0, 500, True),
        TimingPoint(500, -50, False),
        TimingPoint(1000, 400, True),
    )

    assert timing_at(points, 1500) == (400, 1.0)


def test_a_map_needs_an_uninherited_point() -> None:
    with pytest.raises(ValueError):
        timing_at((TimingPoint(0, -50, False),), 100)


# ---------- Slider duration and end times ----------


@pytest.mark.parametrize(
    ("length", "slides", "points", "want"),
    [
        # 100 px per beat at multiplier 1.0: 100 px = 1 beat = 500 ms.
        (100.0, 1, (TimingPoint(0, BEAT, True),), 500.0),
        (100.0, 3, (TimingPoint(0, BEAT, True),), 1500.0),  # repeats add passes
        (50.0, 1, (TimingPoint(0, BEAT, True),), 250.0),
        # 2x speed: the same length takes half as long.
        (100.0, 1, (TimingPoint(0, BEAT, True), TimingPoint(0, -50, False)), 250.0),
        # Faster BPM: a shorter beat.
        (100.0, 1, (TimingPoint(0, 300, True),), 300.0),
    ],
)
def test_slider_duration(
    length: float, slides: int, points: tuple[TimingPoint, ...], want: float
) -> None:
    slider = HitObject("slider", 0, 0, 100, False, "L", ((100, 0),), slides, length)

    assert slider_duration_ms(slider, DIFF, points) == pytest.approx(want)


def test_end_times() -> None:
    points = (TimingPoint(0, BEAT, True),)
    slider = HitObject("slider", 0, 0, 1000, False, "L", ((100, 0),), 1, 100.0)
    spinner = HitObject("spinner", 256, 192, 1000, False, end_time_ms=4000)

    assert end_time_ms(circle(0, 0, 1000), DIFF, points) == 1000
    assert end_time_ms(slider, DIFF, points) == 1500
    assert end_time_ms(spinner, DIFF, points) == 4000


@pytest.mark.parametrize("osu", [MAP_22423, MAP_75], ids=["22423", "75"])
def test_real_sliders_never_run_into_the_next_note(osu: OsuFile) -> None:
    features = object_features(osu)

    overlaps = [
        (a.idx, a.end_time_ms, b.time_ms)
        for a, b in pairwise(features)
        if a.kind == "slider" and a.end_time_ms > b.time_ms
    ]
    assert overlaps == []


@pytest.mark.parametrize(
    ("osu", "beat_fraction"),
    [
        (MAP_22423, 428.571428571429 / 4),  # 140 BPM, quarter beats
        (MAP_75, 500.004999999999 / 4),  # 120 BPM, quarter beats
    ],
    ids=["22423", "75"],
)
def test_real_sliders_end_on_the_beat_grid(osu: OsuFile, beat_fraction: float) -> None:
    # Mappers place the next note on the grid, and sliders are normally a whole number of
    # grid steps long, so a correct slider end lands on the grid too. A wrong formula
    # would put most ends off it. Old maps allow freely drawn lengths, so a few may not.
    features = object_features(osu)
    gaps = [b.time_ms - a.end_time_ms for a, b in pairwise(features) if a.kind == "slider"]
    on_grid = [abs(g / beat_fraction - round(g / beat_fraction)) < 0.05 for g in gaps]

    assert len(gaps) >= 30
    assert sum(on_grid) / len(on_grid) >= 0.95, f"{sum(on_grid)} of {len(gaps)} on the grid"


def test_a_freely_drawn_old_slider() -> None:
    # Map 75's slider 162 (2007) is 408 px long: 408 / 140 px per beat x 2 passes = 5.83
    # beats, so it ends off the grid. osu! times it the same way: 126119 + 2914 ms.
    slider = object_features(MAP_75)[162]

    assert (slider.kind, slider.time_ms, slider.end_time_ms) == ("slider", 126119, 129033)


# ---------- angle_at ----------


@pytest.mark.parametrize(
    ("a", "b", "c", "want"),
    [
        ((0, 0), (100, 0), (200, 0), 180.0),  # straight line
        ((0, 0), (100, 0), (0, 0), 0.0),  # straight back
        ((0, 0), (100, 0), (100, 100), 90.0),  # right angle
        ((0, 0), (100, 0), (200, 100), 135.0),
        ((0, 0), (0, 0), (100, 0), None),  # stacked notes: no direction
        ((0, 0), (100, 0), (100, 0), None),
    ],
)
def test_angle_at(
    a: tuple[float, float], b: tuple[float, float], c: tuple[float, float], want: float | None
) -> None:
    got = angle_at(circle(*a, 0), circle(*b, 1), circle(*c, 2))

    assert got == (pytest.approx(want) if want is not None else None)


@pytest.mark.parametrize(
    ("a", "c"),
    [
        # From b=(0, 0), the moves to a and c point the same way, but in floating point
        # their cos comes out as 1.0000000000000002, and acos() of that raises an error.
        ((1, 5), (2, 10)),
        ((1, 6), (3, 18)),
    ],
)
def test_angle_survives_rounding(a: tuple[float, float], c: tuple[float, float]) -> None:
    got = angle_at(circle(*a, 0), circle(0, 0, 1), circle(*c, 2))

    assert got == pytest.approx(0.0)


# ---------- classify ----------


@pytest.mark.parametrize(
    ("kind", "spacing", "since", "want"),
    [
        ("circle", 30.0, 125, "stream"),  # 1/4 beat, close together
        ("circle", 1.5 * DIAMETER, 125, "stream"),  # at the spacing limit
        ("circle", 30.0, 137, "stream"),  # within the 10% timing margin
        ("circle", 30.0, 140, "other"),  # past it
        ("circle", 200.0, 250, "jump"),  # 1/2 beat, far apart
        ("circle", 2.5 * DIAMETER, 250, "jump"),  # at the spacing limit
        ("circle", 200.0, 125, "jump"),  # fast and far: a jump, not a stream
        ("circle", 200.0, 500, "other"),  # far but slow: plenty of time
        ("circle", 100.0, 250, "other"),  # neither close nor far
        ("circle", None, None, "other"),  # the first object
        ("slider", 30.0, 125, "slider"),
        ("spinner", None, 500, "spinner"),
    ],
)
def test_classify(kind: str, spacing: float | None, since: int | None, want: str) -> None:
    obj = HitObject(kind, 0, 0, 0, False)  # type: ignore[arg-type]

    assert classify(obj, spacing, since, BEAT, DIAMETER) == want


# ---------- object_features ----------


def test_a_stream() -> None:
    notes = [circle(100 + 20 * i, 100, 1000 + 125 * i) for i in range(5)]  # 1/4 beats

    patterns = [f.pattern for f in object_features(a_map(*notes))]

    assert patterns == ["other", "stream", "stream", "stream", "stream"]


def test_back_and_forth_jumps() -> None:
    notes = [circle(50 if i % 2 == 0 else 450, 200, 1000 + 250 * i) for i in range(4)]

    features = object_features(a_map(*notes))

    assert [f.pattern for f in features] == ["other", "jump", "jump", "jump"]
    assert [f.spacing for f in features] == [None, 400, 400, 400]
    assert [f.angle for f in features] == [None, None, 0, 0]  # straight back each time


def test_first_object_has_no_previous() -> None:
    first = object_features(a_map(circle(10, 10, 100), circle(20, 10, 200)))[0]

    assert (first.spacing, first.angle, first.time_since_prev_ms) == (None, None, None)


def test_spinners_have_no_spacing_or_angle() -> None:
    spinner = HitObject("spinner", 256, 192, 2000, True, end_time_ms=4000)
    objects = [circle(0, 0, 1000), circle(100, 0, 1500), spinner, circle(300, 0, 4500)]

    features = object_features(a_map(*objects))

    # The spinner and the note after it: the cursor is wherever the spin ended.
    assert [(f.spacing, f.angle) for f in features[2:]] == [(None, None), (None, None)]
    assert features[3].time_since_prev_ms == 2500  # still start to start
    assert features[2].pattern == "spinner"


def test_features_of_map_22423() -> None:
    features = object_features(MAP_22423)

    assert [f.idx for f in features] == list(range(254))
    first, second, third = features[:3]
    assert first == ObjectFeatures(0, "slider", 14388, 14817, 40, 272, None, None, None, "slider")
    assert (second.kind, second.time_since_prev_ms) == ("circle", 643)
    assert second.spacing == pytest.approx(math.hypot(248 - 40, 224 - 272))
    assert third.angle is not None and 0 <= third.angle <= 180


@pytest.mark.parametrize(
    ("osu", "want"),
    [
        (MAP_22423, {"slider": 84, "spinner": 3, "stream": 18, "other": 149}),
        (MAP_75, {"slider": 30, "spinner": 4, "stream": 2, "other": 158}),
    ],
    ids=["22423", "75"],
)
def test_pattern_counts_of_real_maps(osu: OsuFile, want: dict[str, int]) -> None:
    counts: dict[str, int] = {}
    for f in object_features(osu):
        counts[f.pattern] = counts.get(f.pattern, 0) + 1

    assert counts == want


@pytest.mark.parametrize("osu", [MAP_22423, MAP_75], ids=["22423", "75"])
def test_every_value_is_in_range(osu: OsuFile) -> None:
    for f in object_features(osu):
        assert f.end_time_ms >= f.time_ms
        assert f.spacing is None or f.spacing >= 0
        assert f.angle is None or 0 <= f.angle <= 180
        assert f.time_since_prev_ms is None or f.time_since_prev_ms >= 0
