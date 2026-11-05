"""Judge a replay hit by hit: which press hit which circle, when, and with what result.

Follows osu!lazer's rules for osu! standard without the Classic mod, where every circle
and slider head is judged like a circle (300/100/50/miss by timing) and gives the play's
300/100/50/miss counts. Slider ticks and tails only affect combo; spinners come later.

How a press is judged, as lazer does it:
- A press is a key going down: either "left" (M1 or K1) or "right" (M2 or K2). Pressing
  K1 while M1 is held isn't a new press, since both are "left".
- It goes to the earliest unjudged circle that has appeared and is under the cursor.
- Its timing gives the result (difficulty.HitWindows.result). Too early to count, and
  the press is used up without a result (the circle shakes).
- Note lock: while an earlier circle is still waiting to be hit, a press on a later one
  only counts once the earlier one's time has come; before that it shakes. A hit then
  misses every earlier circle still waiting.
- A circle not hit by the end of its 50 window is a miss.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from osu_coach.difficulty import Conditions, HitResult
from osu_coach.osrfile import Frame
from osu_coach.osufile import OsuFile

PLAYFIELD_HEIGHT = 384
LEFT = 1 | 4  # M1, K1 (K1 also sets M1's bit)
RIGHT = 2 | 8  # M2, K2
# Maps in file formats before v5 play 24 ms later than their written times (osu! adds it
# when loading them; lazer's LegacyBeatmapDecoder does the same).
EARLY_FORMAT_OFFSET_MS = 24


@dataclass(frozen=True)
class Judgement:
    idx: int  # the hit object's place in the map, from 0
    kind: str  # "circle" or "slider" (its head)
    time_ms: int  # when it should be hit (song time, offset applied)
    result: HitResult
    hit_ms: int | None = None  # when the press that hit it came; None if never hit
    error_ms: int | None = None  # hit_ms - time_ms: negative is early
    distance_px: float | None = None  # cursor to circle center at the hit (aim error)
    forced: bool = False  # missed because a later circle was hit first (note lock)


@dataclass
class _Target:
    idx: int
    kind: str
    time_ms: int
    x: float
    y: float
    judgement: Judgement | None = None


def judge(osu: OsuFile, frames: Sequence[Frame], conditions: Conditions) -> list[Judgement]:
    """One Judgement per circle and slider head, in map order. Spinners aren't judged yet."""
    offset = EARLY_FORMAT_OFFSET_MS if osu.format_version < 5 else 0
    targets = [
        _Target(
            idx=i,
            kind=h.kind,
            time_ms=h.time_ms + offset,
            x=h.x,
            y=PLAYFIELD_HEIGHT - h.y if conditions.flip_y else h.y,
        )
        for i, h in enumerate(osu.hit_objects)
        if h.kind != "spinner"
    ]
    windows = conditions.windows
    first_open = 0  # targets before this are all judged

    def miss_late_ones(now: int) -> None:
        # Anything still waiting past its 50 window is a miss.
        nonlocal first_open
        for t in targets[first_open:]:
            if t.time_ms + windows.meh >= now:
                break
            if t.judgement is None:
                t.judgement = Judgement(t.idx, t.kind, t.time_ms, "miss")
        while first_open < len(targets) and targets[first_open].judgement is not None:
            first_open += 1

    def press(now: int, x: float, y: float) -> None:
        # The earliest unjudged circle that has appeared and is under the cursor.
        target = None
        for t in targets[first_open:]:
            if t.time_ms - conditions.preempt_ms > now:
                break  # not on screen yet, nor anything after it
            if t.judgement is None and math.dist((x, y), (t.x, t.y)) <= conditions.radius:
                target = t
                break
        if target is None:
            return

        result = windows.result(now - target.time_ms)
        if result is None:
            return  # too early: the circle shakes, the press is used up
        earlier = [t for t in targets[first_open:] if t.time_ms < target.time_ms]
        # Note lock, as lazer's StartTimeOrderedHitPolicy: the latest earlier circle must
        # be judged, or its time must have come. Otherwise the circle shakes.
        if earlier and earlier[-1].judgement is None and now < earlier[-1].time_ms:
            return
        waiting = [t for t in earlier if t.judgement is None]

        target.judgement = Judgement(
            target.idx,
            target.kind,
            target.time_ms,
            result,
            hit_ms=now,
            error_ms=now - target.time_ms,
            distance_px=math.dist((x, y), (target.x, target.y)),
        )
        for t in waiting:  # hitting a later circle misses the earlier ones
            t.judgement = Judgement(t.idx, t.kind, t.time_ms, "miss", forced=True)

    held = 0
    for frame in frames:
        miss_late_ones(frame.time_ms)
        for button in (LEFT, RIGHT):
            down = bool(frame.keys & button)
            if down and not held & button:
                press(frame.time_ms, frame.x, frame.y)
            held = held | button if down else held & ~button

    for t in targets:  # never hit by the end of the replay
        if t.judgement is None:
            t.judgement = Judgement(t.idx, t.kind, t.time_ms, "miss")
    return [t.judgement for t in targets if t.judgement is not None]


def counts(judgements: Sequence[Judgement]) -> dict[HitResult, int]:
    """How many of each result, like the counts osu! shows for a play."""
    totals: dict[HitResult, int] = {"300": 0, "100": 0, "50": 0, "miss": 0}
    for j in judgements:
        totals[j.result] += 1
    return totals
