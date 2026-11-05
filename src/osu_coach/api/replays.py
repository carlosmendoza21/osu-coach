"""POST /replays/upload: store an .osr replay, then fetch its map and analyze it in the
background."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated

from celery import chain
from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach.config import get_settings
from osu_coach.db import get_session
from osu_coach.models import Replay
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
