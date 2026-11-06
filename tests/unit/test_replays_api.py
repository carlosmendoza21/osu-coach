"""Unit tests for POST /replays/upload (osu_coach.api.replays).

No database, worker or osu!: storing the replay and the "is the map stored" check are
replaced with fakes, the session only answers the reload by id, and queueing the map
fetch and analysis is a fake (the get_queue dependency). The endpoint against the real stack is in
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
from osu_coach.models import Beatmap, Replay
from osu_coach.osrfile import ReplayFileError
from osu_coach.replays import StoredReplay
from tests.osrdata import CHECKSUM, PLAYED_AT

PARSED_AT = datetime(2026, 11, 4, 4, 26, 7, tzinfo=UTC)


def replay_row(id: int = 1, analyzed: bool = False) -> Replay:
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
        unstable_rate=95.5 if analyzed else None,
        mean_error_ms=-1.81 if analyzed else None,
        mean_aim_error_px=14.2 if analyzed else None,
        analyzed_at=PARSED_AT if analyzed else None,
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
        self.queued: list[tuple[int, str | None]] = []

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

    def queue(self, replay_id: int, fetch_map: str | None) -> api.QueuedJobs:
        self.queued.append((replay_id, fetch_map))
        return api.QueuedJobs("map-job" if fetch_map else None, "analysis-job")

    async def post(self, files: Any = None) -> httpx.Response:
        app = create_app()
        app.dependency_overrides[api.get_queue] = lambda: self.queue
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
            "unstable_rate": None,
            "mean_error_ms": None,
            "mean_aim_error_px": None,
            "analyzed_at": None,
        },
        "created": True,
        "map_job_id": "map-job",
        "analysis_job_id": "analysis-job",
    }
    # The exact bytes go to the store, with the configured data folder.
    assert env.stored == [(b"replay bytes", env.data_dir)]
    assert env.session.gets == [(Replay, 1)]
    assert env.queued == [(1, CHECKSUM)]  # the map wasn't stored: fetched, then analysis


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
async def test_a_stored_map_isnt_fetched(env: Env, created: bool) -> None:
    env.result, env.map_stored = StoredReplay(1, created=created), True

    resp = await env.upload()

    assert (resp.json()["map_job_id"], resp.json()["analysis_job_id"]) == (None, "analysis-job")
    assert env.map_checks == [CHECKSUM]
    assert env.queued == [(1, None)]  # analysis only


async def test_a_repeat_queues_a_missing_map_again(env: Env) -> None:
    # An earlier fetch may have failed (osu! down): uploading again retries it.
    env.result = StoredReplay(1, created=False)

    resp = await env.upload()

    assert resp.json()["map_job_id"] == "map-job"
    assert env.queued == [(1, CHECKSUM)]


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


async def test_an_analyzed_replay_queues_nothing(env: Env) -> None:
    env.result, env.session = StoredReplay(1, created=False), FakeSession(replay_row(analyzed=True))

    body = (await env.upload()).json()

    assert (body["map_job_id"], body["analysis_job_id"]) == (None, None)
    assert env.queued == [] and env.map_checks == []  # not even the map is looked up
    # The stored analysis comes back with the replay.
    analysis = {
        k: body["replay"][k]
        for k in ("unstable_rate", "mean_error_ms", "mean_aim_error_px", "analyzed_at")
    }
    assert analysis == {
        "unstable_rate": 95.5,
        "mean_error_ms": -1.81,
        "mean_aim_error_px": 14.2,
        "analyzed_at": "2026-11-04T04:26:07Z",
    }


# ---------- Queueing on the worker ----------


class FakeSignature:
    """Stands in for a Celery task signature: records how it's set up and sent."""

    def __init__(self, sent: list[Any], name: str, args: tuple[Any, ...]) -> None:
        self.sent, self.name, self.args = sent, name, args
        self.task_id: str | None = None

    def set(self, task_id: str) -> "FakeSignature":
        self.task_id = task_id
        return self

    def apply_async(self) -> SimpleNamespace:
        self.sent.append(("alone", self.name, self.args))
        return SimpleNamespace(id=f"{self.name}-id")


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    sent: list[Any] = []

    def task(name: str) -> SimpleNamespace:
        return SimpleNamespace(si=lambda *args: FakeSignature(sent, name, args))

    def chain(*sigs: FakeSignature) -> SimpleNamespace:
        def apply_async() -> SimpleNamespace:
            sent.append(("chain", [(s.name, s.args, s.task_id) for s in sigs]))
            return SimpleNamespace(id=f"{sigs[-1].name}-id")

        return SimpleNamespace(apply_async=apply_async)

    monkeypatch.setattr(api, "analyze_replay", task("analyze"))
    monkeypatch.setattr(api, "fetch_replay_map", task("fetch"))
    monkeypatch.setattr(api, "chain", chain)
    return sent


def test_a_stored_map_queues_the_analysis_alone(sent: list[Any]) -> None:
    jobs = api.queue_via_worker(7, None)

    assert jobs == api.QueuedJobs(None, "analyze-id")
    assert sent == [("alone", "analyze", (7,))]


def test_a_missing_map_is_fetched_first_in_a_chain(sent: list[Any]) -> None:
    jobs = api.queue_via_worker(7, CHECKSUM)

    [(kind, steps)] = sent
    assert kind == "chain"
    [(first, first_args, map_job_id), (second, second_args, _)] = steps
    # Fetch the map, then analyze: the analysis never runs if the fetch fails.
    assert (first, first_args, second, second_args) == ("fetch", (CHECKSUM,), "analyze", (7,))
    # The map job's id is chosen up front, so both ids can be returned.
    assert jobs == api.QueuedJobs(map_job_id, "analyze-id")
    assert map_job_id is not None and len(map_job_id) == 36  # a UUID


def test_each_chain_gets_its_own_map_job_id(sent: list[Any]) -> None:
    a, b = api.queue_via_worker(7, CHECKSUM), api.queue_via_worker(8, CHECKSUM)

    assert a.map_job_id != b.map_job_id


def test_queueing_uses_the_worker() -> None:
    assert api.get_queue() is api.queue_via_worker


# ---------- GET /replays/{id} ----------


def beatmap_row() -> Beatmap:
    return Beatmap(
        id=4767844, beatmapset_id=2287992, artist="Ardolf", title="Split EX",
        creator="mapper", version="REALITIES COLLIDE", star_rating=7.91, ar=9.7, od=9.5,
        cs=3, hp=5, bpm=200, length_s=184, checksum=CHECKSUM,
    )  # fmt: skip


class DetailSession:
    """Loads the replay and its map by id, or the map by checksum, recording how."""

    def __init__(self, replay: Replay | None, beatmap: Beatmap | None) -> None:
        self.replay, self.beatmap = replay, beatmap
        self.calls: list[str] = []

    async def get(self, model: type, key: int) -> Any:
        self.calls.append(f"get {model.__name__} {key}")
        return self.replay if model is Replay else self.beatmap

    async def scalar(self, statement: Any) -> Beatmap | None:
        self.calls.append("map by checksum")
        return self.beatmap


class DetailEnv:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.session = DetailSession(replay_row(), beatmap_row())
        self.judged: dict[str, int] | None = {"300": 1186, "100": 24, "50": 2, "miss": 9}
        self.queries: list[str] = []

        async def judged_counts(session: Any, replay_id: int) -> dict[str, int] | None:
            self.queries.append(f"judged {replay_id}")
            return self.judged

        async def by_pattern(session: Any, replay_id: int) -> dict[str, dict[str, int]]:
            self.queries.append(f"by pattern {replay_id}")
            return {"stream": {"300": 532, "100": 14, "50": 2, "miss": 8}}

        monkeypatch.setattr(api, "judged_counts", judged_counts)
        monkeypatch.setattr(api, "results_by_pattern", by_pattern)

    def analyzed(self) -> None:
        row = replay_row(analyzed=True)
        row.beatmap_id = 4767844
        self.session.replay = row

    async def get(self, path: str = "/replays/1") -> httpx.Response:
        app = create_app()

        async def session() -> AsyncIterator[DetailSession]:
            yield self.session

        app.dependency_overrides[get_session] = session
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)


@pytest.fixture
def denv(monkeypatch: pytest.MonkeyPatch) -> DetailEnv:
    return DetailEnv(monkeypatch)


async def test_an_analyzed_replay(denv: DetailEnv) -> None:
    denv.analyzed()

    resp = await denv.get()

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "analyzed"
    assert body["judged"] == {"300": 1186, "100": 24, "50": 2, "miss": 9}
    assert body["matches_official"] is True  # the row's official counts are the same
    assert body["by_pattern"] == {"stream": {"300": 532, "100": 14, "50": 2, "miss": 8}}
    assert body["replay"]["unstable_rate"] == 95.5
    assert body["beatmap"] == {
        "id": 4767844, "beatmapset_id": 2287992, "artist": "Ardolf", "title": "Split EX",
        "creator": "mapper", "version": "REALITIES COLLIDE", "star_rating": 7.91, "ar": 9.7,
        "od": 9.5, "cs": 3.0, "hp": 5.0, "bpm": 200.0, "length_s": 184,
    }  # fmt: skip
    # The map it was analyzed against, by id.
    assert denv.session.calls == ["get Replay 1", "get Beatmap 4767844"]
    assert denv.queries == ["judged 1", "by pattern 1"]


@pytest.mark.parametrize(
    ("judged", "matches"),
    [
        ({"300": 1186, "100": 24, "50": 2, "miss": 9}, True),
        ({"300": 1186, "100": 25, "50": 1, "miss": 9}, False),  # one 50 judged a 100
        ({"300": 1185, "100": 24, "50": 2, "miss": 10}, False),
    ],
)
async def test_matches_official(denv: DetailEnv, judged: dict[str, int], matches: bool) -> None:
    denv.analyzed()
    denv.judged = judged

    assert (await denv.get()).json()["matches_official"] is matches


async def test_a_pending_replay(denv: DetailEnv) -> None:
    resp = await denv.get()

    body = resp.json()
    assert (body["status"], body["judged"], body["matches_official"]) == ("pending", None, None)
    assert body["by_pattern"] == {}
    assert body["replay"]["unstable_rate"] is None
    assert denv.queries == []  # no results to look up yet
    # Not analyzed: the stored map with the replay's exact file, by checksum.
    assert body["beatmap"]["id"] == 4767844
    assert denv.session.calls == ["get Replay 1", "map by checksum"]


async def test_a_pending_replay_without_its_map(denv: DetailEnv) -> None:
    denv.session.beatmap = None

    body = (await denv.get()).json()

    assert (body["status"], body["beatmap"]) == ("pending", None)


async def test_an_unknown_replay_is_404(denv: DetailEnv) -> None:
    denv.session.replay = None

    resp = await denv.get("/replays/999")

    assert resp.status_code == 404
    assert resp.json() == {"detail": "replay 999 not found"}
    assert denv.session.calls == ["get Replay 999"] and denv.queries == []


@pytest.mark.parametrize("path", ["/replays/abc", "/replays/1.5"])
async def test_an_id_that_isnt_a_number_is_422(denv: DetailEnv, path: str) -> None:
    resp = await denv.get(path)

    assert resp.status_code == 422
    assert denv.session.calls == []


def test_get_replay_is_documented() -> None:
    operation = create_app().openapi()["paths"]["/replays/{replay_id}"]["get"]

    assert set(operation["responses"]) == {"200", "404", "422"}
