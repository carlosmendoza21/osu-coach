"""Helpers shared by the test suites."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from sqlalchemy.engine import URL

from osu_coach.osu.client import OsuApiError
from osu_coach.osu.schemas import OsuScore, OsuUser

ROOT = Path(__file__).parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "osu"


def load(name: str) -> Any:
    """A saved real osu! API response from tests/fixtures/osu/."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def alembic(url: URL, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the alembic CLI against `url`, the way deployment does."""
    env = {**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


# ---------- A fake osu! for sync tests ----------


def user() -> OsuUser:
    return OsuUser.model_validate(load("user.json"))


def best_scores() -> list[OsuScore]:
    return [OsuScore.model_validate(s) for s in load("scores_best.json")[:2]]  # peppy's


def failed_score() -> OsuScore:
    return OsuScore.model_validate(load("score_recent_failed.json"))


class FakeOsu:
    """Serves fixed data and records calls. `fail` makes one method raise."""

    def __init__(
        self,
        best: list[OsuScore] | None = None,
        recent: list[OsuScore] | None = None,
        fail: str | None = None,
    ) -> None:
        self.best = best_scores() if best is None else best
        self.recent = [] if recent is None else recent
        self.fail = fail
        self.calls: list[tuple[str, Any, int]] = []

    def _maybe_fail(self, name: str) -> None:
        if self.fail == name:
            raise OsuApiError(f"{name} failed")

    async def get_user(self, who: int | str) -> OsuUser:
        self.calls.append(("get_user", who, 0))
        self._maybe_fail("get_user")
        return user()

    async def get_best_scores(self, user_id: int, limit: int = 50) -> list[OsuScore]:
        self.calls.append(("get_best_scores", user_id, limit))
        self._maybe_fail("get_best_scores")
        return self.best

    async def get_recent_scores(self, user_id: int, limit: int = 50) -> list[OsuScore]:
        self.calls.append(("get_recent_scores", user_id, limit))
        self._maybe_fail("get_recent_scores")
        return self.recent
