"""Integration tests: osu_sync through the real Celery worker.

Run with:  uv run pytest -m integration
Needs:     the full stack (docker compose up -d), with osu! credentials in .env. Skips with
           the reason if no worker answers.
Note: the worker writes to the dev database, like the real app. Syncing is idempotent,
so running this again changes nothing but the synced player's latest values.
"""

import pytest
from sqlalchemy import create_engine, text

from osu_coach.config import get_settings
from osu_coach.osu.client import OsuNotFoundError
from osu_coach.worker import celery_app, osu_sync

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
