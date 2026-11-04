"""Unit tests for POST /replays/upload (osu_coach.api.replays).

No database, worker or osu!: storing the replay and the "is the map stored" check are
replaced with fakes, the session only answers the reload by id, and queueing the map
fetch is a fake (the get_queue_map dependency). The endpoint against the real stack is in
tests/integration/test_api_live.py.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from osu_coach.api import replays as api
from osu_coach.db import get_session
from osu_coach.main import create_app
from osu_coach.models import Replay
from osu_coach.osrfile import ReplayFileError
from osu_coach.replays import StoredReplay
from osu_coach.worker import fetch_replay_map
from tests.osrdata import CHECKSUM, PLAYED_AT

PARSED_AT = datetime(2026, 11, 4, 4, 26, 7, tzinfo=UTC)


def replay_row(id: int = 1) -> Replay:
    return Replay(
        id=id,
        file_sha256="f" * 64,
        beatmap_checksum=CHECKSUM,
        score_id=6604281970,
        player_name="gwb",
        mods=["HD"],
        lazer=True,
        game_version=30000016,
        count_300=1186,
        count_100=24,
        count_50=2,
        count_geki=0,
        count_katu=0,
        count_miss=9,
        score=724862,
        max_combo=753,
        perfect=False,
        played_at=PLAYED_AT,
        frame_count=12626,
        frames_path="frames/x.parquet",
        parsed_at=PARSED_AT,
    )


class FakeSession:
    """Only the reload of the stored row goes through the session."""

    def __init__(self, row: Replay | None) -> None:
        self.row = row
        self.gets: list[tuple[type, int]] = []

    async def get(self, model: type, key: int) -> Replay | None:
        self.gets.append((model, key))
        return self.row


class Env:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.result: StoredReplay | Exception = StoredReplay(1, created=True)
        self.map_stored = False
        self.session = FakeSession(replay_row())
        self.stored: list[tuple[bytes, Path]] = []
        self.map_checks: list[str] = []
        self.queued: list[str] = []

        async def store(session: Any, data_dir: Path, data: bytes) -> StoredReplay:
            self.stored.append((data, data_dir))
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

        async def stored_map(session: Any, checksum: str) -> int | None:
            self.map_checks.append(checksum)
            return 4767844 if self.map_stored else None

        monkeypatch.setattr(api, "store_replay", store)
        monkeypatch.setattr(api, "stored_map", stored_map)

    def queue(self, checksum: str) -> str:
        self.queued.append(checksum)
        return f"job-{len(self.queued)}"

    async def post(self, files: Any = None) -> httpx.Response:
        app = create_app()
        app.dependency_overrides[api.get_queue_map] = lambda: self.queue
        app.dependency_overrides[api.get_data_dir] = lambda: self.data_dir

        async def session() -> AsyncIterator[FakeSession]:
            yield self.session

        app.dependency_overrides[get_session] = session
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/replays/upload", files=files)

    async def upload(self, data: bytes = b"replay bytes") -> httpx.Response:
        return await self.post({"file": ("play.osr", data, "application/octet-stream")})


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Env:
    return Env(monkeypatch, tmp_path)


# ---------- Uploading ----------


async def test_a_new_replay(env: Env) -> None:
    resp = await env.upload(b"replay bytes")

    assert resp.status_code == 201
    assert resp.json() == {
        "replay": {
            "id": 1,
            "beatmap_checksum": CHECKSUM,
            "score_id": 6604281970,
            "player_name": "gwb",
            "mods": ["HD"],
            "lazer": True,
            "count_300": 1186,
            "count_100": 24,
            "count_50": 2,
            "count_miss": 9,
            "score": 724862,
            "max_combo": 753,
            "perfect": False,
            "played_at": "2026-04-28T02:17:02.332400Z",
            "frame_count": 12626,
            "parsed_at": "2026-11-04T04:26:07Z",
        },
        "created": True,
        "map_job_id": "job-1",
    }
    # The exact bytes go to the store, with the configured data folder.
    assert env.stored == [(b"replay bytes", env.data_dir)]
    assert env.session.gets == [(Replay, 1)]
    assert env.queued == [CHECKSUM]  # the map wasn't stored, so it's fetched


async def test_internal_fields_are_not_sent(env: Env) -> None:
    body = (await env.upload()).json()

    # The file's hash, the frames' path on disk and the raw game version stay inside.
    assert {"file_sha256", "frames_path", "game_version"}.isdisjoint(body["replay"])


async def test_the_same_file_again_is_200(env: Env) -> None:
    env.result = StoredReplay(1, created=False)

    resp = await env.upload()

    assert resp.status_code == 200
    assert resp.json()["created"] is False


@pytest.mark.parametrize("created", [True, False], ids=["new", "repeat"])
async def test_a_stored_map_isnt_queued(env: Env, created: bool) -> None:
    env.result, env.map_stored = StoredReplay(1, created=created), True

    resp = await env.upload()

    assert resp.json()["map_job_id"] is None
    assert env.map_checks == [CHECKSUM]
    assert env.queued == []


async def test_a_repeat_queues_a_missing_map_again(env: Env) -> None:
    # An earlier fetch may have failed (osu! down): uploading again retries it.
    env.result = StoredReplay(1, created=False)

    resp = await env.upload()

    assert resp.json()["map_job_id"] == "job-1"
    assert env.queued == [CHECKSUM]


# ---------- Refused uploads ----------


async def test_a_bad_file_is_422_with_the_reason(env: Env) -> None:
    env.result = ReplayFileError("only osu! standard replays are supported, not taiko")

    resp = await env.upload()

    assert resp.status_code == 422
    assert resp.json() == {"detail": "only osu! standard replays are supported, not taiko"}
    assert env.session.gets == [] and env.queued == []  # nothing looked up or queued


@pytest.mark.parametrize(
    ("size", "status"),
    [
        (api.MAX_UPLOAD_BYTES, 201),  # exactly the limit is fine
        (api.MAX_UPLOAD_BYTES + 1, 413),  # one byte more is not
    ],
)
async def test_the_size_limit(env: Env, size: int, status: int) -> None:
    resp = await env.upload(b"0" * size)

    assert resp.status_code == status
    if status == 413:
        assert resp.json() == {"detail": "a replay is at most 4 MB"}
        assert env.stored == []  # never parsed
    else:
        assert len(env.stored[0][0]) == size  # passed on whole


async def test_an_empty_file_goes_to_the_parser(env: Env) -> None:
    # Zero bytes is no special case: the parser refuses it like any broken file.
    await env.upload(b"")

    assert env.stored == [(b"", env.data_dir)]


@pytest.mark.parametrize(
    "files",
    [None, {"replay": ("play.osr", b"x", "application/octet-stream")}],
    ids=["no file", "wrong field name"],
)
async def test_a_missing_file_is_422(env: Env, files: Any) -> None:
    resp = await env.post(files)

    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "file"]
    assert env.stored == []


async def test_a_row_missing_after_storing_is_500(env: Env) -> None:
    env.session = FakeSession(None)

    resp = await env.upload()

    assert resp.status_code == 500
    assert env.queued == []


# ---------- Wiring ----------


def test_the_endpoint_is_documented() -> None:
    operation = create_app().openapi()["paths"]["/replays/upload"]["post"]

    assert set(operation["responses"]) == {"200", "201", "413", "422"}
    assert "multipart/form-data" in operation["requestBody"]["content"]


def test_queueing_uses_the_worker_task(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    def delay(checksum: str) -> SimpleNamespace:
        sent.append(checksum)
        return SimpleNamespace(id="celery-task-id")

    monkeypatch.setattr(fetch_replay_map, "delay", delay)

    assert api.get_queue_map()(CHECKSUM) == "celery-task-id"
    assert sent == [CHECKSUM]
