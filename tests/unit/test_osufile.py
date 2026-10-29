"""Unit tests for osu_coach.osufile, the .osu parser.

Two real map files from tests/fixtures/osu/ (22423: format v5, \\r\\n line endings; 75:
format v3, \\n) are checked against what osu!'s API says about them. Small hand-written
files cover each quirk of the format one at a time.
tests/integration/test_osufile_live.py parses many more real maps.
"""

import dataclasses
from collections import Counter

import pytest

from osu_coach.osufile import (
    HitObject,
    OsuFile,
    OsuFileError,
    TimingPoint,
    _hit_object,
    _timing_point,
    parse_osu,
)
from tests.support import FIXTURES, load

MAP_22423 = (FIXTURES / "22423.osu").read_text(encoding="utf-8")
MAP_75 = (FIXTURES / "75.osu").read_text(encoding="utf-8")

DIFFICULTY = """[Difficulty]
HPDrainRate:5
CircleSize:4
OverallDifficulty:8
SliderMultiplier:1.4
SliderTickRate:1
"""


def osu(*sections: str, version: int = 14) -> str:
    """A minimal .osu file: the version line and the given sections."""
    return "\n".join([f"osu file format v{version}", "", *sections])


# ---------- Real files ----------


@pytest.mark.parametrize(
    ("text", "want"),
    [
        # From osu!'s API (beatmap.json for 22423; GET /beatmaps/75 for 75).
        (MAP_22423, {"circle": 167, "slider": 84, "spinner": 3}),
        (MAP_75, {"circle": 160, "slider": 30, "spinner": 4}),
    ],
    ids=["22423", "75"],
)
def test_object_counts_match_osu(text: str, want: dict[str, int]) -> None:
    parsed = parse_osu(text)

    assert Counter(o.kind for o in parsed.hit_objects) == want


def test_map_22423_matches_the_api() -> None:
    api = load("beatmap.json")

    parsed = parse_osu(MAP_22423)

    d = parsed.difficulty
    assert parsed.format_version == 5
    assert (d.hp, d.cs, d.od, d.ar) == (api["drain"], api["cs"], api["accuracy"], api["ar"])
    assert (d.slider_multiplier, d.slider_tick_rate) == (1.6, 2.0)
    assert parsed.timing_points == (TimingPoint(15460.0, 428.571428571429, True),)


def test_map_22423_first_object() -> None:
    first = parse_osu(MAP_22423).hit_objects[0]

    assert first == HitObject(
        "slider",
        40.0,
        272.0,
        14388,
        False,
        curve_type="B",
        curve_points=((114.0, 308.0), (216.0, 280.0)),
        slides=1,
        length=160.0,
    )


def test_map_75_old_format() -> None:
    parsed = parse_osu(MAP_75)

    assert parsed.format_version == 3
    # "SliderMultiplier: 1.4" has a space after the colon; no ApproachRate line (AR = OD).
    assert (parsed.difficulty.slider_multiplier, parsed.difficulty.ar) == (1.4, 6.0)
    # Its timing point has only 2 fields: positive beat length = uninherited.
    assert parsed.timing_points == (TimingPoint(118.0, 500.004999999999, True),)


@pytest.mark.parametrize("text", [MAP_22423, MAP_75], ids=["22423", "75"])
def test_objects_come_in_time_order(text: str) -> None:
    times = [o.time_ms for o in parse_osu(text).hit_objects]

    assert times == sorted(times)


def test_line_endings_dont_matter() -> None:
    # Map 22423 uses \r\n; the same file with \n must parse to exactly the same result.
    assert b"\r\n" in (FIXTURES / "22423.osu").read_bytes()

    assert parse_osu(MAP_22423.replace("\r\n", "\n")) == parse_osu(MAP_22423)


def test_result_is_read_only() -> None:
    parsed = parse_osu(MAP_75)

    with pytest.raises(dataclasses.FrozenInstanceError):
        parsed.format_version = 99  # type: ignore[misc]
    assert isinstance(parsed.hit_objects, tuple)
    assert isinstance(parsed.timing_points, tuple)
    assert isinstance(parsed.hit_objects[0].curve_points, tuple)


# ---------- The header ----------


@pytest.mark.parametrize(
    ("text", "version"),
    [
        ("osu file format v14", 14),
        ("﻿osu file format v14", 14),  # a BOM some editors add, invisible
        ("\n\n  osu file format v9  ", 9),  # blank lines and spaces first
    ],
)
def test_version_line(text: str, version: int) -> None:
    assert parse_osu(text + "\n" + DIFFICULTY).format_version == version


@pytest.mark.parametrize(
    "text",
    [
        "",
        "hello",
        "<html>Not found</html>",  # what a web error page would look like
        "osu file format vX",
        "osu file format v",
        "[Difficulty]\nosu file format v14",  # the version line must come first
    ],
)
def test_not_an_osu_file(text: str) -> None:
    with pytest.raises(OsuFileError):
        parse_osu(text)


# ---------- [Difficulty] ----------


def test_space_after_the_colon() -> None:
    parsed = parse_osu(osu(DIFFICULTY.replace("SliderMultiplier:1.4", "SliderMultiplier: 1.4 ")))

    assert parsed.difficulty.slider_multiplier == 1.4


@pytest.mark.parametrize(
    ("extra", "want_ar"),
    [
        ("", 8.0),  # no ApproachRate line: AR = OD, as osu! does for old maps
        ("ApproachRate:9.3\n", 9.3),
        ("ApproachRate:0\n", 0.0),  # 0 is a real value, not "missing"
    ],
)
def test_approach_rate(extra: str, want_ar: float) -> None:
    assert parse_osu(osu(DIFFICULTY + extra)).difficulty.ar == want_ar


@pytest.mark.parametrize(
    "key",
    ["HPDrainRate", "CircleSize", "OverallDifficulty", "SliderMultiplier", "SliderTickRate"],
)
def test_missing_difficulty_value_is_named(key: str) -> None:
    text = osu("\n".join(line for line in DIFFICULTY.splitlines() if not line.startswith(key)))

    with pytest.raises(OsuFileError, match=key):
        parse_osu(text)


def test_difficulty_value_that_isnt_a_number() -> None:
    with pytest.raises(OsuFileError, match="CircleSize"):
        parse_osu(osu(DIFFICULTY.replace("CircleSize:4", "CircleSize:four")))


# ---------- Sections and lines ----------


def test_comments_blank_lines_and_other_sections_are_skipped() -> None:
    text = osu(
        "[General]\nAudioFilename: song.mp3\n// a comment\n",
        DIFFICULTY,
        "[Colours]\nCombo1 : 255,0,0\n",
        "[HitObjects]\n// another comment\n\n256,192,1000,1,0\n",
    )

    assert parse_osu(text).hit_objects == (HitObject("circle", 256.0, 192.0, 1000, False),)


def test_sections_can_come_in_any_order() -> None:
    text = osu("[HitObjects]\n256,192,1000,1,0\n", "[TimingPoints]\n0,500\n", DIFFICULTY)

    parsed = parse_osu(text)

    assert len(parsed.hit_objects) == 1
    assert len(parsed.timing_points) == 1


def test_a_map_without_objects() -> None:
    parsed = parse_osu(osu(DIFFICULTY))

    assert parsed.hit_objects == ()
    assert parsed.timing_points == ()


def test_errors_name_the_right_line() -> None:
    text = osu(DIFFICULTY.strip(), "[HitObjects]", "256,192,500,1,0", "96,64,abc,1,0")
    # Where the broken object is, counting from 1 like an editor does.
    line_no = text.splitlines().index("96,64,abc,1,0") + 1
    assert line_no == 11

    with pytest.raises(OsuFileError, match=f"line {line_no}:"):
        parse_osu(text)


# ---------- [TimingPoints] ----------


@pytest.mark.parametrize(
    ("line", "want"),
    [
        ("15460,428.571428571429,4,2,1,100,1", TimingPoint(15460.0, 428.571428571429, True)),
        ("20000,-50,4,2,1,100,0,0", TimingPoint(20000.0, -50.0, False)),  # field 6 = 0
        ("118,500.004999999999", TimingPoint(118.0, 500.004999999999, True)),  # 2 fields
        ("30000,-100", TimingPoint(30000.0, -100.0, False)),  # 2 fields, negative
        ("1500.5,300", TimingPoint(1500.5, 300.0, True)),  # decimal time
    ],
)
def test_timing_point(line: str, want: TimingPoint) -> None:
    assert _timing_point(line, 1) == want


@pytest.mark.parametrize("line", ["abc,500", "118", "", "118,fast"])
def test_bad_timing_point(line: str) -> None:
    with pytest.raises(OsuFileError, match="line 7: bad timing point"):
        _timing_point(line, 7)


# ---------- [HitObjects] ----------


@pytest.mark.parametrize(
    ("type_bits", "kind", "new_combo"),
    [
        (1, "circle", False),
        (5, "circle", True),  # circle + new combo
        (21, "circle", True),  # + combo colour skip bits (16): still a circle
        (2, "slider", False),
        (6, "slider", True),
        (8, "spinner", False),
        (12, "spinner", True),  # spinner + new combo
    ],
)
def test_type_bits(type_bits: int, kind: str, new_combo: bool) -> None:
    rest = {"circle": "0", "slider": "0,L|300:200,1,100", "spinner": "0,3000"}[kind]

    obj = _hit_object(f"256,192,1000,{type_bits},{rest}", 1)

    assert (obj.kind, obj.new_combo) == (kind, new_combo)


@pytest.mark.parametrize("type_bits", [0, 128, 16])  # none of circle/slider/spinner
def test_unknown_type(type_bits: int) -> None:
    with pytest.raises(OsuFileError, match=f"unknown hit object type {type_bits}"):
        _hit_object(f"256,192,1000,{type_bits},0", 3)


def test_circle_with_trailing_comma() -> None:
    assert _hit_object("96,64,8118,5,4,", 1) == HitObject("circle", 96.0, 64.0, 8118, True)


@pytest.mark.parametrize("curve", ["B", "P", "L", "C"])
def test_slider_curve_types(curve: str) -> None:
    obj = _hit_object(f"100,100,500,2,0,{curve}|200:150|300:100,1,240.5", 1)

    assert obj.curve_type == curve
    assert obj.curve_points == ((200.0, 150.0), (300.0, 100.0))
    assert obj.length == 240.5


def test_slider_with_repeats_and_extra_fields() -> None:
    # Newer files add edge sounds and samples after the length; they're ignored.
    obj = _hit_object(
        "40,272,14388,6,0,B|114:308|216:280,3,160,2|0|0|2,0:0|0:0|0:0|0:0,0:0:0:0:", 1
    )

    assert (obj.kind, obj.slides, obj.length, obj.new_combo) == ("slider", 3, 160.0, True)


def test_spinner() -> None:
    obj = _hit_object("256,192,42888,12,4,46317", 1)

    assert obj == HitObject("spinner", 256.0, 192.0, 42888, True, end_time_ms=46317)


def test_decimal_values() -> None:
    # A few old maps write times and positions with decimals.
    obj = _hit_object("96.5,64.25,1500.0,1,0", 1)

    assert (obj.x, obj.y, obj.time_ms) == (96.5, 64.25, 1500)


@pytest.mark.parametrize(
    "line",
    [
        "96,64,abc,1,0",  # time isn't a number
        "96,64",  # fields missing
        "x,64,100,1,0",  # position isn't a number
        "96,64,100,one,0",  # type isn't a number
        "1,2,3,2,0,B|1:2,1",  # slider without its length
        "1,2,3,2,0,B|1-2,1,100",  # slider point without a colon
        "1,2,3,2,0,B|1:y,1,100",  # slider point that isn't a number
        "256,192,100,8,0",  # spinner without its end time
    ],
)
def test_bad_hit_object(line: str) -> None:
    with pytest.raises(OsuFileError, match="line 412: bad hit object"):
        _hit_object(line, 412)


def test_parse_result_type() -> None:
    assert isinstance(parse_osu(MAP_75), OsuFile)
