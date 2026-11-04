"""Unit tests for osu_coach.osrfile.

Replays are built byte by byte here (`osr()` below writes the .osr format), so every
header field, frame and lazer block can be set exactly. See
tests/integration/test_osrfile_live.py for real replays checked against their maps.
"""

import contextlib
import json
import lzma
import random
import struct
import tracemalloc
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from osu_coach import osrfile
from osu_coach.osrfile import (
    FIRST_LAZER_VERSION,
    LONG_SCORE_ID_VERSION,
    MAX_FRAME_TEXT,
    MAX_LAZER_JSON,
    MOD_BITS,
    Frame,
    ReplayFileError,
    mod_names,
    parse_replay,
)

CHECKSUM = "ce03ee786a8f8c2bf794cc3ced189bbb"
PLAYED_AT = datetime(2026, 4, 28, 2, 17, 2, 332400, tzinfo=UTC)
STABLE = 20230326
LAZER = 30000016
DAMAGED = "not a valid .osr file"

# What osu!stable writes first in every replay: two placeholder frames at (256, -500),
# one at 0 and one just before the skip point.
STABLE_START = [(0, 256, -500, 0), (-1, 256, -500, 0)]
BODY = [(1000, 100.5, 200.25, 0), (16, 101, 201, 5), (17, 102, 202, 0)]

RawFrame = tuple[int, float, float, int]  # time delta, x, y, keys


# ---------- Writing .osr files ----------


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


def case_id(value: Any) -> str | None:
    """Short test ids: built .osr bytes would make ids too long for Windows."""
    return "osr" if isinstance(value, bytes) else None


# ---------- The header ----------


def test_stable_header() -> None:
    r = parse_replay(osr(perfect=True))

    assert (r.game_version, r.lazer, r.beatmap_checksum, r.player) == (
        STABLE,
        False,
        CHECKSUM,
        "tester",
    )
    counts = (r.count_300, r.count_100, r.count_50, r.count_geki, r.count_katu, r.count_miss)
    assert counts == (100, 5, 1, 10, 2, 3)
    assert (r.score, r.max_combo, r.perfect, r.score_id, r.mods) == (123456, 150, True, 42, ())
    assert r.played_at == PLAYED_AT  # exact to the microsecond, in UTC
    assert r.played_at.tzinfo is UTC


def test_lazer_header() -> None:
    r = parse_replay(lazer_osr())

    assert (r.game_version, r.lazer, r.score_id, r.mods) == (LAZER, True, 6604281970, ())
    assert r.beatmap_checksum == CHECKSUM


def test_counts_are_unsigned() -> None:
    # osu! stores counts and combo as unsigned 16-bit numbers: 40000 is not -25536.
    r = parse_replay(osr(counts=(40000, 0, 0, 0, 0, 65535), combo=65535))

    assert (r.count_300, r.count_miss, r.max_combo) == (40000, 65535, 65535)


def test_names_are_utf8() -> None:
    assert parse_replay(osr(player="Ωmega 東方")).player == "Ωmega 東方"


def test_a_long_string() -> None:
    # Over 127 bytes, the length takes two bytes (7 bits each).
    assert parse_replay(osr(player="x" * 300)).player == "x" * 300


@pytest.mark.parametrize(
    "version",
    [
        FIRST_LAZER_VERSION - 1,  # the last stable-style number
        FIRST_LAZER_VERSION,  # the first lazer one: needs the block
    ],
)
def test_the_first_lazer_version(version: int) -> None:
    lazer = version >= FIRST_LAZER_VERSION
    data = osr(version=version, lazer={"mods": []} if lazer else None)

    assert parse_replay(data).lazer is lazer


def test_bytes_after_the_end_are_ignored() -> None:
    # osu! ignores anything after what it reads, so the parser does too.
    assert parse_replay(osr() + b"extra").score_id == 42


# ---------- Score id ----------


@pytest.mark.parametrize(
    ("data", "want"),
    [
        (osr(score_id=42), 42),
        (osr(score_id=0), None),  # stable writes 0 for a play osu! never saw
        (osr(score_id=-1), None),
        (osr(score_id=2**40), 2**40),  # needs all 8 bytes
        # Before 2014-07-21 the id had 4 bytes.
        (osr(version=LONG_SCORE_ID_VERSION - 1, score_id=7), 7),
        (osr(version=LONG_SCORE_ID_VERSION, score_id=7), 7),
        # Target Practice adds 8 bytes after the id; a lazer block must still be found.
        (osr(version=LAZER, mods=MOD_BITS["TP"], score_id=9, lazer={"mods": []}), 9),
        (lazer_osr({"mods": [], "online_id": 7}), 7),  # lazer: the header says -1
        (lazer_osr({"mods": []}), None),  # an offline lazer play
        (lazer_osr({"mods": [], "online_id": 0}), None),
        (lazer_osr({"mods": [], "online_id": -1}), None),
        (lazer_osr({"mods": [], "online_id": "7"}), None),  # not a number
        (lazer_osr({"mods": [], "online_id": True}), None),  # JSON true is not an id
        (lazer_osr({"mods": [], "online_id": 7}, score_id=42), 42),  # the header wins
    ],
    ids=case_id,
)
def test_score_id(data: bytes, want: int | None) -> None:
    assert parse_replay(data).score_id == want


# ---------- Mods ----------


@pytest.mark.parametrize(
    ("bits", "want"),
    [
        (0, ()),
        (MOD_BITS["HD"] | MOD_BITS["HR"], ("HD", "HR")),
        (MOD_BITS["HD"] | MOD_BITS["DT"] | MOD_BITS["FL"], ("HD", "DT", "FL")),  # bit order
        (MOD_BITS["NC"] | MOD_BITS["DT"], ("NC",)),  # NC sets DT's bit too: show NC only
        (MOD_BITS["PF"] | MOD_BITS["SD"], ("PF",)),  # same for PF and SD
        (MOD_BITS["NC"], ("NC",)),  # a hand-made file with NC's bit alone must not crash
        (MOD_BITS["PF"], ("PF",)),
        (MOD_BITS["DT"], ("DT",)),
        (MOD_BITS["SD"], ("SD",)),
        (MOD_BITS["V2"] | MOD_BITS["MR"], ("V2", "MR")),
        (1 << 15, ()),  # 4K, a mania mod: not osu! standard, so not listed
        (1 << 31, ()),  # no mod uses this bit
    ],
)
def test_mod_names(bits: int, want: tuple[str, ...]) -> None:
    assert mod_names(bits) == want


def test_stable_mods_come_from_the_bits() -> None:
    data = osr(mods=MOD_BITS["HD"] | MOD_BITS["NC"] | MOD_BITS["DT"])

    assert parse_replay(data).mods == ("HD", "NC")


@pytest.mark.parametrize(
    ("mods", "bits", "want"),
    [
        ([{"acronym": "CL"}], 0, ("CL",)),  # Classic has no stable bit
        ([], MOD_BITS["HD"], ()),  # lazer's own list wins over the bits
        (
            [{"acronym": "HD"}, {"acronym": "DT", "settings": {"speed_change": 1.3}}],
            0,
            ("HD", "DT"),
        ),
    ],
)
def test_lazer_mods_come_from_the_block(
    mods: list[dict[str, Any]], bits: int, want: tuple[str, ...]
) -> None:
    assert parse_replay(lazer_osr({"mods": mods}, mods=bits)).mods == want


# ---------- Frames ----------


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        # Each frame stores the time since the one before; frames get the running total.
        (
            [(10, 1, 2, 0), (16, 3, 4, 1), (17, 5, 6, 0)],
            [(10, 1, 2, 0), (26, 3, 4, 1), (43, 5, 6, 0)],
        ),
        # Stable's two placeholders are dropped, but their time still counts: here the
        # second sits at the skip point (5000 - 1 ms), so the next frame is at 5019.
        ([(0, 256, -500, 0), (4999, 256, -500, 0), (20, 1, 1, 0)], [(5019, 1, 1, 0)]),
        ([*STABLE_START, (20, 1, 1, 0)], [(19, 1, 1, 0)]),  # no skip point: 0 and -1
        # Only the first two frames can be placeholders.
        (
            [(5, 1, 1, 0), (5, 1, 1, 0), (5, 256, -500, 0)],
            [(5, 1, 1, 0), (10, 1, 1, 0), (15, 256, -500, 0)],
        ),
        # Not the placeholder position: kept, even though it's first.
        ([(0, 256, -499, 0), (5, 1, 1, 0)], [(0, 256, -499, 0), (5, 1, 1, 0)]),
        # A frame that steps back in time is skipped, but the clock still goes back.
        ([(100, 1, 1, 0), (-30, 2, 2, 0), (10, 3, 3, 0)], [(100, 1, 1, 0), (80, 3, 3, 0)]),
        # lazer starts before the song (negative), as in the gwb replay: -967 then +18.
        ([(-967, 1, 1, 0), (18, 2, 2, 0)], [(-949, 2, 2, 0)]),
        # Two frames at the same time are both kept (a key change between polls).
        ([(10, 1, 1, 0), (0, 1, 1, 1)], [(10, 1, 1, 0), (10, 1, 1, 1)]),
        # Keys as osu! writes them: M1 1, M2 2, K1 5 (sets M1), K2 10 (sets M2), smoke 16.
        (
            [(1, 0, 0, 1), (1, 0, 0, 2), (1, 0, 0, 5), (1, 0, 0, 10), (1, 0, 0, 16), (1, 0, 0, 3)],
            [(1, 0, 0, 1), (2, 0, 0, 2), (3, 0, 0, 5), (4, 0, 0, 10), (5, 0, 0, 16), (6, 0, 0, 3)],
        ),
        # Positions are fractions, and the cursor can leave the playfield.
        ([(1, 100.5, -20.25, 0), (1, 600, 400, 0)], [(1, 100.5, -20.25, 0), (2, 600, 400, 0)]),
    ],
)
def test_frames(raw: list[RawFrame], want: list[RawFrame]) -> None:
    frames = parse_replay(osr(frames=raw)).frames

    assert frames == tuple(Frame(t, x, y, k) for t, x, y, k in want)


@pytest.mark.parametrize(
    ("text", "times"),
    [
        (b"10|1|1|0,16|2|2|0,-12345|0|0|7,", [10, 26]),  # stable: the seed comes last
        (b"10|1|1|0,-12345|0|0|7,16|2|2|0", [10, 26]),  # anywhere, and its time doesn't count
        (b"10|1|1|0,16|2|2|0", [10, 26]),  # some lazer versions leave off the last comma
        (b"10|1|1|0,,16|2|2|0,", [10, 26]),  # an empty entry is skipped, like osu! does
        (b"10|1|1|0,16|2|2", [10]),  # so is one with fewer than 4 parts
    ],
)
def test_frame_text(text: bytes, times: list[int]) -> None:
    frames = parse_replay(osr(text=text)).frames

    assert [f.time_ms for f in frames] == times


def test_frame_types() -> None:
    f = parse_replay(osr()).frames[0]

    assert (type(f.time_ms), type(f.x), type(f.y), type(f.keys)) == (int, float, float, int)


# ---------- Errors ----------


def bomb(size: int) -> bytes:
    """Zeros compress to almost nothing: a small file that unpacks to `size` bytes."""
    return b"0" * size


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", DAMAGED),
        (osr()[:1], DAMAGED),
        (osr()[:10], DAMAGED),  # inside the header
        (osr()[:100], DAMAGED),  # inside the frames
        (osr()[:-1], DAMAGED),  # the score id cut short
        (b"garbage!" * 50, DAMAGED),
        (osr(mode=4), DAMAGED),  # no such mode
        (osr(mode=-1), DAMAGED),
        (osr(mode=1), "only osu! standard replays are supported, not taiko"),
        (osr(mode=2), "only osu! standard replays are supported, not catch"),
        (osr(mode=3), "only osu! standard replays are supported, not mania"),
        (osr(checksum=None), "doesn't say which map"),
        (osr(checksum=""), "doesn't say which map"),
        (osr(player=b"\x05"), DAMAGED),  # a string must start with 0x00 or 0x0b
        (osr(player=b"\x0b\x02\xff\xfe"), DAMAGED),  # not UTF-8
        (osr(player=b"\x0b\xff\xff\xff\xff\x0f"), DAMAGED),  # longer than the file
        (osr(ticks=-10), DAMAGED),  # before year 1
        (osr(ticks=2**63 - 1), DAMAGED),  # after year 9999
        (osr(text=b"10|1|1|0,ten|1|1|0"), DAMAGED),  # a time that isn't a number
        (osr(text=b"10.5|1|1|0"), DAMAGED),  # or not a whole number
        (osr(text=b"10|nan|1|0"), DAMAGED),  # a position that isn't a real number
        (osr(text=b"10|1|inf|0"), DAMAGED),
        (osr(text=b"10|1|1|K1"), DAMAGED),  # keys that aren't a number
        (osr(text=b"\xff\xfe|1|1|0"), DAMAGED),
        (osr(text=bomb(MAX_FRAME_TEXT + 1)), "too large once unpacked"),
        (osr(frames=STABLE_START, seed=None), "no cursor frames"),  # placeholders only
        (osr(frames=[(-5, 1, 1, 0)], seed=None), "no cursor frames"),  # all step back
        (osr(text=b""), "no cursor frames"),
        (osr(version=LAZER), DAMAGED),  # lazer without its block: the file just ends
        (osr(version=LAZER, lazer=struct.pack("<i", 0)), "without its score data block"),
        (osr(version=LAZER, lazer=struct.pack("<i", -1)), "without its score data block"),
        (lazer_osr()[:-1], DAMAGED),  # the block cut short
        (osr(version=LAZER, lazer=struct.pack("<i", 5) + b"hello"), DAMAGED),  # not LZMA
        (osr(version=LAZER, lazer=packed(b"not json")), "unreadable score data"),
        (osr(version=LAZER, lazer=packed(b"\xff\xfe")), "unreadable score data"),
        (lazer_osr([{"mods": []}]), "unreadable score data"),  # JSON, but not an object
        (osr(version=LAZER, lazer=packed(bomb(MAX_LAZER_JSON + 1))), "too large once unpacked"),
        (lazer_osr({"mods": "HD"}), "unreadable mod list"),
        (lazer_osr({"mods": [{"acronym": 5}]}), "unreadable mod list"),
        (lazer_osr({"mods": [{"name": "HD"}]}), "unreadable mod list"),
        (lazer_osr({"mods": ["HD"]}), "unreadable mod list"),
    ],
    ids=case_id,
)
def test_errors(data: bytes, message: str) -> None:
    with pytest.raises(ReplayFileError, match=message):
        parse_replay(data)


@pytest.mark.parametrize("limit", [MAX_FRAME_TEXT, MAX_LAZER_JSON])
def test_a_bomb_is_small(limit: int) -> None:
    # The point of the limits: a tiny upload that would unpack to far more.
    assert len(compress(bomb(limit + 1))) < 10_000


@pytest.mark.parametrize(("size", "ok"), [(1000, True), (1001, False)])
def test_the_limit_is_exact(monkeypatch: pytest.MonkeyPatch, size: int, ok: bool) -> None:
    # A smaller limit for speed: frame text of exactly the limit is fine, one byte more isn't.
    monkeypatch.setattr(osrfile, "MAX_FRAME_TEXT", 1000)
    text = b"10|1|1|0,"
    data = osr(text=text + b"," * (size - len(text)))

    if ok:
        assert len(parse_replay(data).frames) == 1
    else:
        with pytest.raises(ReplayFileError, match="too large once unpacked"):
            parse_replay(data)


def test_cut_frame_data_is_an_error() -> None:
    # The stored length matches the bytes, and the file goes on after them, but the LZMA
    # stream inside stops early: the frames would be silently cut short.
    stream = compress(frame_text(BODY * 200, seed=None).encode())
    cut = stream[: len(stream) // 2]

    with pytest.raises(ReplayFileError, match=DAMAGED):
        parse_replay(osr(frame_data=struct.pack("<i", len(cut)) + cut))


def test_a_bomb_is_not_unpacked(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unpacking stops just past the limit: 20 MB of zeros never all land in memory.
    monkeypatch.setattr(osrfile, "MAX_FRAME_TEXT", 1000)
    data = osr(text=bomb(20 * 1024 * 1024))

    tracemalloc.start()
    try:
        with pytest.raises(ReplayFileError, match="too large once unpacked"):
            parse_replay(data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # LZMA's own dictionary takes 8 MB whatever the data; a full unpack would add 20 MB.
    assert peak < 16 * 1024 * 1024, f"peak {peak} bytes while unpacking"


def test_errors_keep_their_cause_out() -> None:
    # The uploader gets one fixed message, not struct/LZMA/UTF-8 internals.
    with pytest.raises(ReplayFileError) as exc:
        parse_replay(osr(player=b"\x0b\x02\xff\xfe"))

    assert str(exc.value) == "not a valid .osr file (cut short or damaged)"
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


@pytest.mark.parametrize("data", [osr(), lazer_osr()], ids=["stable", "lazer"])
def test_every_cut_is_an_error(data: bytes) -> None:
    # An upload cut off at any byte gives ReplayFileError, never another exception.
    for end in range(len(data)):
        with pytest.raises(ReplayFileError):
            parse_replay(data[:end])


@pytest.mark.parametrize("data", [osr(), lazer_osr()], ids=["stable", "lazer"])
def test_damaged_bytes_never_crash(data: bytes) -> None:
    # A changed byte may still parse (a hit count, say) or fail, but only as ReplayFileError.
    rng = random.Random(0)
    for i in range(len(data)):
        for flip in (0xFF, 1 << rng.randrange(8)):
            damaged = bytearray(data)
            damaged[i] ^= flip
            with contextlib.suppress(ReplayFileError):
                parse_replay(bytes(damaged))
