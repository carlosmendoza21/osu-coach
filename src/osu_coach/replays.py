"""Uploaded replays: storing the file, its frames and a row, then fetching the map."""

import asyncio
import hashlib
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import polars as pl
from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach.beatmaps import fetch_beatmap
from osu_coach.models import Beatmap, BeatmapObject, Replay
from osu_coach.osrfile import Frame, parse_replay
from osu_coach.osu.client import OsuClient
from osu_coach.osu.files import BeatmapFiles

UPLOADS = "uploads"  # the original .osr files, so replays can be read again later
FRAMES = "frames"  # one Parquet file of frames per replay

# Column types for the frames. 64-bit, so no value from a hand-made file can overflow.
FRAME_SCHEMA = {"time_ms": pl.Int64, "x": pl.Float64, "y": pl.Float64, "keys": pl.Int64}


class MapChangedError(ValueError):
    """osu! has a newer version of the map than the one the replay was played on."""


@dataclass(frozen=True)
class StoredReplay:
    id: int
    created: bool  # False if this exact file was uploaded before


@dataclass(frozen=True)
class ReplayMap:
    beatmap_id: int
    fetched: bool  # False if the map and its hit objects were already stored


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

    # Parsing and writing take up to ~100 ms for a long replay: in a thread, so the API
    # keeps answering other requests meanwhile.
    replay = await asyncio.to_thread(parse_replay, data)

    # Files first, named by the file's hash: if the database write fails, a retry writes
    # the same files again, so nothing is left half-done or duplicated.
    frames_path = f"{FRAMES}/{digest}.parquet"
    await asyncio.to_thread(_write_files, data_dir, digest, data, frames_frame(replay.frames))

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


async def stored_map(session: AsyncSession, checksum: str) -> int | None:
    """The id of the stored map with this exact file, if its hit objects are stored too."""
    has_objects = exists().where(BeatmapObject.beatmap_id == Beatmap.id)
    stmt = select(Beatmap.id).where(Beatmap.checksum == checksum.lower(), has_objects)
    return await session.scalar(stmt)


async def fetch_replay_map(
    osu: OsuClient, files: BeatmapFiles, session: AsyncSession, checksum: str
) -> ReplayMap:
    """Make sure the map a replay was played on is stored, with its hit objects.

    Raises OsuNotFoundError if osu! doesn't know the checksum (a deleted map, or a version
    replaced by an update), and MapChangedError if osu!'s file no longer matches it.
    """
    if (beatmap_id := await stored_map(session, checksum)) is not None:
        return ReplayMap(beatmap_id, fetched=False)

    beatmap = await osu.lookup_beatmap(checksum)
    await fetch_beatmap(osu, files, session, beatmap.id)
    # fetch_beatmap stores osu!'s current file. If the map was updated after the play,
    # that's a different file: its hit objects aren't the ones the player saw.
    if await stored_map(session, checksum) is None:
        raise MapChangedError(f"map {beatmap.id} has changed since this replay was played")
    return ReplayMap(beatmap.id, fetched=True)


def _write_files(data_dir: Path, digest: str, data: bytes, frames: pl.DataFrame) -> None:
    _write_atomic(data_dir / UPLOADS / f"{digest}.osr", lambda p: p.write_bytes(data))
    _write_atomic(data_dir / FRAMES / f"{digest}.parquet", frames.write_parquet)


def _write_atomic(path: Path, write: Callable[[Path], object]) -> None:
    # Write to a temporary file, then rename: a reader never sees a half-written file.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")  # unique per writer
    try:
        write(tmp)
        try:
            os.replace(tmp, path)
        except PermissionError:
            # Windows refuses to replace a file another writer is replacing at the same
            # moment. Files are named by their content's hash, so if it's there now, the
            # other writer put the same bytes there.
            if not path.exists():
                raise
    finally:
        tmp.unlink(missing_ok=True)
