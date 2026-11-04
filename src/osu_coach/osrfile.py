"""Read .osr replay files: who played which map, the result, and every cursor frame.

The reader follows osu!lazer's LegacyScoreDecoder, which reads both osu!stable and
osu!lazer replays, so frames line up with how osu! itself replays the file.
"""

import json
import lzma
import math
import re
import struct
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

# osu!lazer replays have a game version of 30000001 or higher, and add a block of extra
# score data (JSON) at the end of the file.
FIRST_LAZER_VERSION = 30000001
# The score id grew from 4 to 8 bytes in this version.
LONG_SCORE_ID_VERSION = 20140721

# Limits on decompressed sizes, so a small upload can't unpack into gigabytes. A 6-minute
# replay's frames are under 1 MB.
MAX_FRAME_TEXT = 16 * 1024 * 1024
MAX_LAZER_JSON = 1024 * 1024

# Stable's mod bits, by acronym. NC also sets DT's bit and PF also sets SD's.
MOD_BITS = {
    "NF": 1 << 0,
    "EZ": 1 << 1,
    "TD": 1 << 2,
    "HD": 1 << 3,
    "HR": 1 << 4,
    "SD": 1 << 5,
    "DT": 1 << 6,
    "RX": 1 << 7,
    "HT": 1 << 8,
    "NC": 1 << 9,
    "FL": 1 << 10,
    "AT": 1 << 11,
    "SO": 1 << 12,
    "AP": 1 << 13,
    "PF": 1 << 14,
    "FI": 1 << 20,
    "RD": 1 << 21,
    "CN": 1 << 22,
    "TP": 1 << 23,
    "V2": 1 << 29,
    "MR": 1 << 30,
}

MODES = {0: "osu", 1: "taiko", 2: "catch", 3: "mania"}
SEED_FRAME = "-12345"  # stable's last frame holds a random seed, not a cursor position
DAMAGED = "not a valid .osr file (cut short or damaged)"
DOTNET_EPOCH = datetime(1, 1, 1, tzinfo=UTC)  # the file stores .NET ticks (100 ns) since


class ReplayFileError(ValueError):
    """The file isn't a valid osu! standard replay."""


@dataclass(frozen=True)
class Frame:
    time_ms: int  # song time, the same clock as the .osu file's hit object times
    x: float  # playfield pixels (0-512 by 0-384; the cursor can leave the playfield)
    y: float
    keys: int  # bits: M1 1, M2 2, K1 4, K2 8, smoke 16 (K1 also sets M1, K2 sets M2)


@dataclass(frozen=True)
class ReplayFile:
    game_version: int
    lazer: bool
    beatmap_checksum: str  # MD5 of the .osu file the replay was played on
    player: str
    score_id: int | None  # the online score, if osu! knows it
    mods: tuple[str, ...]  # acronyms, e.g. ("HD", "DT")
    count_300: int
    count_100: int
    count_50: int
    count_geki: int
    count_katu: int
    count_miss: int
    score: int
    max_combo: int
    perfect: bool
    played_at: datetime  # UTC
    frames: tuple[Frame, ...]


def parse_replay(data: bytes) -> ReplayFile:
    """Parse an osu! standard .osr file from osu!stable or osu!lazer."""
    r = _Reader(data)

    mode = r.unpack("b")
    if mode not in MODES:
        raise ReplayFileError(DAMAGED)
    if mode != 0:
        raise ReplayFileError(f"only osu! standard replays are supported, not {MODES[mode]}")
    game_version = r.unpack("i")
    lazer = game_version >= FIRST_LAZER_VERSION

    beatmap_checksum = r.string().lower()  # osu! writes MD5s in lowercase
    if not beatmap_checksum:
        raise ReplayFileError("the replay doesn't say which map it was played on")
    if not re.fullmatch(r"[0-9a-f]{32}", beatmap_checksum):
        raise ReplayFileError("the replay's map checksum isn't an MD5")
    player = r.string()
    r.string()  # the replay's own MD5
    count_300, count_100, count_50, count_geki, count_katu, count_miss = (
        r.unpack("H") for _ in range(6)
    )
    score, max_combo, perfect, mod_bits = r.unpack("i"), r.unpack("H"), r.unpack("?"), r.unpack("i")
    r.string()  # the life bar graph
    played_at = _played_at(r.unpack("q"))
    frame_text = _decompress(r.take(r.unpack("i")), MAX_FRAME_TEXT)

    header_id = r.unpack("q") if game_version >= LONG_SCORE_ID_VERSION else r.unpack("i")
    score_id = header_id if header_id > 0 else None
    if mod_bits & MOD_BITS["TP"]:
        r.unpack("d")  # Target Practice's accuracy

    if lazer:
        # Lists lazer's mods (Classic, for one, has no stable bit) and the online score id,
        # which lazer leaves out of the header.
        length = r.unpack("i")
        if length <= 0:
            raise ReplayFileError("lazer replay without its score data block")
        info = _lazer_info(_decompress(r.take(length), MAX_LAZER_JSON))
        mods = _lazer_mods(info)
        online_id = info.get("online_id")
        if score_id is None and type(online_id) is int and online_id > 0:
            score_id = online_id
    else:
        mods = mod_names(mod_bits)

    frames = _frames(frame_text)
    if not frames:
        raise ReplayFileError("the replay has no cursor frames")

    return ReplayFile(
        game_version=game_version,
        lazer=lazer,
        beatmap_checksum=beatmap_checksum,
        player=player,
        score_id=score_id,
        mods=mods,
        count_300=count_300,
        count_100=count_100,
        count_50=count_50,
        count_geki=count_geki,
        count_katu=count_katu,
        count_miss=count_miss,
        score=score,
        max_combo=max_combo,
        perfect=perfect,
        played_at=played_at,
        frames=frames,
    )


def mod_names(bits: int) -> tuple[str, ...]:
    """Stable's mod bits as acronyms, written the way osu! shows them (NC, not NC+DT)."""
    names = [name for name, bit in MOD_BITS.items() if bits & bit]
    # osu! sets both bits, but a hand-made file may not, so check each one is there.
    if "NC" in names and "DT" in names:
        names.remove("DT")
    if "PF" in names and "SD" in names:
        names.remove("SD")
    return tuple(names)


class _Reader:
    """Reads little-endian numbers and osu! strings; running out is a ReplayFileError."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise ReplayFileError(DAMAGED)
        chunk = self.data[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def unpack(self, fmt: str) -> Any:
        (value,) = struct.unpack("<" + fmt, self.take(struct.calcsize("<" + fmt)))
        return value

    def string(self) -> str:
        # 0x00 for no string; else 0x0b, the length (7 bits per byte, low bits first), UTF-8.
        marker = self.unpack("B")
        if marker == 0x00:
            return ""
        if marker != 0x0B:
            raise ReplayFileError(DAMAGED)
        length = shift = 0
        while True:
            byte = self.unpack("B")
            length |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                break
        try:
            return self.take(length).decode()
        except UnicodeDecodeError:
            raise ReplayFileError(DAMAGED) from None


def _played_at(ticks: int) -> datetime:
    try:
        return DOTNET_EPOCH + timedelta(microseconds=ticks // 10)
    except OverflowError:  # before year 1 or after 9999
        raise ReplayFileError(DAMAGED) from None


def _decompress(packed: bytes, limit: int) -> bytes:
    decompressor = lzma.LZMADecompressor()
    try:
        data = decompressor.decompress(packed, max_length=limit + 1)
    except lzma.LZMAError:
        raise ReplayFileError(DAMAGED) from None
    if len(data) > limit:
        raise ReplayFileError("the replay is too large once unpacked")
    if not decompressor.eof:  # cut short
        raise ReplayFileError(DAMAGED)
    return data


def _frames(text: bytes) -> tuple[Frame, ...]:
    # Each frame is "time since the previous frame|x|y|keys", separated by commas.
    frames: list[Frame] = []
    time_ms = 0
    for i, entry in enumerate(text.split(b",")):
        parts = entry.split(b"|")
        if len(parts) < 4:  # the trailing comma's empty entry
            continue
        if parts[0] == SEED_FRAME.encode():
            continue
        try:
            delta, x, y, keys = int(parts[0]), float(parts[1]), float(parts[2]), int(parts[3])
        except ValueError:
            raise ReplayFileError(DAMAGED) from None
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ReplayFileError(DAMAGED)

        time_ms += delta
        # Stable starts with two placeholder frames at (256, -500): one at 0 and one just
        # before the skip point. They aren't cursor positions, but their time counts.
        if i < 2 and x == 256 and y == -500:
            continue
        # Frames that step back in time are skipped, but still move the clock.
        if delta < 0:
            continue
        frames.append(Frame(time_ms, x, y, keys))
    return tuple(frames)


def _lazer_info(text: bytes) -> dict[str, Any]:
    try:
        info = json.loads(text)
    except ValueError:  # includes text that isn't UTF-8
        raise ReplayFileError("lazer replay with unreadable score data") from None
    if not isinstance(info, dict):
        raise ReplayFileError("lazer replay with unreadable score data")
    return info


def _lazer_mods(info: dict[str, Any]) -> tuple[str, ...]:
    mods = info.get("mods", [])
    if not isinstance(mods, list) or not all(
        isinstance(m, dict) and isinstance(m.get("acronym"), str) for m in mods
    ):
        raise ReplayFileError("lazer replay with an unreadable mod list")
    return tuple(m["acronym"] for m in mods)
