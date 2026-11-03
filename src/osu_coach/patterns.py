"""Per-object pattern features of a parsed map: timing, spacing, angle and a label.

The features describe the map as written (no mods). Mods change them in simple ways
(DT/HT scale times, HR mirrors y and raises CS), which the analysis applies on top.
"""

import math
from bisect import bisect_right
from dataclasses import dataclass
from typing import Literal

from osu_coach.osufile import Difficulty, HitKind, HitObject, OsuFile, TimingPoint

Pattern = Literal["jump", "stream", "slider", "spinner", "other"]

# A stream is a run of notes at 1/4 of a beat or faster; a jump is at 1/2 or faster.
# The small margin absorbs maps whose timing is a millisecond or two off the beat grid.
STREAM_BEAT_FRACTION = 0.25
JUMP_BEAT_FRACTION = 0.5
TIMING_MARGIN = 1.1
# In circle diameters: streams are packed close together, jumps far apart.
STREAM_MAX_SPACING = 1.5
JUMP_MIN_SPACING = 2.5


@dataclass(frozen=True)
class ObjectFeatures:
    idx: int  # position in the map, 0-based
    kind: HitKind
    time_ms: int
    end_time_ms: int  # sliders and spinners last a while; circles end where they start
    x: float
    y: float
    spacing: float | None  # osu! pixels from the previous object; None after a spinner
    angle: float | None  # degrees at the previous object, 0-180; None if not defined
    time_since_prev_ms: int | None  # start to start; None for the first object
    pattern: Pattern


def circle_radius(cs: float) -> float:
    """A circle's radius in osu! pixels for a Circle Size (CS), as osu! computes it."""
    return 54.4 - 4.48 * cs


def timing_at(points: tuple[TimingPoint, ...], time_ms: float) -> tuple[float, float]:
    """The beat length (ms per beat) and slider speed factor in effect at a time.

    The beat length comes from the latest uninherited point at or before `time_ms`; the
    speed factor from an inherited point after it (beat_length -50 means 2x speed). Before
    the first point, osu! uses the first one.
    """
    uninherited = [p for p in points if p.uninherited]
    if not uninherited:
        raise ValueError("a map needs at least one uninherited timing point")

    times = [p.time_ms for p in uninherited]
    base = uninherited[max(bisect_right(times, time_ms) - 1, 0)]

    speed = 1.0
    for p in points:
        if p.time_ms > time_ms:
            break
        if p.uninherited:
            speed = 1.0  # a new BPM section resets the slider speed
        elif p.time_ms >= base.time_ms and p.beat_length < 0:
            # osu! limits inherited speeds to 0.1x-10x.
            speed = min(max(-100.0 / p.beat_length, 0.1), 10.0)
    return base.beat_length, speed


def slider_duration_ms(
    obj: HitObject, difficulty: Difficulty, points: tuple[TimingPoint, ...]
) -> float:
    """How long a slider lasts, all repeats included.

    One pass covers `length` osu! pixels at `slider_multiplier * 100 * speed` pixels per
    beat, so it takes length / (that) beats.
    """
    beat_length, speed = timing_at(points, obj.time_ms)
    pixels_per_beat = difficulty.slider_multiplier * 100 * speed
    return obj.length / pixels_per_beat * beat_length * obj.slides


def end_time_ms(obj: HitObject, difficulty: Difficulty, points: tuple[TimingPoint, ...]) -> int:
    if obj.kind == "slider":
        return obj.time_ms + round(slider_duration_ms(obj, difficulty, points))
    if obj.kind == "spinner" and obj.end_time_ms is not None:
        return obj.end_time_ms
    return obj.time_ms


def angle_at(a: HitObject, b: HitObject, c: HitObject) -> float | None:
    """The angle at b, in degrees, between the moves a->b and b->c.

    180 means a straight line (back-and-forth jumps are near 0). None when two of the
    objects share a position, since there's no direction then.
    """
    v1 = (a.x - b.x, a.y - b.y)
    v2 = (c.x - b.x, c.y - b.y)
    n1, n2 = math.hypot(*v1), math.hypot(*v2)
    if n1 == 0 or n2 == 0:
        return None
    cos = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
    return math.degrees(math.acos(max(-1.0, min(1.0, cos))))  # clamp rounding errors


def classify(
    obj: HitObject,
    spacing: float | None,
    time_since_prev_ms: int | None,
    beat_length: float,
    diameter: float,
) -> Pattern:
    """A simple label for the move into this object, for grouping misses by pattern."""
    if obj.kind == "spinner":
        return "spinner"
    if obj.kind == "slider":
        return "slider"
    if spacing is None or time_since_prev_ms is None:
        return "other"
    if (
        time_since_prev_ms <= beat_length * STREAM_BEAT_FRACTION * TIMING_MARGIN
        and spacing <= STREAM_MAX_SPACING * diameter
    ):
        return "stream"
    if (
        time_since_prev_ms <= beat_length * JUMP_BEAT_FRACTION * TIMING_MARGIN
        and spacing >= JUMP_MIN_SPACING * diameter
    ):
        return "jump"
    return "other"


def object_features(osu: OsuFile) -> list[ObjectFeatures]:
    """Features for every hit object, in map order."""
    objects = osu.hit_objects
    diameter = 2 * circle_radius(osu.difficulty.cs)
    result: list[ObjectFeatures] = []
    for i, obj in enumerate(objects):
        prev = objects[i - 1] if i >= 1 else None
        before = objects[i - 2] if i >= 2 else None

        time_since = obj.time_ms - prev.time_ms if prev else None
        # A spinner sits at the centre whatever the map says, so distances to or from one
        # mean nothing; the player's cursor is wherever the spin ended.
        positioned = prev is not None and prev.kind != "spinner" and obj.kind != "spinner"
        spacing = math.hypot(obj.x - prev.x, obj.y - prev.y) if positioned and prev else None
        angle = (
            angle_at(before, prev, obj)
            if positioned and prev and before and before.kind != "spinner"
            else None
        )
        beat_length, _ = timing_at(osu.timing_points, obj.time_ms)

        result.append(
            ObjectFeatures(
                idx=i,
                kind=obj.kind,
                time_ms=obj.time_ms,
                end_time_ms=end_time_ms(obj, osu.difficulty, osu.timing_points),
                x=obj.x,
                y=obj.y,
                spacing=spacing,
                angle=angle,
                time_since_prev_ms=time_since,
                pattern=classify(obj, spacing, time_since, beat_length, diameter),
            )
        )
    return result
