"""Unit tests for osu_coach.judge.

Small maps and presses are built by hand, so each rule can be checked alone. The map is
OD 10 (300 within 19.5 ms, 100 within 59.5, 50 within 99.5), CS 4 (radius about 36.5 px)
and AR 10 (objects appear 450 ms early). Real replays checked against their official
counts are in tests/integration/test_judge_live.py.
"""

from collections.abc import Sequence

import pytest

from osu_coach.difficulty import play_conditions
from osu_coach.judge import EARLY_FORMAT_OFFSET_MS, Judgement, counts, judge
from osu_coach.osrfile import Frame
from osu_coach.osufile import Difficulty, HitKind, HitObject, OsuFile

M1, M2, K1, K2, SMOKE = 1, 2, 5, 10, 16  # as replays store keys (K1 also sets M1's bit)
P, Q = (100.0, 100.0), (400.0, 300.0)  # two places far apart


def obj(time_ms: int, at: tuple[float, float] = P, kind: HitKind = "circle") -> HitObject:
    return HitObject(kind=kind, x=at[0], y=at[1], time_ms=time_ms, new_combo=False)


def osu_file(*objects: HitObject, version: int = 14) -> OsuFile:
    diff = Difficulty(hp=5, cs=4, od=10, ar=10, slider_multiplier=1.4, slider_tick_rate=1)
    return OsuFile(version, diff, (), tuple(objects))


def tap(time_ms: int, at: tuple[float, float] = P, key: int = M1) -> list[Frame]:
    """A key going down at `time_ms` and up 20 ms later, with the cursor at `at`."""
    return [Frame(time_ms, *at, key), Frame(time_ms + 20, *at, 0)]


def run(
    osu: OsuFile, *taps: list[Frame], mods: Sequence[str] = (), end: int = 100_000
) -> list[Judgement]:
    frames = sorted((f for t in taps for f in t), key=lambda f: f.time_ms)
    frames.append(Frame(end, 0, 0, 0))  # the replay runs on past the last object
    return judge(osu, frames, play_conditions(osu.difficulty, list(mods)))


def results(judgements: Sequence[Judgement]) -> list[str]:
    return [j.result for j in judgements]


# ---------- Timing ----------


@pytest.mark.parametrize(
    ("press_ms", "result"),
    [
        (1000, "300"),
        (1019, "300"),  # inside 19.5 ms
        (981, "300"),
        (1020, "100"),  # just outside
        (940, "50"),  # 60 ms early
        (1099, "50"),
        (900, "miss"),  # 100 ms early: inside 400 ms, so the press is used up as a miss
        (600, "miss"),
    ],
)
def test_timing(press_ms: int, result: str) -> None:
    [j] = run(osu_file(obj(1000)), tap(press_ms))

    assert j.result == result
    assert (j.hit_ms, j.error_ms, j.distance_px) == (press_ms, press_ms - 1000, 0)
    assert not j.forced


def test_a_press_too_early_is_ignored() -> None:
    # 401 ms early doesn't count at all; the on-time press after it still hits.
    [j] = run(osu_file(obj(1000)), tap(599), tap(1005))

    assert (j.result, j.error_ms) == ("300", 5)


def test_a_circle_never_pressed_is_a_miss() -> None:
    [j] = run(osu_file(obj(1000)))

    assert j == Judgement(0, "circle", 1000, "miss")  # no press, no error, not forced


def test_a_press_too_late_finds_the_circle_missed() -> None:
    # 100 ms late: the 50 window (99.5 ms) has passed, so the circle was already missed.
    [j] = run(osu_file(obj(1000)), tap(1100))

    assert (j.result, j.hit_ms) == ("miss", None)


def test_the_replay_ending_early_misses_the_rest() -> None:
    js = run(osu_file(obj(1000), obj(5000)), tap(1000), end=2000)

    assert results(js) == ["300", "miss"]


# ---------- Aim ----------


@pytest.mark.parametrize(
    ("offset", "hit"),
    [
        (0, True),
        (30, True),
        (36.4, True),  # the radius is 36.48 x 1.00041 = 36.495
        (36.6, False),  # just outside
        (80, False),
    ],
)
def test_the_cursor_must_be_on_the_circle(offset: float, hit: bool) -> None:
    [j] = run(osu_file(obj(1000)), tap(1000, (P[0] + offset, P[1])))

    assert j.result == ("300" if hit else "miss")
    if hit:
        assert j.distance_px == pytest.approx(offset)


def test_aim_error_is_the_distance_from_the_center() -> None:
    [j] = run(osu_file(obj(1000)), tap(1000, (P[0] + 12, P[1] + 16)))

    assert j.distance_px == pytest.approx(20)  # a 3-4-5 triangle


@pytest.mark.parametrize(
    ("mods", "cursor_y", "result"),
    [
        (("HR",), 384 - 100, "300"),  # HR flips the map: the circle is drawn at y 284
        (("HR",), 100, "miss"),  # where the file says, but not where the player saw it
        ((), 100, "300"),
    ],
)
def test_hr_flips_the_map(mods: tuple[str, ...], cursor_y: float, result: str) -> None:
    [j] = run(osu_file(obj(1000, (100, 100))), tap(1000, (100, cursor_y)), mods=mods)

    assert j.result == result


def test_hr_shrinks_the_circle() -> None:
    # CS 4 -> 5.2 with HR: the radius drops from 36.5 to 31.1 px.
    without = run(osu_file(obj(1000)), tap(1000, (P[0] + 34, P[1])))
    with_hr = run(osu_file(obj(1000)), tap(1000, (P[0] + 34, 384 - P[1])), mods=["HR"])

    assert (without[0].result, with_hr[0].result) == ("300", "miss")


# ---------- Keys ----------


def test_both_buttons_count_as_presses() -> None:
    js = run(osu_file(obj(1000), obj(1100)), tap(1000, key=M1), tap(1100, key=M2))

    assert results(js) == ["300", "300"]


def test_holding_a_key_is_one_press() -> None:
    held = [Frame(1000, *P, M1), Frame(1100, *P, M1), Frame(1150, *P, 0)]

    assert results(run(osu_file(obj(1000), obj(1100)), held)) == ["300", "miss"]


def test_a_keyboard_key_while_its_mouse_button_is_held_isnt_a_new_press() -> None:
    # M1 then K1 (which sets M1's bit too): both are "left", already down.
    frames = [Frame(1000, *P, M1), Frame(1100, *P, K1), Frame(1150, *P, 0)]

    assert results(run(osu_file(obj(1000), obj(1100)), frames)) == ["300", "miss"]


def test_the_other_button_while_one_is_held_is_a_press() -> None:
    frames = [Frame(1000, *P, M1), Frame(1100, *P, M1 | M2), Frame(1150, *P, 0)]

    assert results(run(osu_file(obj(1000), obj(1100)), frames)) == ["300", "300"]


def test_both_buttons_in_one_frame_are_two_presses() -> None:
    # Two circles at the same time and place: one press each.
    frames = [Frame(1000, *P, K1 | K2), Frame(1050, *P, 0)]

    assert results(run(osu_file(obj(1000), obj(1000)), frames)) == ["300", "300"]


def test_smoke_isnt_a_press() -> None:
    assert results(run(osu_file(obj(1000)), tap(1000, key=SMOKE))) == ["miss"]


def test_a_press_with_nothing_under_the_cursor_does_nothing() -> None:
    js = run(osu_file(obj(1000), obj(2000)), tap(1000, Q), tap(2000))

    assert results(js) == ["miss", "300"]
    assert not js[0].forced  # missed by time, not by note lock


# ---------- Which circle a press goes to ----------


def test_stacked_circles_are_hit_in_order() -> None:
    # Same place: each press goes to the earliest circle not yet judged.
    js = run(osu_file(obj(1000), obj(1100), obj(1200)), tap(1000), tap(1100), tap(1200))

    assert results(js) == ["300", "300", "300"]
    assert [j.error_ms for j in js] == [0, 0, 0]


def test_a_press_goes_to_the_earliest_circle_under_the_cursor() -> None:
    # Pressing at the second circle's time hits the first: it's earlier and also there.
    # (The other button for the second press: the first is still held at 1090.)
    js = run(osu_file(obj(1000), obj(1080)), tap(1080), tap(1090, key=M2))

    assert [(j.result, j.error_ms) for j in js] == [("50", 80), ("300", 10)]


def test_a_circle_not_on_screen_yet_cant_be_hit() -> None:
    # The second circle appears at 9550 (450 ms early); a press there before does nothing.
    js = run(osu_file(obj(1000), obj(10_000, Q)), tap(1000), tap(9_500, Q), tap(10_000, Q))

    assert results(js) == ["300", "300"]
    assert js[1].error_ms == 0


# ---------- Note lock ----------


def test_note_lock_shakes_a_later_circle() -> None:
    # Q is hit at 1050, before P's time (1100) has come and while P is waiting: it shakes.
    # The press at Q again at 1150 counts (P's time has passed) and misses P.
    js = run(osu_file(obj(1100, P), obj(1160, Q)), tap(1050, Q), tap(1150, Q))

    assert [(j.result, j.forced) for j in js] == [("miss", True), ("300", False)]
    assert js[1].error_ms == -10


def test_once_its_time_has_come_a_later_circle_can_be_hit() -> None:
    # Exactly at P's time (lazer allows simultaneous circles): Q is hit early, P missed.
    js = run(osu_file(obj(1000, P), obj(1050, Q)), tap(1000, Q))

    assert [(j.result, j.forced) for j in js] == [("miss", True), ("100", False)]  # 50 early


def test_hitting_a_later_circle_misses_every_earlier_one_waiting() -> None:
    js = run(osu_file(obj(1000, P), obj(1030, P), obj(1080, Q)), tap(1060, Q))

    assert results(js) == ["miss", "miss", "100"]
    assert [j.forced for j in js] == [True, True, False]


def test_a_judged_earlier_circle_doesnt_lock() -> None:
    js = run(osu_file(obj(1000, P), obj(1100, Q)), tap(1000, P), tap(1060, Q))

    # P is judged, so Q's early press counts at once (40 ms early: a 100).
    assert [(j.result, j.forced) for j in js] == [("300", False), ("100", False)]


# ---------- The map ----------


def test_slider_heads_are_judged_like_circles() -> None:
    js = run(osu_file(obj(1000, kind="slider"), obj(2000)), tap(1030), tap(2000))

    assert [(j.kind, j.result) for j in js] == [("slider", "100"), ("circle", "300")]


def test_spinners_are_left_out_for_now() -> None:
    # Not judged yet; the circle after keeps its place in the map (idx 2).
    js = run(osu_file(obj(1000), obj(2000, kind="spinner"), obj(5000)), tap(1000), tap(5000))

    assert [(j.idx, j.result) for j in js] == [(0, "300"), (2, "300")]


@pytest.mark.parametrize(("version", "offset"), [(4, EARLY_FORMAT_OFFSET_MS), (5, 0), (14, 0)])
def test_old_file_formats_play_24_ms_later(version: int, offset: int) -> None:
    [j] = run(osu_file(obj(1000), version=version), tap(1024))

    assert j.time_ms == 1000 + offset
    assert j.error_ms == 24 - offset


def test_no_objects() -> None:
    assert run(osu_file(), tap(1000)) == []


# ---------- counts ----------


def test_counts() -> None:
    js = run(osu_file(obj(1000), obj(2000), obj(3000), obj(4000), obj(5000)),
             tap(1000), tap(2030), tap(3070), tap(4000))  # fmt: skip

    assert counts(js) == {"300": 2, "100": 1, "50": 1, "miss": 1}
    assert counts([]) == {"300": 0, "100": 0, "50": 0, "miss": 0}
