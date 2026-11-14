"""The data behind an analyzed replay's charts: each judged note, and where the cursor was.

- The hit-error histogram and UR over time come from each note's timing error.
- The heatmap shows the cursor's time on each part of the playfield, with the notes on it.

Timing is in real time (DT's 15 ms of song time is 10 ms), as the analysis's UR is.
Note positions are as the player saw them: flipped upside down with HR, so they line up
with the cursor.
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path

import polars as pl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach.difficulty import SPEED, play_conditions
from osu_coach.judge import PLAYFIELD_HEIGHT
from osu_coach.models import Beatmap, BeatmapObject, Replay, ReplayHit
from osu_coach.osufile import Difficulty
from osu_coach.replays import load_frames

PLAYFIELD_WIDTH = 512
# The heatmap's grid: 8 x 8 osu! pixels per cell, fine enough to see a circle (CS 4 is
# 73 px wide) and small enough to send (3,072 cells).
GRID_COLS = 64
GRID_ROWS = 48


@dataclass(frozen=True)
class NotePoint:
    idx: int  # the note's place in the map
    time_ms: int  # when it should be hit (song time: the map's clock, for a time axis)
    result: str  # "300", "100", "50" or "miss"
    error_ms: float | None  # real time; negative is early; None if never pressed
    aim_error_px: float | None
    x: float  # as the player saw it (flipped with HR)
    y: float
    pattern: str  # jump, stream, slider, other...


@dataclass(frozen=True)
class CursorGrid:
    cols: int
    rows: int
    # cols x rows counts of frames, row by row from the top-left; frames off the
    # playfield (the cursor can leave it) aren't counted.
    counts: list[int]
    off_playfield: int


@dataclass(frozen=True)
class Windows:
    """How far from a note's time (either way, in ms of real time) each result counts."""

    great: float  # a 300
    ok: float  # a 100
    meh: float  # a 50


@dataclass(frozen=True)
class ReplayCharts:
    rate: float  # the song speed: 1.5 with DT, 0.75 with HT
    windows: Windows
    notes: list[NotePoint]
    cursor: CursorGrid


def song_rate(mods: list[str]) -> float:
    return next((SPEED[m] for m in mods if m in SPEED), 1.0)


async def note_points(session: AsyncSession, replay: Replay) -> list[NotePoint]:
    """Each judged note of an analyzed replay, in map order, with its map position."""
    stmt = (
        select(ReplayHit, BeatmapObject)
        .join(
            BeatmapObject,
            (BeatmapObject.beatmap_id == replay.beatmap_id) & (BeatmapObject.idx == ReplayHit.idx),
        )
        .where(ReplayHit.replay_id == replay.id)
        .order_by(ReplayHit.idx)
    )
    rate = song_rate(replay.mods)
    flip = "HR" in replay.mods
    return [
        NotePoint(
            idx=hit.idx,
            time_ms=obj.time_ms,
            result=hit.result,
            # Two decimals: finer than any chart shows, and a much smaller response.
            error_ms=round(hit.error_ms / rate, 2) if hit.error_ms is not None else None,
            aim_error_px=round(hit.aim_error_px, 2) if hit.aim_error_px is not None else None,
            x=obj.x,
            y=PLAYFIELD_HEIGHT - obj.y if flip else obj.y,
            pattern=obj.pattern,
        )
        for hit, obj in await session.execute(stmt)
    ]


def cursor_grid(frames: pl.DataFrame, cols: int = GRID_COLS, rows: int = GRID_ROWS) -> CursorGrid:
    """How many frames the cursor spent in each cell of the playfield."""
    on = frames.filter(
        (pl.col("x") >= 0)
        & (pl.col("x") < PLAYFIELD_WIDTH)
        & (pl.col("y") >= 0)
        & (pl.col("y") < PLAYFIELD_HEIGHT)
    )
    cells = (
        on.select(
            (pl.col("y") * rows / PLAYFIELD_HEIGHT).floor().cast(pl.Int64).alias("row"),
            (pl.col("x") * cols / PLAYFIELD_WIDTH).floor().cast(pl.Int64).alias("col"),
        )
        .group_by("row", "col")
        .len()
    )
    counts = [0] * (cols * rows)
    for row, col, n in cells.iter_rows():
        counts[row * cols + col] = n
    return CursorGrid(cols, rows, counts, off_playfield=frames.height - on.height)


def hit_windows(beatmap: Beatmap, mods: list[str]) -> Windows:
    """The play's 300/100/50 windows in real time, from the map's OD and the mods."""
    # The slider fields don't affect hit windows; play_conditions just needs a Difficulty.
    difficulty = Difficulty(
        hp=beatmap.hp, cs=beatmap.cs, od=beatmap.od, ar=beatmap.ar,
        slider_multiplier=1, slider_tick_rate=1,
    )  # fmt: skip
    conditions = play_conditions(difficulty, mods)
    w, rate = conditions.windows, conditions.rate
    return Windows(round(w.great / rate, 2), round(w.ok / rate, 2), round(w.meh / rate, 2))


async def replay_charts(session: AsyncSession, data_dir: Path, replay: Replay) -> ReplayCharts:
    """The charts' data for an analyzed replay (replay.beatmap_id set)."""
    beatmap = await session.get(Beatmap, replay.beatmap_id)
    if beatmap is None:  # analysis set beatmap_id, and analyzed maps can't be deleted
        raise LookupError(f"replay {replay.id}'s map {replay.beatmap_id} is missing")
    frames = await asyncio.to_thread(load_frames, data_dir, replay.frames_path)
    return ReplayCharts(
        rate=song_rate(replay.mods),
        windows=hit_windows(beatmap, replay.mods),
        notes=await note_points(session, replay),
        cursor=await asyncio.to_thread(cursor_grid, frames),
    )
