from dataclasses import dataclass
from typing import Literal


class OsuFileError(ValueError):
    """The file isn't a valid .osu file"""


@dataclass(frozen=True)
class Difficulty:
    hp: float
    cs: float
    od: float
    ar: float
    slider_multiplier: float
    slider_tick_rate: float


@dataclass(frozen=True)
class TimingPoint:
    time_ms: float
    beat_length: float
    uninherited: bool


HitKind = Literal["circle", "slider", "spinner"]


@dataclass(frozen=True)
class HitObject:
    kind: HitKind
    x: float
    y: float
    time_ms: int
    new_combo: bool
    curve_type: str | None = None  # sliders only
    curve_points: tuple[tuple[float, float], ...] = ()
    slides: int = 1
    length: float = 0.0
    end_time_ms: int | None = None


@dataclass(frozen=True)
class OsuFile:
    format_version: int
    difficulty: Difficulty
    timing_points: tuple[TimingPoint, ...]
    hit_objects: tuple[HitObject, ...]


CIRCLE, SLIDER, NEW_COMBO, SPINNER = 1, 2, 4, 8


def parse_osu(text: str) -> OsuFile:
    """Parse a .osu file's text into its difficulty, timing points and hit objects."""
    # A BOM is an invisible character some editors put first; splitlines() handles both
    # \r\n (map 22423) and \n (map 75) line endings.
    lines = text.lstrip("\ufeff").splitlines()

    # The first non-empty line says which format version the file uses.
    first = next((line.strip() for line in lines if line.strip()), "")
    prefix = "osu file format v"
    if not first.startswith(prefix):
        raise OsuFileError(f"not a .osu file: starts with {first[:40]!r}")
    try:
        format_version = int(first.removeprefix(prefix))
    except ValueError:
        raise OsuFileError(f"bad format version: {first!r}") from None

    section = ""
    difficulty: dict[str, str] = {}
    timing_points: list[TimingPoint] = []
    hit_objects: list[HitObject] = []

    # enumerate(..., start=1): line numbers as an editor shows them, for error messages.
    for line_no, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("//"):  # blank lines and comments
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]  # "[HitObjects]" -> "HitObjects"
            continue
        if section == "Difficulty":
            key, _, value = line.partition(":")
            # .strip(): map 75 writes "SliderMultiplier: 1.4", with a space
            difficulty[key.strip()] = value.strip()
        elif section == "TimingPoints":
            timing_points.append(_timing_point(line, line_no))
        elif section == "HitObjects":
            hit_objects.append(_hit_object(line, line_no))
        # Other sections (General, Metadata, Events, Colours, ...) aren't needed.

    return OsuFile(
        format_version=format_version,
        difficulty=_difficulty(difficulty),
        timing_points=tuple(timing_points),
        hit_objects=tuple(hit_objects),
    )


def _difficulty(values: dict[str, str]) -> Difficulty:
    """Build Difficulty from the [Difficulty] section's key-value pairs."""

    def number(key: str) -> float:
        if key not in values:
            raise OsuFileError(f"[Difficulty] has no {key}")
        try:
            return float(values[key])
        except ValueError:
            raise OsuFileError(f"[Difficulty] {key} is not a number: {values[key]!r}") from None

    od = number("OverallDifficulty")
    return Difficulty(
        hp=number("HPDrainRate"),
        cs=number("CircleSize"),
        od=od,
        # Old file formats have no ApproachRate line; osu! uses the OD then.
        ar=number("ApproachRate") if "ApproachRate" in values else od,
        slider_multiplier=number("SliderMultiplier"),
        slider_tick_rate=number("SliderTickRate"),
    )


def _timing_point(line: str, line_no: int) -> TimingPoint:
    try:
        fields = line.split(",")
        time = float(fields[0])
        beat_length = float(fields[1])
        uninherited = fields[6] == "1" if len(fields) > 6 else beat_length > 0

        return TimingPoint(time_ms=time, beat_length=beat_length, uninherited=uninherited)
    except (ValueError, IndexError):
        raise OsuFileError(f"line {line_no}: bad timing point {line!r}") from None


def _hit_object(line: str, line_no: int) -> HitObject:
    """Parse one [HitObjects] line: x,y,time,type,hitSound,params..."""
    try:
        fields = line.split(",")
        x, y = float(fields[0]), float(fields[1])
        time = int(float(fields[2]))  # a few old maps write times like "1500.0"
        kind_bits = int(fields[3])
        # The type is a set of bit flags: 5 = circle + new combo, 12 = spinner + new
        # combo. Test each bit with &; == would miss every new-combo object.
        new_combo = bool(kind_bits & NEW_COMBO)

        if kind_bits & CIRCLE:
            return HitObject("circle", x, y, time, new_combo)

        if kind_bits & SLIDER:
            # fields[5] is like "B|114:308|216:280": the curve type, then control points.
            curve_type, *points = fields[5].split("|")
            curve_points = tuple(
                (float(px), float(py)) for px, py in (point.split(":") for point in points)
            )
            return HitObject(
                "slider",
                x,
                y,
                time,
                new_combo,
                curve_type=curve_type,
                curve_points=curve_points,
                slides=int(fields[6]),
                length=float(fields[7]),
            )

        if kind_bits & SPINNER:
            return HitObject("spinner", x, y, time, new_combo, end_time_ms=int(float(fields[5])))
    except (ValueError, IndexError):
        # Only broken input: a number that isn't one, or a field that's missing. Any
        # other error is a bug and must surface, not be hidden here.
        raise OsuFileError(f"line {line_no}: bad hit object {line!r}") from None

    # None of the three kinds: e.g. 128, a mania hold note, which an osu! standard map
    # never has.
    raise OsuFileError(f"line {line_no}: unknown hit object type {kind_bits} in {line!r}")
