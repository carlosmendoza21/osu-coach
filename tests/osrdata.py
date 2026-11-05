"""Building .osr replay files byte by byte, for tests.

`osr()` writes the format osu_coach.osrfile reads, with every header field, frame and
lazer block settable, so tests don't need real players' replays.
"""

import json
import lzma
import struct
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from osu_coach.osrfile import LONG_SCORE_ID_VERSION, MOD_BITS

CHECKSUM = "ce03ee786a8f8c2bf794cc3ced189bbb"
PLAYED_AT = datetime(2026, 4, 28, 2, 17, 2, 332400, tzinfo=UTC)
STABLE = 20230326
LAZER = 30000016

# What osu!stable writes first in every replay: two placeholder frames at (256, -500),
# one at 0 and one just before the skip point.
STABLE_START = [(0, 256, -500, 0), (-1, 256, -500, 0)]
BODY = [(1000, 100.5, 200.25, 0), (16, 101, 201, 5), (17, 102, 202, 0)]

RawFrame = tuple[int, float, float, int]  # time delta, x, y, keys


def osu_string(value: str | None) -> bytes:
    """osu!'s string: 0x00 for none, else 0x0b, the length as ULEB128, then UTF-8."""
    if value is None:
        return b"\x00"
    raw = value.encode()
    n, length = len(raw), bytearray()
    while True:
        byte, n = n & 0x7F, n >> 7
        length.append(byte | (0x80 if n else 0))
        if not n:
            return b"\x0b" + bytes(length) + raw


def compress(data: bytes) -> bytes:
    return lzma.compress(data, format=lzma.FORMAT_ALONE)


def packed(data: bytes) -> bytes:
    """[int32 length][LZMA data], how both the frames and lazer's block are stored."""
    body = compress(data)
    return struct.pack("<i", len(body)) + body


def frame_text(frames: Sequence[RawFrame], seed: int | None) -> str:
    text = ",".join(f"{d}|{x}|{y}|{k}" for d, x, y, k in frames)
    return text + (f",-12345|0|0|{seed}" if seed is not None else "") + ","


def osr(
    *,
    mode: int = 0,
    version: int = STABLE,
    checksum: str | None = CHECKSUM,
    player: bytes | str = "tester",
    counts: tuple[int, ...] = (100, 5, 1, 10, 2, 3),  # 300, 100, 50, geki, katu, miss
    score: int = 123456,
    combo: int = 150,
    perfect: bool = False,
    mods: int = 0,
    ticks: int | None = None,  # .NET ticks; PLAYED_AT by default
    frames: Sequence[RawFrame] | None = None,
    seed: int | None = 7,  # stable's last frame, -12345|0|0|seed
    text: bytes | None = None,  # the frame text as it is, instead of frames and seed
    frame_data: bytes | None = None,  # the stored frame bytes as they are: length, then LZMA
    score_id: int = 42,
    lazer: Any = None,  # a dict or list becomes the block; bytes are appended as they are
) -> bytes:
    frames = [*STABLE_START, *BODY] if frames is None else frames
    if text is None:
        text = frame_text(frames, seed).encode()
    if ticks is None:
        ticks = (PLAYED_AT - datetime(1, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1) * 10
    name = player if isinstance(player, bytes) else osu_string(player)
    data = (
        struct.pack("<bi", mode, version)
        + osu_string(checksum)
        + name
        + osu_string("replay-md5")
        + struct.pack("<6H", *counts)
        + struct.pack("<iH?i", score, combo, perfect, mods)
        + osu_string("")  # life bar graph
        + struct.pack("<q", ticks)
        + (packed(text) if frame_data is None else frame_data)
        # The score id has 8 bytes since 2014, 4 before.
        + struct.pack("<q" if version >= LONG_SCORE_ID_VERSION else "<i", score_id)
    )
    if mods & MOD_BITS["TP"]:
        data += struct.pack("<d", 0.97)  # Target Practice's accuracy
    if isinstance(lazer, bytes):
        return data + lazer
    if lazer is not None:
        return data + packed(json.dumps(lazer).encode())
    return data


def lazer_osr(info: Any = None, **kwargs: Any) -> bytes:
    """A lazer replay: lazer writes -1 as the header's score id and no placeholder frames."""
    kwargs = {"version": LAZER, "score_id": -1, "frames": BODY, "seed": None, **kwargs}
    return osr(lazer={"mods": [], "online_id": 6604281970} if info is None else info, **kwargs)


def play(
    hit_objects: Sequence[Any], errors: Sequence[int] = (0,), missed: Sequence[int] = ()
) -> list[RawFrame]:
    """Frames that play a map: each circle and slider head tapped on its spot, `errors`
    ms off (cycling through them), alternating M1 and M2. Spinners and the objects at
    the indexes in `missed` are left alone. Give `osu_file.hit_objects`."""
    events: list[tuple[int, float, float, int]] = []  # absolute time, x, y, keys
    n = 0
    for i, h in enumerate(hit_objects):
        if h.kind == "spinner" or i in missed:
            continue
        t = h.time_ms + errors[n % len(errors)]
        key = 1 if n % 2 == 0 else 2
        events += [(t, h.x, h.y, key), (t + 30, h.x, h.y, 0)]
        n += 1
    frames: list[RawFrame] = []
    last = 0
    for t, x, y, keys in sorted(events):
        frames.append((t - last, x, y, keys))
        last = t
    return frames
