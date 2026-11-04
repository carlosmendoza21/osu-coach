"""POST /replays/upload: store an .osr replay, and fetch its map in the background."""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach.config import get_settings
from osu_coach.db import get_session
from osu_coach.models import Replay
from osu_coach.osrfile import ReplayFileError
from osu_coach.replays import store_replay, stored_map
from osu_coach.worker import fetch_replay_map

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


class UploadResponse(BaseModel):
    replay: ReplayOut
    created: bool  # False if this exact file was uploaded before
    # The worker job fetching the replay's map; None if the map is already stored.
    map_job_id: str | None


# ---------- Dependencies: tests replace these ----------

QueueMapFn = Callable[[str], str]  # a map's checksum -> the job's id


def queue_map_via_worker(checksum: str) -> str:
    return str(fetch_replay_map.delay(checksum).id)


def get_queue_map() -> QueueMapFn:
    return queue_map_via_worker


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
    queue_map: Annotated[QueueMapFn, Depends(get_queue_map)],
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

    # Queued again on a repeat upload if the map still isn't stored (an earlier fetch may
    # have failed); a map that's already stored is never fetched again.
    map_job_id = None
    if await stored_map(session, replay.beatmap_checksum) is None:
        map_job_id = queue_map(replay.beatmap_checksum)

    return UploadResponse(
        replay=ReplayOut.model_validate(replay), created=stored.created, map_job_id=map_job_id
    )
