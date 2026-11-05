"""Unit tests for osu_coach.worker: how the Celery tasks are declared.

Running the tasks needs the real stack; that's tests/integration/test_worker_live.py.
"""

from osu_coach.analysis import MapNotStoredError, ReplayNotFoundError
from osu_coach.beatmaps import UnsupportedModeError
from osu_coach.difficulty import UnsupportedModsError
from osu_coach.osu.client import OsuNotFoundError
from osu_coach.replays import MapChangedError
from osu_coach.worker import analyze_replay, fetch_beatmap, fetch_replay_map, osu_sync


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
