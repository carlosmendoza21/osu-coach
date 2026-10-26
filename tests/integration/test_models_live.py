"""Integration tests for the tables and migration, against a real Postgres.

Run with:  uv run pytest -m integration
Needs:     Postgres (docker compose up -d postgres). Skips with the reason if it isn't
           reachable. Redis and osu! credentials aren't needed.
Uses a separate `<db>_test` database, migrated with Alembic exactly as deployment does
(see conftest.py), so the dev database is never touched.
"""

from collections.abc import Iterator

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from osu_coach.models import Player, Score
from osu_coach.osu.schemas import OsuScore, OsuUser
from osu_coach.sync import player_values, score_values
from tests.support import ROOT, alembic, load

pytestmark = pytest.mark.integration

HEAD = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as s:
        yield s
        s.rollback()
    with engine.begin() as conn:  # each test starts from empty tables
        conn.execute(text("TRUNCATE players, scores"))


# The rows come from the same mapping the sync uses, so these tests cover it too.
def player_row(user: OsuUser) -> Player:
    return Player(**player_values(user))


def score_row(score: OsuScore) -> Score:
    return Score(**score_values(score))


def a_player(osu_id: int = 999) -> Player:
    return Player(osu_id=osu_id, username="test", country="XX", pp=1.0, playcount=1, accuracy=99.0)


# ---------- Migration ----------


def test_migration_reaches_head(engine: Engine) -> None:
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

    assert version == HEAD


def test_models_match_the_migrated_database(test_url: URL) -> None:
    result = alembic(test_url, "check")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "No new upgrade operations detected" in result.stdout + result.stderr


def test_migration_can_be_undone_and_redone(test_url: URL, engine: Engine) -> None:
    assert alembic(test_url, "downgrade", "base").returncode == 0
    assert not {"players", "scores"} & set(inspect(engine).get_table_names())

    assert alembic(test_url, "upgrade", "head").returncode == 0
    assert {"players", "scores"} <= set(inspect(engine).get_table_names())


@pytest.mark.parametrize(
    ("table", "nullable"),
    [("players", {"global_rank"}), ("scores", {"pp"})],
)
def test_nullable_columns_in_postgres(engine: Engine, table: str, nullable: set[str]) -> None:
    columns = inspect(engine).get_columns(table)

    assert {c["name"] for c in columns if c["nullable"]} == nullable


def test_foreign_key_and_indexes_in_postgres(engine: Engine) -> None:
    insp = inspect(engine)
    (fk,) = insp.get_foreign_keys("scores")
    indexes = {i["name"]: i["column_names"] for i in insp.get_indexes("scores")}

    assert (fk["constrained_columns"], fk["referred_table"]) == (["player_id"], "players")
    assert fk["options"].get("ondelete") == "CASCADE"
    assert indexes == {
        "ix_scores_beatmap_id": ["beatmap_id"],
        "ix_scores_player_pp": ["player_id", "pp"],
    }


# ---------- Real osu! data ----------


def test_real_user_and_scores_round_trip(session: Session) -> None:
    user = OsuUser.model_validate(load("user.json"))
    scores = [OsuScore.model_validate(s) for s in load("scores_best.json")]
    session.add(player_row(user))
    session.flush()  # the player must exist before scores that reference it
    for score in scores[:2]:  # peppy's scores (the third is another player's)
        session.add(score_row(score))
    session.commit()
    session.expire_all()

    player = session.get(Player, user.id)
    assert player is not None
    assert (player.username, player.global_rank, player.pp) == (
        "peppy",
        user.statistics.global_rank,
        user.statistics.pp,
    )
    assert player.updated_at is not None, "set by Postgres"
    stored = session.get(Score, scores[0].id)
    assert stored is not None
    assert stored.mods == scores[0].mods
    assert stored.count_300 == scores[0].statistics.count_300
    assert stored.date == scores[0].created_at, "time and timezone survive the round trip"


def test_failed_play_without_pp_is_stored(session: Session) -> None:
    failed = OsuScore.model_validate(load("score_recent_failed.json"))
    session.add(a_player(failed.user_id))
    session.flush()

    session.add(score_row(failed))
    session.commit()

    stored = session.get(Score, failed.id)
    assert stored is not None
    assert (stored.pp, stored.passed, stored.rank) == (None, False, "F")


def test_several_mods_and_big_ids(session: Session) -> None:
    modded = OsuScore.model_validate(load("scores_best.json")[2])
    session.add(a_player(modded.user_id))
    session.flush()
    row = score_row(modded)
    row.id = 5_000_000_000  # past the 32-bit integer limit

    session.add(row)
    session.commit()

    stored = session.get(Score, 5_000_000_000)
    assert stored is not None
    assert stored.mods == ["HD", "DT"]


# ---------- Constraints ----------


def test_score_needs_an_existing_player(session: Session) -> None:
    score = OsuScore.model_validate(load("scores_best.json")[0])
    session.add(score_row(score))

    with pytest.raises(IntegrityError, match="foreign key"):
        session.commit()


def test_deleting_a_player_deletes_their_scores(session: Session, engine: Engine) -> None:
    score = OsuScore.model_validate(load("scores_best.json")[0])
    session.add(a_player(score.user_id))
    session.flush()
    session.add(score_row(score))
    session.commit()

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM players WHERE osu_id = :id"), {"id": score.user_id})
        remaining = conn.execute(text("SELECT count(*) FROM scores")).scalar_one()

    assert remaining == 0


def test_play_date_is_required(session: Session) -> None:
    score = OsuScore.model_validate(load("scores_best.json")[0])
    session.add(a_player(score.user_id))
    session.flush()
    row = score_row(score)
    row.date = None  # type: ignore[assignment]
    session.add(row)

    # No default: a missing play time fails instead of becoming the sync time.
    with pytest.raises(IntegrityError, match="date"):
        session.commit()


def test_best_scores_query_uses_the_index(session: Session) -> None:
    session.add(a_player(2))
    session.commit()

    # Tiny tables are cheaper to scan, so turn off the other plans to see the index scan
    # Postgres uses once the table is big.
    session.execute(text("SET LOCAL enable_seqscan = off"))
    session.execute(text("SET LOCAL enable_bitmapscan = off"))
    plan = "\n".join(
        session.execute(
            text("EXPLAIN SELECT * FROM scores WHERE player_id = 2 ORDER BY pp DESC LIMIT 50")
        ).scalars()
    )

    assert "ix_scores_player_pp" in plan, plan
    assert "Sort" not in plan, f"the index should already give pp order:\n{plan}"
