"""Integration tests: the running app, end to end, over real HTTP.

Run with:  uv run pytest -m integration
Needs:     the full stack (docker compose up -d --build) with osu! credentials in .env, so
           the API, worker, Postgres, Redis and real osu! are all involved. Skips with the
           reason if the API isn't answering on localhost:8000.
Note: like the app itself, this syncs players and stores replays into the dev database.
"""

import time
from collections.abc import Iterator

import httpx
import pytest
from sqlalchemy import create_engine, text

from osu_coach.config import get_settings
from tests.osrdata import osr
from tests.support import load

pytestmark = pytest.mark.integration

API = "http://localhost:8000"
TIMEOUT_S = 60  # a first sync goes through the worker and osu!


@pytest.fixture(scope="module")
def api() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=API, timeout=TIMEOUT_S) as client:
        try:
            client.get("/health", timeout=3)
        except httpx.TransportError:
            pytest.skip(f"The API isn't running at {API}. Start it: docker compose up -d --build")
        yield client


def test_health(api: httpx.Client) -> None:
    resp = api.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_player_profile_and_plays(api: httpx.Client) -> None:
    resp = api.get("/players/peppy")

    assert resp.status_code == 200
    body = resp.json()
    assert (body["player"]["osu_id"], body["player"]["username"]) == (2, "peppy")
    pps = [s["pp"] for s in body["best"]]
    assert len(pps) == 50, "Day 2's goal: the top 50 plays, from the database"
    assert None not in pps
    assert pps == sorted(pps, reverse=True)
    assert len(body["recent"]) <= 20


def test_repeat_request_is_served_from_the_database(api: httpx.Client) -> None:
    first = api.get("/players/peppy").json()
    second = api.get("/players/PEPPY").json()

    # Same sync time: the second request didn't sync again (fresh within the window).
    assert second["player"]["updated_at"] == first["player"]["updated_at"]
    assert second == first


def test_unknown_player_is_404(api: httpx.Client) -> None:
    resp = api.get("/players/no-such-player-osu-coach-test")

    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


def test_endpoint_is_documented(api: httpx.Client) -> None:
    paths = api.get("/openapi.json").json()["paths"]

    assert "/players/{username}" in paths


# ---------- Uploading a replay ----------

MAP_22423 = load("beatmap.json")["checksum"]


def dev_db(sql: str) -> None:
    engine = create_engine(get_settings().database_url)
    try:
        with engine.begin() as conn:
            conn.execute(text(sql))
    finally:
        engine.dispose()


def map_objects() -> object:
    engine = create_engine(get_settings().database_url)
    try:
        with engine.connect() as conn:
            sql = "SELECT count(*) FROM hit_objects WHERE beatmap_id = 22423"
            return conn.execute(text(sql)).scalar()
    finally:
        engine.dispose()


def test_uploading_a_replay_stores_it_and_its_map(api: httpx.Client) -> None:
    # Day 3's goal: uploading a replay stores its map, the map's hit objects, and its
    # frames. A built replay on map 22423, whose objects are removed first so the upload
    # has to fetch them.
    dev_db("DELETE FROM hit_objects WHERE beatmap_id = 22423")
    data = osr(checksum=MAP_22423, player="osu-coach-test")

    resp = api.post("/replays/upload", files={"file": ("test.osr", data)})

    assert resp.status_code in (200, 201)  # 200 if an earlier run uploaded the same file
    body = resp.json()
    assert body["created"] is (resp.status_code == 201)
    assert body["replay"]["beatmap_checksum"] == MAP_22423
    assert body["replay"]["frame_count"] == 3
    assert body["map_job_id"] is not None  # the map's objects were missing: fetched

    deadline = time.monotonic() + TIMEOUT_S
    while map_objects() != 254:
        assert time.monotonic() < deadline, "the worker didn't store the map's objects"
        time.sleep(1)

    # Now stored: uploading again finds the same replay and queues nothing.
    again = api.post("/replays/upload", files={"file": ("test.osr", data)})
    assert again.status_code == 200
    assert again.json()["replay"]["id"] == body["replay"]["id"]
    assert again.json()["map_job_id"] is None


def test_a_bad_upload_is_refused(api: httpx.Client) -> None:
    resp = api.post("/replays/upload", files={"file": ("bad.osr", b"not a replay")})

    assert resp.status_code == 422
    assert resp.json() == {"detail": "not a valid .osr file (cut short or damaged)"}
