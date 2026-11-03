"""Unit tests for osu_coach.worker: how the Celery tasks are declared.

Running the tasks needs the real stack; that's tests/integration/test_worker_live.py.
"""

from osu_coach.beatmaps import UnsupportedModeError
from osu_coach.osu.client import OsuNotFoundError
from osu_coach.worker import fetch_beatmap, osu_sync


def test_task_names() -> None:
    # The worker finds tasks by these names; renaming one breaks queued jobs.
    assert (osu_sync.name, fetch_beatmap.name) == ("osu_sync", "fetch_beatmap")


def test_expected_errors_are_declared() -> None:
    # Declared with throws=, these are logged as expected outcomes (one line), not as
    # crashes with a traceback. The task still fails and the caller still gets the error.
    assert osu_sync.throws == (OsuNotFoundError,)
    assert set(fetch_beatmap.throws) == {OsuNotFoundError, UnsupportedModeError}
