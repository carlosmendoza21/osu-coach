"""Integration tests: migrations applied to a database that already holds data.

Run with:  uv run pytest -m integration
Needs:     Postgres (docker compose up -d postgres). Skips with the reason if it isn't
           reachable.
The other database tests start from an empty schema. These start from the revision
before a migration, put realistic data in, and then upgrade, the way a deployed database
is migrated. Each test gets its own `<db>_migration_test` database, dropped afterwards.
"""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import OperationalError

from osu_coach.config import get_settings
from tests.support import alembic

pytestmark = pytest.mark.integration

BEFORE_BEATMAPS = "117b390a05b1"  # players and scores, scores.beatmap_id with no foreign key
BEATMAPS = "82b58b64c0eb"


# ---------- Preconditions and fixtures ----------


@pytest.fixture
def url() -> Iterator[URL]:
    dev = make_url(get_settings().database_url)
    url = dev.set(database=f"{dev.database}_migration_test")
    admin = create_engine(dev.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    except OperationalError:
        admin.dispose()
        pytest.skip(
            f"Postgres not reachable at {dev.host}:{dev.port}. "
            "Start it: docker compose up -d postgres"
        )
    yield url
    with admin.connect() as conn:  # runs even if the test failed
        conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture
def engine(url: URL) -> Iterator[Engine]:
    eng = create_engine(url)
    yield eng
    eng.dispose()


def migrate(url: URL, *args: str) -> None:
    result = alembic(url, *args)
    assert result.returncode == 0, result.stdout + result.stderr


def scalar(engine: Engine, sql: str) -> object:
    with engine.connect() as conn:
        return conn.execute(text(sql)).scalar()


def fill_pre_beatmap_data(engine: Engine) -> None:
    """A synced player with scores on maps that aren't stored: the state before beatmaps."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO players (osu_id, username, country, pp, playcount, accuracy) "
                "VALUES (2, 'peppy', 'AU', 1184.2, 7816, 96.85)"
            )
        )
        for score_id, beatmap_id in [(1, 22423), (2, 258467), (3, 22423)]:
            conn.execute(
                text(
                    "INSERT INTO scores (id, player_id, beatmap_id, score, max_combo, "
                    "count_300, count_100, count_50, count_miss, rank, pp, mods, date, "
                    "accuracy, passed) VALUES (:id, 2, :map, 1, 1, 1, 0, 0, 0, 'X', 10, "
                    "'{}', now(), 1.0, true)"
                ),
                {"id": score_id, "map": beatmap_id},
            )


# ---------- The beatmaps migration on existing data ----------


def test_upgrade_succeeds_with_scores_whose_maps_arent_stored(url: URL, engine: Engine) -> None:
    migrate(url, "upgrade", BEFORE_BEATMAPS)
    fill_pre_beatmap_data(engine)

    # Without the data step, adding the foreign key would fail on these scores.
    migrate(url, "upgrade", BEATMAPS)

    assert scalar(engine, "SELECT version_num FROM alembic_version") == BEATMAPS
    assert scalar(engine, "SELECT count(*) FROM scores") == 0, "orphaned scores removed"
    assert scalar(engine, "SELECT count(*) FROM players") == 1, "players are kept"
    fks = {fk["name"] for fk in inspect(engine).get_foreign_keys("scores")}
    assert "scores_beatmap_id_fkey" in fks


def test_upgrade_marks_players_stale(url: URL, engine: Engine) -> None:
    migrate(url, "upgrade", BEFORE_BEATMAPS)
    fill_pre_beatmap_data(engine)

    migrate(url, "upgrade", BEATMAPS)

    # Stale players re-sync on their next request instead of showing no scores for up
    # to 10 minutes.
    stale = scalar(engine, "SELECT updated_at = 'epoch' FROM players WHERE osu_id = 2")
    assert stale is True


def test_downgrade_keeps_players_and_drops_the_new_table(url: URL, engine: Engine) -> None:
    migrate(url, "upgrade", BEFORE_BEATMAPS)
    fill_pre_beatmap_data(engine)
    migrate(url, "upgrade", BEATMAPS)

    migrate(url, "downgrade", BEFORE_BEATMAPS)

    insp = inspect(engine)
    assert "beatmaps" not in insp.get_table_names()
    assert {fk["name"] for fk in insp.get_foreign_keys("scores")} == {"scores_player_id_fkey"}
    assert scalar(engine, "SELECT count(*) FROM players") == 1
    # The removed scores don't come back on downgrade; a re-sync restores them.
    assert scalar(engine, "SELECT count(*) FROM scores") == 0


def test_upgrade_again_after_downgrade(url: URL, engine: Engine) -> None:
    migrate(url, "upgrade", "head")
    migrate(url, "downgrade", BEFORE_BEATMAPS)

    migrate(url, "upgrade", "head")

    assert "beatmaps" in inspect(engine).get_table_names()


def test_upgrade_on_an_empty_database(url: URL, engine: Engine) -> None:
    # A fresh deployment: the data step has nothing to remove and must not fail.
    migrate(url, "upgrade", "head")

    assert scalar(engine, "SELECT count(*) FROM scores") == 0
    assert "beatmaps" in inspect(engine).get_table_names()
