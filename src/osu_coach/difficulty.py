"""The conditions a replay was played under: the map's difficulty after mods, hit windows,
circle size and approach time, as osu!lazer computes them for osu! standard.

Hit windows are in song time, the clock of the .osu file and the replay frames. Speed mods
(DT, HT) don't change them there; they only make the song, and so every window, pass
faster or slower in real time. `effective_od` and `effective_ar` give what a player feels.
"""

import math
from dataclasses import dataclass
from typing import Literal

from osu_coach.osufile import Difficulty
from osu_coach.patterns import circle_radius

HitResult = Literal["300", "100", "50", "miss"]

# Mods that don't change where or when notes are, or how they're judged. Other mods
# (Relax, Autopilot, Mirror, lazer's custom difficulty and fun mods...) would make the
# analysis wrong, so they're refused instead of ignored.
NEUTRAL_MODS = frozenset({"NF", "SD", "PF", "HD", "FL", "TD", "SO", "CL"})
SPEED = {"DT": 1.5, "NC": 1.5, "HT": 0.75, "DC": 0.75}  # DC: lazer's Daycore

# stable's playfield is a little larger than its numbers say; lazer copies that so
# circles are the same size in both (LegacyRulesetExtensions: 1.00041).
STABLE_RADIUS_FUDGE = 1.00041
MISS_WINDOW_MS = 400.0  # a press this early still counts, as a miss


class UnsupportedModsError(ValueError):
    """The replay uses mods whose effect on hit judgements isn't modelled."""


def difficulty_range(value: float, low: float, mid: float, high: float) -> float:
    """osu!'s scale for a 0-10 difficulty value: `low` at 0, `mid` at 5, `high` at 10."""
    if value > 5:
        return mid + (high - mid) * (value - 5) / 5
    if value < 5:
        return mid + (low - mid) * (5 - value) / 5
    return mid


@dataclass(frozen=True)
class HitWindows:
    """How far from an object's time (either way, in ms of song time) each result counts."""

    great: float  # a 300
    ok: float  # a 100
    meh: float  # a 50
    miss: float = MISS_WINDOW_MS

    @classmethod
    def for_od(cls, od: float) -> "HitWindows":
        # lazer rounds each window down to whole ms, then takes off half a ms: with whole-ms
        # times, a 26 ms window at OD 9 then means "at most 25 ms off", as in stable.
        def window(low: float, mid: float, high: float) -> float:
            return math.floor(difficulty_range(od, low, mid, high)) - 0.5

        return cls(great=window(80, 50, 20), ok=window(140, 100, 60), meh=window(200, 150, 100))

    def result(self, error_ms: float) -> HitResult | None:
        """The result of a press `error_ms` from the object's time (negative is early).

        None for a press too early to count at all: osu! ignores it (the note shakes).
        """
        off = abs(error_ms)
        if off <= self.great:
            return "300"
        if off <= self.ok:
            return "100"
        if off <= self.meh:
            return "50"
        if error_ms < 0 and off <= self.miss:
            return "miss"  # early, but close enough that the press is used up
        return None


@dataclass(frozen=True)
class Conditions:
    cs: float  # after HR/EZ
    ar: float
    od: float
    hp: float
    rate: float  # how fast the song plays: 1.5 with DT, 0.75 with HT
    flip_y: bool  # HR flips the map upside down
    radius: float  # a circle's hit radius, osu! pixels
    preempt_ms: float  # how long before its time an object appears (song time)
    windows: HitWindows

    @property
    def effective_od(self) -> float:
        """The OD whose 300 window, in real time, matches this one at this speed."""
        return (80 - (80 - 6 * self.od) / self.rate) / 6

    @property
    def effective_ar(self) -> float:
        """The AR whose approach time, in real time, matches this one at this speed."""
        preempt = self.preempt_ms / self.rate
        if preempt > 1200:
            return 5 - (preempt - 1200) / 120
        return 5 + (1200 - preempt) / 150


def play_conditions(difficulty: Difficulty, mods: tuple[str, ...] | list[str]) -> Conditions:
    """The map's difficulty as played with these mods (acronyms, as a replay lists them)."""
    unsupported = sorted(set(mods) - NEUTRAL_MODS - SPEED.keys() - {"HR", "EZ"})
    if unsupported:
        raise UnsupportedModsError(f"mods not supported for analysis: {', '.join(unsupported)}")
    if "HR" in mods and "EZ" in mods:
        raise UnsupportedModsError("HR and EZ can't be played together")
    speeds = [SPEED[m] for m in mods if m in SPEED]
    if len(speeds) > 1:
        raise UnsupportedModsError("only one speed mod can be played at a time")

    cs, ar, od, hp = difficulty.cs, difficulty.ar, difficulty.od, difficulty.hp
    if "HR" in mods:  # harder: CS x1.3, the rest x1.4, none above 10
        cs, ar, od, hp = min(cs * 1.3, 10), min(ar * 1.4, 10), min(od * 1.4, 10), min(hp * 1.4, 10)
    if "EZ" in mods:  # easier: everything halved
        cs, ar, od, hp = cs / 2, ar / 2, od / 2, hp / 2

    return Conditions(
        cs=cs,
        ar=ar,
        od=od,
        hp=hp,
        rate=speeds[0] if speeds else 1.0,
        flip_y="HR" in mods,
        radius=circle_radius(cs) * STABLE_RADIUS_FUDGE,
        preempt_ms=difficulty_range(ar, 1800, 1200, 450),
        windows=HitWindows.for_od(od),
    )
