"""Unit tests for osu_coach.difficulty.

Expected values are osu!'s published ones (the osu! wiki's hit window and approach rate
tables, and well-known speed-mod conversions such as AR 10 + DT = AR 11). osu!'s API no
longer reports AR or OD with mods, so there's no live service to check against.
"""

import pytest

from osu_coach.difficulty import (
    MISS_WINDOW_MS,
    NEUTRAL_MODS,
    STABLE_RADIUS_FUDGE,
    HitWindows,
    UnsupportedModsError,
    difficulty_range,
    play_conditions,
)
from osu_coach.osufile import Difficulty


def difficulty(cs: float = 4, ar: float = 9, od: float = 8, hp: float = 5) -> Difficulty:
    return Difficulty(hp=hp, cs=cs, od=od, ar=ar, slider_multiplier=1.4, slider_tick_rate=1)


# ---------- difficulty_range ----------


@pytest.mark.parametrize(
    ("value", "want"),
    [
        (0, 1800),  # low end
        (2.5, 1500),  # halfway to the middle
        (5, 1200),  # the middle
        (7.5, 825),  # the second half has its own slope
        (10, 450),  # high end
        (11, 300),  # past 10 keeps the slope (osu! allows this for DT's effective values)
    ],
)
def test_difficulty_range(value: float, want: float) -> None:
    assert difficulty_range(value, 1800, 1200, 450) == pytest.approx(want)


# ---------- Hit windows ----------


@pytest.mark.parametrize(
    ("od", "great", "ok", "meh"),
    [
        # The osu! wiki's table (80 - 6 OD, 140 - 8 OD, 200 - 10 OD), each rounded down to
        # whole ms, minus half a ms.
        (0, 79.5, 139.5, 199.5),
        (5, 49.5, 99.5, 149.5),
        (8, 31.5, 75.5, 119.5),
        (9.5, 22.5, 63.5, 104.5),  # 23, 64, 105 before rounding: whole already
        (9.2, 23.5, 65.5, 107.5),  # 24.8, 66.4, 108: rounded down first
        (10, 19.5, 59.5, 99.5),
    ],
)
def test_hit_windows(od: float, great: float, ok: float, meh: float) -> None:
    w = HitWindows.for_od(od)

    assert (w.great, w.ok, w.meh, w.miss) == (great, ok, meh, MISS_WINDOW_MS)


@pytest.mark.parametrize(
    ("error_ms", "result"),
    [
        # OD 10: 300 within 19.5 ms, 100 within 59.5, 50 within 99.5, either way.
        (0, "300"),
        (19, "300"),
        (-19, "300"),
        (20, "100"),  # a whole ms past the 300 window
        (-59, "100"),
        (60, "50"),
        (-99.5, "50"),  # the edge counts
        (99.6, None),  # late past the 50 window: no press result (the object was missed)
        (-100, "miss"),  # early past the 50 window, but inside 400 ms: the press is a miss
        (-400, "miss"),
        (-400.1, None),  # too early to count at all: osu! ignores the press
        (400, None),
    ],
)
def test_result(error_ms: float, result: str | None) -> None:
    assert HitWindows.for_od(10).result(error_ms) == result


# ---------- Mods ----------


def test_no_mods() -> None:
    c = play_conditions(difficulty(cs=4, ar=9, od=8, hp=5), ())

    assert (c.cs, c.ar, c.od, c.hp, c.rate, c.flip_y) == (4, 9, 8, 5, 1.0, False)
    assert c.windows == HitWindows.for_od(8)
    assert c.preempt_ms == 600  # AR 9


@pytest.mark.parametrize(
    ("mods", "cs", "ar", "od", "hp"),
    [
        (("HR",), 5.2, 10, 10, 7),  # CS x1.3, AR/OD/HP x1.4 capped at 10 (9 x 1.4 = 12.6)
        (("EZ",), 2, 4.5, 4, 2.5),  # everything halved
    ],
)
def test_hr_and_ez(mods: tuple[str, ...], cs: float, ar: float, od: float, hp: float) -> None:
    c = play_conditions(difficulty(cs=4, ar=9, od=8, hp=5), mods)

    assert (c.cs, c.ar, c.od, c.hp) == pytest.approx((cs, ar, od, hp))
    assert c.windows == HitWindows.for_od(od)
    # Circles are smaller with HR and bigger with EZ: the radius follows the new CS.
    assert c.radius == pytest.approx((54.4 - 4.48 * cs) * STABLE_RADIUS_FUDGE)


def test_hr_caps_cs_at_10() -> None:
    assert play_conditions(difficulty(cs=8), ("HR",)).cs == 10  # 10.4 capped


@pytest.mark.parametrize(
    ("mods", "flip"),
    [(("HR",), True), (("HD", "HR"), True), ((), False), (("EZ",), False)],
)
def test_hr_flips_the_map(mods: tuple[str, ...], flip: bool) -> None:
    assert play_conditions(difficulty(), mods).flip_y is flip


@pytest.mark.parametrize(
    ("mods", "rate"),
    [((), 1.0), (("DT",), 1.5), (("NC",), 1.5), (("HT",), 0.75), (("DC",), 0.75)],
)
def test_speed(mods: tuple[str, ...], rate: float) -> None:
    c = play_conditions(difficulty(od=8), mods)

    assert c.rate == rate
    # Windows are in song time: speed mods don't change them there.
    assert c.windows == HitWindows.for_od(8)


@pytest.mark.parametrize(
    ("ar", "od", "mods", "effective_ar", "effective_od"),
    [
        (9, 8, (), 9, 8),  # no speed mod: as written
        (10, 10, ("DT",), 11, 11.11),  # well known: AR 10 + DT = AR 11, OD 10 + DT = 11.11
        (9, 9, ("DT",), 10.33, 10.44),  # AR 9 + DT = 10.33
        (8, 8, ("DT",), 9.67, 9.78),
        (10, 10, ("HT",), 9, 8.89),  # HT: AR 10 plays like AR 9
        (9, 8, ("HR", "DT"), 11, 11.11),  # HR first (AR 9 -> 10, OD 8 -> 10), then DT
        (5, 5, ("HT",), 1.67, 2.22),  # 1600 ms: below AR 5 the scale has another slope
    ],
)
def test_effective_ar_and_od(
    ar: float, od: float, mods: tuple[str, ...], effective_ar: float, effective_od: float
) -> None:
    c = play_conditions(difficulty(ar=ar, od=od), mods)

    assert c.effective_ar == pytest.approx(effective_ar, abs=0.01)
    assert c.effective_od == pytest.approx(effective_od, abs=0.01)


@pytest.mark.parametrize(
    ("ar", "preempt"),
    [(0, 1800), (5, 1200), (9, 600), (9.7, 495), (10, 450)],  # the osu! wiki's AR table
)
def test_approach_time(ar: float, preempt: float) -> None:
    assert play_conditions(difficulty(ar=ar), ()).preempt_ms == pytest.approx(preempt)


@pytest.mark.parametrize(
    ("cs", "radius"),
    [(0, 54.4), (2, 45.44), (4, 36.48), (5, 32), (7, 23.04), (10, 9.6)],  # 54.4 - 4.48 CS
)
def test_radius(cs: float, radius: float) -> None:
    c = play_conditions(difficulty(cs=cs), ())

    assert c.radius == pytest.approx(radius * STABLE_RADIUS_FUDGE)


# ---------- Which mods can be analyzed ----------


@pytest.mark.parametrize("mod", sorted(NEUTRAL_MODS))
def test_neutral_mods_change_nothing(mod: str) -> None:
    assert play_conditions(difficulty(), (mod,)) == play_conditions(difficulty(), ())


@pytest.mark.parametrize(
    ("mods", "message"),
    [
        (("RX",), "not supported for analysis: RX"),  # Relax clicks for the player
        (("AP",), "not supported for analysis: AP"),  # Autopilot aims for the player
        (("AT",), "not supported for analysis: AT"),
        (("MR",), "not supported for analysis: MR"),  # Mirror moves the notes
        (("DA",), "not supported for analysis: DA"),  # lazer's custom difficulty
        (("WU",), "not supported for analysis: WU"),  # Wind Up: the speed changes over time
        (("HD", "RX", "AP"), "not supported for analysis: AP, RX"),  # all named, sorted
        (("HR", "EZ"), "HR and EZ can't be played together"),
        (("DT", "HT"), "only one speed mod"),
        (("DT", "NC"), "only one speed mod"),
    ],
)
def test_unsupported_mods(mods: tuple[str, ...], message: str) -> None:
    with pytest.raises(UnsupportedModsError, match=message):
        play_conditions(difficulty(), mods)


def test_mods_can_be_a_list() -> None:
    # The replays table stores mods as a list; the parser gives a tuple.
    assert play_conditions(difficulty(), ["HR"]) == play_conditions(difficulty(), ("HR",))
