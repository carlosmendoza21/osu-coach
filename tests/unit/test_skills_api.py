"""Unit tests for GET /players/{osu_id}/skills (osu_coach.api.skills).

No database: the session is a fake that answers `get` from the rows it was given. The
endpoint against the real stack is in tests/integration/test_api_live.py.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest

from osu_coach.db import get_session
from osu_coach.main import create_app
from osu_coach.models import Player, SkillProfile

COMPUTED_AT = datetime(2026, 11, 18, 5, 23, 6, tzinfo=UTC)


def profile_row(player_id: int = 2) -> SkillProfile:
    return SkillProfile(
        player_id=player_id,
        aim=41.1,
        speed=32.9,
        accuracy=69.4,
        consistency=80.9,
        stamina=38.3,
        reading=8.0,
        strongest="consistency",
        weakest="reading",
        plays_used=50,
        plays_skipped=3,
        computed_at=COMPUTED_AT,
    )


def player_row(osu_id: int = 2) -> Player:
    return Player(osu_id=osu_id, username="peppy", country="AU", pp=1, playcount=1, accuracy=99)


class FakeSession:
    """Answers get(Model, key) from the rows given; records what was asked."""

    def __init__(self, *rows: Player | SkillProfile) -> None:
        self.rows = {(type(r), r.osu_id if isinstance(r, Player) else r.player_id): r for r in rows}
        self.gets: list[tuple[type, int]] = []

    async def get(self, model: type, key: int) -> Player | SkillProfile | None:
        self.gets.append((model, key))
        return self.rows.get((model, key))


async def get(session: FakeSession, path: str) -> httpx.Response:
    app = create_app()

    async def fake() -> AsyncIterator[FakeSession]:
        yield session

    app.dependency_overrides[get_session] = fake
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


# ---------- Which answer ----------

CASES = [
    # (what's stored, path, status, the detail's start (None: a profile))
    pytest.param([profile_row(), player_row()], "/players/2/skills", 200, None, id="scored"),
    # Only the profile: it's enough (it can't exist without the player anyway).
    pytest.param([profile_row()], "/players/2/skills", 200, None, id="profile-only"),
    pytest.param([player_row()], "/players/2/skills", 409, "skill profile isn't ready",
                 id="not-scored-yet"),
    pytest.param([], "/players/2/skills", 404, "player 2 not found", id="unknown-player"),
    # Another player's profile isn't this one's.
    pytest.param([profile_row(3), player_row(3)], "/players/2/skills", 404, "player 2 not found",
                 id="someone-elses-profile"),
    pytest.param([player_row(3)], "/players/3/skills", 409, "skill profile isn't ready",
                 id="other-id-not-scored"),
    # Edge: ids are osu! ids, so numbers only.
    pytest.param([], "/players/peppy/skills", 422, None, id="a-username"),
    pytest.param([], "/players/2.5/skills", 422, None, id="not-a-whole-number"),
    pytest.param([], "/players//skills", 404, None, id="no-id"),
]  # fmt: skip


@pytest.mark.parametrize(("rows", "path", "status", "detail"), CASES)
async def test_answers(rows: list[Player | SkillProfile], path: str, status: int,
                       detail: str | None) -> None:  # fmt: skip
    resp = await get(FakeSession(*rows), path)

    assert resp.status_code == status
    if detail is not None:
        assert resp.json()["detail"].startswith(detail)


# ---------- The profile sent ----------


async def test_the_profile_sent() -> None:
    resp = await get(FakeSession(profile_row(), player_row()), "/players/2/skills")

    assert resp.json() == {
        "player_id": 2,
        "aim": 41.1,
        "speed": 32.9,
        "accuracy": 69.4,
        "consistency": 80.9,
        "stamina": 38.3,
        "reading": 8.0,
        "strongest": "consistency",
        "weakest": "reading",
        "plays_used": 50,
        "plays_skipped": 3,
        "computed_at": "2026-11-18T05:23:06Z",
    }


async def test_a_scored_player_needs_one_lookup() -> None:
    session = FakeSession(profile_row(), player_row())

    await get(session, "/players/2/skills")

    assert session.gets == [(SkillProfile, 2)]


async def test_an_unscored_player_is_looked_up() -> None:
    session = FakeSession(player_row())

    await get(session, "/players/2/skills")

    assert session.gets == [(SkillProfile, 2), (Player, 2)]


async def test_the_endpoint_is_documented() -> None:
    resp = await get(FakeSession(), "/openapi.json")

    op = resp.json()["paths"]["/players/{osu_id}/skills"]["get"]
    schema = op["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["$ref"].endswith("/SkillsOut")
