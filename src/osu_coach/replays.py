"""Store an uploaded replay: the original file, its frames as Parquet, and a database row."""

import hashlib
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import polars as pl
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach.models import Replay
from osu_coach.osrfile import Frame, parse_replay

UPLOADS = "uploads"  # the original .osr files, so replays can be read again later
FRAMES = "frames"  # one Parquet file of frames per replay

# Column types for the frames. 64-bit, so no value from a hand-made file can overflow.
FRAME_SCHEMA = {"time_ms": pl.Int64, "x": pl.Float64, "y": pl.Float64, "keys": pl.Int64}


@dataclass(frozen=True)
class StoredReplay:
    id: int
    created: bool  # False if this exact file was uploaded before


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def frames_frame(frames: tuple[Frame, ...]) -> pl.DataFrame:
    """The frames as a table: one row per frame, in time order."""
    return pl.DataFrame(
        {
            "time_ms": [f.time_ms for f in frames],
            "x": [f.x for f in frames],
            "y": [f.y for f in frames],
            "keys": [f.keys for f in frames],
        },
        schema=FRAME_SCHEMA,
    )


def load_frames(data_dir: Path, frames_path: str) -> pl.DataFrame:
    """A stored replay's frames, from the path in its database row."""
    return pl.read_parquet(data_dir / frames_path)


async def store_replay(session: AsyncSession, data_dir: Path, data: bytes) -> StoredReplay:
    """Parse an .osr upload and store it. Uploading the same file again returns the same id.

    Raises ReplayFileError (from osu_coach.osrfile) for a file that isn't a valid osu!
    standard replay; nothing is stored then.
    """
    digest = sha256_hex(data)
    existing = await session.scalar(select(Replay.id).where(Replay.file_sha256 == digest))
    if existing is not None:
        return StoredReplay(existing, created=False)

    replay = parse_replay(data)

    # Files first, named by the file's hash: if the database write fails, a retry writes
    # the same files again, so nothing is left half-done or duplicated.
    frames_path = f"{FRAMES}/{digest}.parquet"
    _write_atomic(data_dir / UPLOADS / f"{digest}.osr", lambda p: p.write_bytes(data))
    _write_atomic(data_dir / frames_path, frames_frame(replay.frames).write_parquet)

    # ON CONFLICT: if the same file is being stored at the same moment, keep one row.
    stmt = (
        insert(Replay)
        .values(
            file_sha256=digest,
            beatmap_checksum=replay.beatmap_checksum,
            score_id=replay.score_id,
            player_name=replay.player,
            mods=list(replay.mods),
            lazer=replay.lazer,
            game_version=replay.game_version,
            count_300=replay.count_300,
            count_100=replay.count_100,
            count_50=replay.count_50,
            count_geki=replay.count_geki,
            count_katu=replay.count_katu,
            count_miss=replay.count_miss,
            score=replay.score,
            max_combo=replay.max_combo,
            perfect=replay.perfect,
            played_at=replay.played_at,
            frame_count=len(replay.frames),
            frames_path=frames_path,
        )
        .on_conflict_do_nothing(index_elements=["file_sha256"])
        .returning(Replay.id)
    )
    new_id = await session.scalar(stmt)
    await session.commit()
    if new_id is None:  # the other upload won
        other = await session.scalar(select(Replay.id).where(Replay.file_sha256 == digest))
        assert other is not None
        return StoredReplay(other, created=False)
    return StoredReplay(new_id, created=True)


def _write_atomic(path: Path, write: Callable[[Path], object]) -> None:
    # Write to a temporary file, then rename: a reader never sees a half-written file.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")  # unique per writer
    try:
        write(tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
