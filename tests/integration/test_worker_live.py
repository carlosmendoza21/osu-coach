"""Integration tests: osu_sync through the real Celery worker.

Run with:  uv run pytest -m integration
Needs:     the full stack (docker compose up -d), with osu! credentials in .env. Skips with
           the reason if no worker answers.
Note: the worker writes to the dev database, like the real app. Syncing is idempotent,
so running this again changes nothing but the synced player's latest values.
"""

import time

import pytest
from sqlalchemy import create_engine, text

from osu_coach.analysis import ReplayNotFoundError
from osu_coach.beatmaps import UnsupportedModeError
from osu_coach.config import get_settings
from osu_coach.osu.client import OsuNotFoundError
from osu_coach.worker import (
    analyze_replay,
    celery_app,
    fetch_beatmap,
    fetch_replay_map,
    osu_sync,
)
from tests.support import load

pytestmark = pytest.mark.integration

TIMEOUT_S = 90


@pytest.fixture(autouse=True)
def worker_running() -> None:
    try:
        replies = celery_app.control.ping(timeout=3)
    except Exception as e:
        pytest.skip(f"Redis (the Celery broker) not reachable: {e}")
    if not replies:
        pytest.skip("No Celery worker answered. Start it: docker compose up -d worker")


def test_sync_through_the_worker() -> None:
    result = osu_sync.delay("peppy").get(timeout=TIMEOUT_S)

    assert result["osu_id"] == 2
    assert result["username"] == "peppy"
    assert 50 <= result["scores"] <= 100


def test_worker_sync_is_idempotent() -> None:
    first = osu_sync.delay(2).get(timeout=TIMEOUT_S)
    second = osu_sync.delay(2).get(timeout=TIMEOUT_S)

    engine = create_engine(get_settings().database_url)
    with engine.connect() as conn:
        players = conn.execute(text("SELECT count(*) FROM players WHERE osu_id = 2")).scalar()
        scores = conn.execute(text("SELECT count(*) FROM scores WHERE player_id = 2")).scalar()
    engine.dispose()

    assert players == 1
    # At least this sync's scores; older recent plays from earlier syncs stay stored.
    assert scores is not None and scores >= second["scores"] == first["scores"]


def test_unknown_player_fails_the_task() -> None:
    task = osu_sync.delay("no-such-player-osu-coach-test")

    with pytest.raises(OsuNotFoundError):
        task.get(timeout=TIMEOUT_S)

    assert task.state == "FAILURE"


def test_fetch_beatmap_through_the_worker() -> None:
    result = fetch_beatmap.delay(22423).get(timeout=TIMEOUT_S)

    assert result == {"beatmap_id": 22423, "version": "Hard", "objects": 254}


def test_fetch_beatmap_refuses_other_modes() -> None:
    task = fetch_beatmap.delay(4139830)  # a taiko map

    with pytest.raises(UnsupportedModeError):
        task.get(timeout=TIMEOUT_S)

    assert task.state == "FAILURE"


def test_sync_fetches_the_players_missing_maps() -> None:
    # Remove map 22423's objects (peppy's top play) in the dev database, then sync peppy:
    # the sync must queue a fetch for it, and the worker must store its objects again.
    engine = create_engine(get_settings().database_url)
    count = text("SELECT count(*) FROM hit_objects WHERE beatmap_id = 22423")
    try:
        osu_sync.delay(2).get(timeout=TIMEOUT_S)  # make sure peppy and the map are stored
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM hit_objects WHERE beatmap_id = 22423"))

        result = osu_sync.delay(2).get(timeout=TIMEOUT_S)

        assert result["maps_queued"] >= 1
        deadline = time.monotonic() + TIMEOUT_S
        while time.monotonic() < deadline:
            with engine.connect() as conn:
                if conn.execute(count).scalar() == 254:
                    break
            time.sleep(1)
        with engine.connect() as conn:
            assert conn.execute(count).scalar() == 254, "the queued fetch never stored the map"

        again = osu_sync.delay(2).get(timeout=TIMEOUT_S)
        assert again["maps_queued"] == 0, "maps already stored aren't fetched again"
    finally:
        engine.dispose()


def test_fetch_replay_map_through_the_worker() -> None:
    # Map 22423 by its file's MD5, the way an uploaded replay names it.
    checksum = load("beatmap.json")["checksum"]

    result = fetch_replay_map.delay(checksum).get(timeout=TIMEOUT_S)
    again = fetch_replay_map.delay(checksum.upper()).get(timeout=TIMEOUT_S)

    assert result["beatmap_id"] == 22423
    assert again == {"beatmap_id": 22423, "fetched": False}  # stored now: not fetched again


def test_fetch_replay_map_for_an_unknown_file() -> None:
    task = fetch_replay_map.delay("0" * 32)  # no map has this file

    with pytest.raises(OsuNotFoundError):
        task.get(timeout=TIMEOUT_S)

    assert task.state == "FAILURE"


def test_analyze_replay_for_an_unknown_replay() -> None:
    task = analyze_replay.delay(2**62)  # no replay has this id

    with pytest.raises(ReplayNotFoundError):
        task.get(timeout=TIMEOUT_S)

    assert task.state == "FAILURE"
