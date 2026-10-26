"""Unit tests for osu_coach.sync.

No database and no osu!: FakeOsu (tests/support.py) serves the saved fixtures, and
FakeSession records the statements it's given, which are compiled to Postgres SQL to check
the upserts. The same code against a real Postgres is in tests/integration/test_sync_live.py.
"""

from typing import Any, cast

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach.models import Base
from osu_coach.osu.client import OsuApiError, OsuClient, OsuNotFoundError
from osu_coach.osu.schemas import OsuScore, OsuUser
from osu_coach.sync import (
    SyncResult,
    player_values,
    score_values,
    sync_player,
    upsert_player,
    upsert_scores,
)
from tests.support import FakeOsu, best_scores, failed_score, load, user

PLAYER_COLUMNS = {c.name for c in Base.metadata.tables["players"].columns}
SCORE_COLUMNS = {c.name for c in Base.metadata.tables["scores"].columns}
PG = postgresql.dialect()  # type: ignore[no-untyped-call]  # SQLAlchemy leaves it untyped


class FakeSession:
    """Records executed statements (as Postgres SQL) and commits."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> None:
        self.statements.append(statement)
        sql = str(statement.compile(dialect=PG))
        self.events.append(f"execute {sql.split()[2]}")  # e.g. "execute players"

    async def commit(self) -> None:
        self.events.append("commit")

    def sql(self, index: int) -> str:
        return str(self.statements[index].compile(dialect=PG))

    def row_count(self, index: int) -> int:
        params = self.statements[index].compile(dialect=PG).params
        return sum(1 for key in params if key.startswith("id_m")) or 1


async def run_sync(osu: FakeOsu, session: FakeSession, who: int | str = 2) -> SyncResult:
    return await sync_player(cast(OsuClient, osu), cast(AsyncSession, session), who)


# ---------- Mapping ----------


def test_player_values() -> None:
    raw = load("user.json")

    values = player_values(user())

    assert values == {
        "osu_id": 2,
        "username": "peppy",
        "country": "AU",
        "global_rank": raw["statistics"]["global_rank"],
        "pp": raw["statistics"]["pp"],
        "playcount": raw["statistics"]["play_count"],
        "accuracy": raw["statistics"]["hit_accuracy"],  # a percent
    }


def test_player_values_cover_every_column_but_updated_at() -> None:
    # updated_at is the database's job; every other column must come from osu!.
    assert set(player_values(user())) == PLAYER_COLUMNS - {"updated_at"}


@pytest.mark.parametrize("index", [0, 1, 2])
def test_score_values(index: int) -> None:
    raw = load("scores_best.json")[index]
    score = OsuScore.model_validate(raw)

    values = score_values(score)

    assert values == {
        "id": raw["id"],
        "player_id": raw["user_id"],
        "beatmap_id": raw["beatmap"]["id"],
        "score": raw["score"],
        "max_combo": raw["max_combo"],
        "count_300": raw["statistics"]["count_300"],
        "count_100": raw["statistics"]["count_100"],
        "count_50": raw["statistics"]["count_50"],
        "count_miss": raw["statistics"]["count_miss"],
        "rank": raw["rank"],
        "pp": raw["pp"],
        "mods": raw["mods"],
        "date": score.created_at,  # the play time, parsed
        "accuracy": raw["accuracy"],
        "passed": raw["passed"],
    }


def test_score_values_cover_every_column() -> None:
    assert set(score_values(best_scores()[0])) == SCORE_COLUMNS


def test_failed_play_values() -> None:
    values = score_values(failed_score())

    assert (values["pp"], values["passed"], values["rank"]) == (None, False, "F")


# ---------- Upsert statements ----------


async def test_upsert_player_sql() -> None:
    session = FakeSession()

    await upsert_player(cast(AsyncSession, session), user())

    sql = session.sql(0)
    assert sql.startswith("INSERT INTO players")
    assert "ON CONFLICT (osu_id) DO UPDATE SET" in sql
    updated = sql.split("DO UPDATE SET", 1)[1]
    for column in PLAYER_COLUMNS - {"osu_id", "updated_at"}:
        assert f"{column} = excluded.{column}" in updated, column
    # Set by hand: the model's onupdate doesn't fire for INSERT ... ON CONFLICT.
    assert "updated_at = now()" in updated
    assert "osu_id =" not in updated, "the key itself is never updated"


async def test_upsert_scores_sql() -> None:
    session = FakeSession()

    await upsert_scores(cast(AsyncSession, session), best_scores())

    sql = session.sql(0)
    assert sql.startswith("INSERT INTO scores")
    assert "ON CONFLICT (id) DO UPDATE SET" in sql
    updated = sql.split("DO UPDATE SET", 1)[1]
    for column in SCORE_COLUMNS - {"id"}:
        assert f"{column} = excluded.{column}" in updated, column
    assert " id =" not in updated


async def test_upsert_scores_is_one_statement() -> None:
    session = FakeSession()
    scores = [*best_scores(), failed_score()]

    saved = await upsert_scores(cast(AsyncSession, session), scores)

    assert saved == 3
    assert len(session.statements) == 1, "all rows in one INSERT, not one per score"
    assert session.row_count(0) == 3


@pytest.mark.parametrize(
    ("scores", "want"),
    [
        (lambda: [*best_scores(), *best_scores()], 2),  # in both best and recent
        (lambda: [best_scores()[0]] * 5, 1),
    ],
)
async def test_duplicate_scores_are_saved_once(scores: Any, want: int) -> None:
    # Postgres refuses to update the same row twice in one INSERT ... ON CONFLICT.
    session = FakeSession()

    saved = await upsert_scores(cast(AsyncSession, session), scores())

    assert saved == want
    assert session.row_count(0) == want


async def test_no_scores_means_no_statement() -> None:
    session = FakeSession()

    saved = await upsert_scores(cast(AsyncSession, session), [])

    assert saved == 0
    assert session.statements == []


# ---------- sync_player ----------


async def test_sync_order_player_scores_commit() -> None:
    session = FakeSession()

    result = await run_sync(FakeOsu(), session)

    # The player first (scores reference it), then the scores, then one commit.
    assert session.events == ["execute players", "execute scores", "commit"]
    assert result == SyncResult(osu_id=2, username="peppy", scores=2)


async def test_sync_merges_best_and_recent() -> None:
    best = best_scores()
    session = FakeSession()

    result = await run_sync(FakeOsu(best=best, recent=[best[0], failed_score()]), session)

    assert result.scores == 3, "best[0] is in both lists and counts once"


async def test_sync_by_username_fetches_scores_by_id() -> None:
    osu = FakeOsu()

    await run_sync(osu, FakeSession(), "peppy")

    assert osu.calls == [
        ("get_user", "peppy", 0),
        ("get_best_scores", 2, 50),  # by the resolved id, not the name
        ("get_recent_scores", 2, 50),
    ]


async def test_sync_passes_limits() -> None:
    osu = FakeOsu()

    await sync_player(
        cast(OsuClient, osu), cast(AsyncSession, FakeSession()), 2, best_limit=100, recent_limit=10
    )

    assert [c[2] for c in osu.calls[1:]] == [100, 10]


async def test_sync_with_no_scores_still_saves_the_player() -> None:
    session = FakeSession()

    result = await run_sync(FakeOsu(best=[], recent=[]), session)

    assert session.events == ["execute players", "commit"]
    assert result.scores == 0


@pytest.mark.parametrize("failing", ["get_user", "get_best_scores", "get_recent_scores"])
async def test_osu_failure_writes_nothing(failing: str) -> None:
    session = FakeSession()

    with pytest.raises(OsuApiError):
        await run_sync(FakeOsu(fail=failing), session)

    # Everything is fetched before anything is written.
    assert session.events == []


async def test_unknown_player_writes_nothing() -> None:
    class Missing(FakeOsu):
        async def get_user(self, who: int | str) -> OsuUser:
            raise OsuNotFoundError(f"/users/{who}/osu")

    session = FakeSession()

    with pytest.raises(OsuNotFoundError):
        await run_sync(Missing(), session, "nobody")

    assert session.events == []
