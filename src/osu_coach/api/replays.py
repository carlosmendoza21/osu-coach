"""Replays: POST /replays/upload stores an .osr replay, then fetches its map and analyzes
it in the background; GET /replays/{id} shows it with its analysis."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from celery import chain
from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach.analysis import judged_counts, results_by_pattern
from osu_coach.config import get_settings
from osu_coach.db import get_session
from osu_coach.models import Beatmap, Replay
from osu_coach.osrfile import ReplayFileError
from osu_coach.replays import store_replay, stored_map
from osu_coach.worker import analyze_replay, fetch_replay_map

router = APIRouter(tags=["replays"])

# A 6-minute replay is about 110 KB; even a 30-minute marathon stays far below this.
MAX_UPLOAD_BYTES = 4 * 1024 * 1024


class ReplayOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)  # build it straight from a Replay row

    id: int
    beatmap_checksum: str
    score_id: int | None
    player_name: str
    mods: list[str]
    lazer: bool
    count_300: int
    count_100: int
    count_50: int
    count_miss: int
    score: int
    max_combo: int
    perfect: bool
    played_at: datetime
    frame_count: int
    parsed_at: datetime
    # Set once the replay is analyzed (see osu_coach.analysis); null until then.
    unstable_rate: float | None
    mean_error_ms: float | None
    mean_aim_error_px: float | None
    analyzed_at: datetime | None


class UploadResponse(BaseModel):
    replay: ReplayOut
    created: bool  # False if this exact file was uploaded before
    # The worker job fetching the replay's map; None if the map is already stored.
    map_job_id: str | None
    # The job analyzing the replay (after the map job, if there is one); None if the
    # replay was analyzed before.
    analysis_job_id: str | None


class BeatmapOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    beatmapset_id: int
    artist: str
    title: str
    creator: str
    version: str  # the difficulty name, e.g. "Hard"
    star_rating: float
    ar: float
    od: float
    cs: float
    hp: float
    bpm: float
    length_s: int


class ReplayDetail(BaseModel):
    replay: ReplayOut
    # The map the replay was played on, once it's stored (that exact version).
    beatmap: BeatmapOut | None
    # "pending" until the analysis is stored; the fields below are empty until then.
    status: Literal["pending", "analyzed"]
    # Our own 300/100/50/miss counts, and whether they equal the ones osu! wrote into
    # the file: the check that the analysis is right.
    judged: dict[str, int] | None
    matches_official: bool | None
    # {pattern: {result: count}}, e.g. {"stream": {"300": 532, ..., "miss": 8}}
    by_pattern: dict[str, dict[str, int]]


# ---------- Dependencies: tests replace these ----------


@dataclass(frozen=True)
class QueuedJobs:
    map_job_id: str | None
    analysis_job_id: str


# (replay id, the map's checksum if the map must be fetched first) -> the jobs queued
QueueFn = Callable[[int, str | None], QueuedJobs]


def queue_via_worker(replay_id: int, fetch_map: str | None) -> QueuedJobs:
    analyze = analyze_replay.si(replay_id)  # si: takes no result from a job before it
    if fetch_map is None:
        return QueuedJobs(None, str(analyze.apply_async().id))
    # A chain: the analysis starts only once the map is stored, and not at all if
    # fetching it fails. The map job gets its id here, so both ids can be returned.
    map_job_id = str(uuid.uuid4())
    result = chain(fetch_replay_map.si(fetch_map).set(task_id=map_job_id), analyze).apply_async()
    return QueuedJobs(map_job_id, str(result.id))


def get_queue() -> QueueFn:
    return queue_via_worker


def get_data_dir() -> Path:
    return get_settings().data_dir


# ---------- Endpoint ----------


@router.post(
    "/replays/upload",
    response_model=UploadResponse,
    status_code=201,
    responses={
        200: {"description": "This exact file was uploaded before", "model": UploadResponse},
        413: {"description": "The file is too large to be a replay"},
        422: {"description": "The file isn't a valid osu! standard replay"},
    },
)
async def upload_replay(
    file: UploadFile,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    data_dir: Annotated[Path, Depends(get_data_dir)],
    queue: Annotated[QueueFn, Depends(get_queue)],
) -> UploadResponse:
    # One byte past the limit is enough to know the file is too large.
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"a replay is at most {MAX_UPLOAD_BYTES // 1024 // 1024} MB")

    try:
        stored = await store_replay(session, data_dir, data)
    except ReplayFileError as error:
        raise HTTPException(422, str(error)) from None

    replay = await session.get(Replay, stored.id)
    if replay is None:  # store_replay just returned it, so this means something is wrong
        raise HTTPException(500, "replay missing after storing it")
    if not stored.created:
        response.status_code = 200

    # Queued again on a repeat upload if the replay still isn't analyzed (an earlier job
    # may have failed, say while osu! was down). A map that's stored isn't fetched again.
    jobs = None
    if replay.analyzed_at is None:
        map_missing = await stored_map(session, replay.beatmap_checksum) is None
        jobs = queue(replay.id, replay.beatmap_checksum if map_missing else None)

    return UploadResponse(
        replay=ReplayOut.model_validate(replay),
        created=stored.created,
        map_job_id=jobs.map_job_id if jobs else None,
        analysis_job_id=jobs.analysis_job_id if jobs else None,
    )


@router.get(
    "/replays/{replay_id}",
    response_model=ReplayDetail,
    responses={404: {"description": "No replay has this id"}},
)
async def get_replay(
    replay_id: int, session: Annotated[AsyncSession, Depends(get_session)]
) -> ReplayDetail:
    replay = await session.get(Replay, replay_id)
    if replay is None:
        raise HTTPException(404, f"replay {replay_id} not found")

    # The map it was analyzed against, or before that, the stored map with its file.
    if replay.beatmap_id is not None:
        beatmap = await session.get(Beatmap, replay.beatmap_id)
    else:
        stmt = select(Beatmap).where(Beatmap.checksum == replay.beatmap_checksum)
        beatmap = await session.scalar(stmt)

    analyzed = replay.analyzed_at is not None
    judged = await judged_counts(session, replay_id) if analyzed else None
    official = {
        "300": replay.count_300,
        "100": replay.count_100,
        "50": replay.count_50,
        "miss": replay.count_miss,
    }
    return ReplayDetail(
        replay=ReplayOut.model_validate(replay),
        beatmap=BeatmapOut.model_validate(beatmap) if beatmap is not None else None,
        status="analyzed" if analyzed else "pending",
        judged=judged,
        matches_official=judged == official if judged is not None else None,
        by_pattern=await results_by_pattern(session, replay_id) if analyzed else {},
    )
