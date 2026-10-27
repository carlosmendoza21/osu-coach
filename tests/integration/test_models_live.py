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

from osu_coach.models import Beatmap, Player, Score
from osu_coach.osu.schemas import OsuScore, OsuUser
from osu_coach.sync import beatmap_values, player_values, score_values
from tests.support import ROOT, alembic, load

pytestmark = pytest.mark.integration

HEAD = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as s:
        yield s
        s.rollback()
    with engine.begin() as conn:  # each test starts from empty tables
        conn.execute(text("TRUNCATE players, scores, beatmaps"))


# The rows come from the same mapping the sync uses, so these tests cover it too.
def player_row(user: OsuUser) -> Player:
    return Player(**player_values(user))


def score_row(score: OsuScore) -> Score:
    return Score(**score_values(score))


def map_row(score: OsuScore) -> Beatmap:
    return Beatmap(**beatmap_values(score.beatmap, score.beatmapset))


def add_score(session: Session, score: OsuScore, player: Player | None = None) -> None:
    """Store a score with the rows it references: its map and (unless given) its player."""
    session.merge(map_row(score))
    session.merge(player if player is not None else a_player(score.user_id))
    session.flush()  # referenced rows first
    session.add(score_row(score))


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
    assert not {"players", "scores", "beatmaps"} & set(inspect(engine).get_table_names())

    assert alembic(test_url, "upgrade", "head").returncode == 0
    assert {"players", "scores", "beatmaps"} <= set(inspect(engine).get_table_names())


@pytest.mark.parametrize(
    ("table", "nullable"),
    [
        ("players", {"global_rank"}),
        ("scores", {"pp"}),
        ("beatmaps", {"max_combo", "checksum"}),
    ],
)
def test_nullable_columns_in_postgres(engine: Engine, table: str, nullable: set[str]) -> None:
    columns = inspect(engine).get_columns(table)

    assert {c["name"] for c in columns if c["nullable"]} == nullable


def test_foreign_keys_and_indexes_in_postgres(engine: Engine) -> None:
    insp = inspect(engine)
    fks = {fk["name"]: fk for fk in insp.get_foreign_keys("scores")}
    indexes = {i["name"]: i["column_names"] for i in insp.get_indexes("scores")}

    assert set(fks) == {"scores_player_id_fkey", "scores_beatmap_id_fkey"}
    player_fk = fks["scores_player_id_fkey"]
    assert player_fk["referred_table"] == "players"
    assert player_fk["options"].get("ondelete") == "CASCADE"
    map_fk = fks["scores_beatmap_id_fkey"]
    assert map_fk["referred_table"] == "beatmaps"
    assert map_fk["options"].get("ondelete") is None, "maps with scores can't be deleted"
    assert indexes == {
        "ix_scores_beatmap_id": ["beatmap_id"],
        "ix_scores_player_pp": ["player_id", "pp"],
    }


def test_checksum_is_unique_in_postgres(engine: Engine) -> None:
    (unique,) = inspect(engine).get_unique_constraints("beatmaps")

    assert (unique["name"], unique["column_names"]) == ("beatmaps_checksum_key", ["checksum"])


def test_constraint_names_match_the_convention(engine: Engine) -> None:
    # The names the models give (the naming convention) are the ones Postgres has,
    # including for constraints created before the convention existed.
    with engine.connect() as conn:
        names = set(
            conn.execute(
                text(
                    "SELECT conname FROM pg_constraint WHERE conrelid IN "
                    "('players'::regclass, 'scores'::regclass, 'beatmaps'::regclass)"
                )
            ).scalars()
        )

    assert names == {
        "players_pkey",
        "scores_pkey",
        "scores_player_id_fkey",
        "scores_beatmap_id_fkey",
        "beatmaps_pkey",
        "beatmaps_checksum_key",
    }


# ---------- Real osu! data ----------


def test_real_user_and_scores_round_trip(session: Session) -> None:
    user = OsuUser.model_validate(load("user.json"))
    scores = [OsuScore.model_validate(s) for s in load("scores_best.json")]
    peppy = player_row(user)
    for score in scores[:2]:  # peppy's scores (the third is another player's)
        add_score(session, score, peppy)
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

    add_score(session, failed)
    session.commit()

    stored = session.get(Score, failed.id)
    assert stored is not None
    assert (stored.pp, stored.passed, stored.rank) == (None, False, "F")


def test_several_mods_and_big_ids(session: Session) -> None:
    modded = OsuScore.model_validate(load("scores_best.json")[2])

    add_score(session, modded.model_copy(update={"id": 5_000_000_000}))  # past 32 bits
    session.commit()

    stored = session.get(Score, 5_000_000_000)
    assert stored is not None
    assert stored.mods == ["HD", "DT"]


# ---------- Constraints ----------


def test_score_needs_an_existing_player(session: Session) -> None:
    score = OsuScore.model_validate(load("scores_best.json")[0])
    session.add(map_row(score))  # the map exists, the player doesn't
    session.flush()
    session.add(score_row(score))

    with pytest.raises(IntegrityError, match="scores_player_id_fkey"):
        session.commit()


def test_score_needs_an_existing_map(session: Session) -> None:
    score = OsuScore.model_validate(load("scores_best.json")[0])
    session.add(a_player(score.user_id))  # the player exists, the map doesn't
    session.flush()
    session.add(score_row(score))

    with pytest.raises(IntegrityError, match="scores_beatmap_id_fkey"):
        session.commit()


def test_a_map_with_scores_cant_be_deleted(session: Session, engine: Engine) -> None:
    score = OsuScore.model_validate(load("scores_best.json")[0])
    add_score(session, score)
    session.commit()

    # No CASCADE on maps: deleting one that people have scores on is refused.
    with pytest.raises(IntegrityError, match="scores_beatmap_id_fkey"), engine.begin() as conn:
        conn.execute(text("DELETE FROM beatmaps WHERE id = :id"), {"id": score.beatmap.id})


def test_two_maps_cant_share_a_checksum(session: Session) -> None:
    first, second = (OsuScore.model_validate(s) for s in load("scores_best.json")[:2])
    session.add(map_row(first))
    clash = map_row(second)
    clash.checksum = first.beatmap.checksum
    session.add(clash)

    with pytest.raises(IntegrityError, match="beatmaps_checksum_key"):
        session.commit()


def test_any_number_of_maps_without_a_checksum(session: Session) -> None:
    rows = [map_row(OsuScore.model_validate(s)) for s in load("scores_best.json")]
    for row in rows:
        row.checksum = None
    session.add_all(rows)

    session.commit()  # Postgres allows many NULLs in a unique column

    assert session.execute(text("SELECT count(*) FROM beatmaps")).scalar_one() == 3


def test_deleting_a_player_deletes_their_scores(session: Session, engine: Engine) -> None:
    score = OsuScore.model_validate(load("scores_best.json")[0])
    add_score(session, score)
    session.commit()

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM players WHERE osu_id = :id"), {"id": score.user_id})
        remaining = conn.execute(text("SELECT count(*) FROM scores")).scalar_one()

    assert remaining == 0


def test_play_date_is_required(session: Session) -> None:
    score = OsuScore.model_validate(load("scores_best.json")[0])
    session.add_all([map_row(score), a_player(score.user_id)])
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
