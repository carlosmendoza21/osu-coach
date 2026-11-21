"""Unit tests for osu_coach.worker: how the Celery tasks are declared.

Running the tasks needs the real stack; that's tests/integration/test_worker_live.py.
"""

from typing import Any

import pytest

from osu_coach.analysis import MapNotStoredError, ReplayNotFoundError
from osu_coach.beatmaps import UnsupportedModeError
from osu_coach.difficulty import UnsupportedModsError
from osu_coach.osu.client import OsuNotFoundError
from osu_coach.replays import MapChangedError
from osu_coach.worker import (
    SYNC_QUEUE,
    analyze_replay,
    build_map_vectors,
    celery_app,
    collect_candidates,
    fetch_attributes,
    fetch_beatmap,
    fetch_replay_map,
    osu_sync,
)


def test_task_names() -> None:
    # The worker finds tasks by these names; renaming one breaks queued jobs.
    names = (osu_sync.name, fetch_beatmap.name, fetch_replay_map.name, analyze_replay.name)
    assert names == ("osu_sync", "fetch_beatmap", "fetch_replay_map", "analyze_replay")


def test_expected_errors_are_declared() -> None:
    # Declared with throws=, these are logged as expected outcomes (one line), not as
    # crashes with a traceback. The task still fails and the caller still gets the error.
    assert osu_sync.throws == (OsuNotFoundError,)
    assert set(fetch_beatmap.throws) == {OsuNotFoundError, UnsupportedModeError}
    # A map osu! no longer has in the version that was played is expected too.
    assert set(fetch_replay_map.throws) == {
        OsuNotFoundError,
        UnsupportedModeError,
        MapChangedError,
    }
    # A replay or map that isn't there, or mods the judging doesn't model.
    assert set(analyze_replay.throws) == {
        ReplayNotFoundError,
        MapNotStoredError,
        UnsupportedModsError,
    }


def test_newer_task_names() -> None:
    names = (fetch_attributes.name, build_map_vectors.name, collect_candidates.name)
    assert names == ("fetch_attributes", "build_map_vectors", "collect_candidates")


ALL_TASKS = [osu_sync, fetch_beatmap, fetch_replay_map, analyze_replay, fetch_attributes,
             build_map_vectors, collect_candidates]  # fmt: skip


def queue_of(task_name: str) -> str | None:
    """The queue a task is sent to: its route's, or None for Celery's default queue."""
    route = celery_app.amqp.router.route({}, task_name)
    queue = route.get("queue")
    return None if queue is None or queue.name == celery_app.conf.task_default_queue else queue.name


def test_syncs_have_their_own_queue() -> None:
    # A search waits on its sync: it must never queue behind background work.
    assert SYNC_QUEUE == "sync"
    assert queue_of("osu_sync") == "sync"


@pytest.mark.parametrize("task", [t for t in ALL_TASKS if t is not osu_sync], ids=lambda t: t.name)
def test_everything_else_goes_to_the_default_queue(task: Any) -> None:
    # Edge: a map download routed to "sync" would bring back the very wait this avoids.
    assert queue_of(task.name) is None
