"""Fixtures for the integration tests that need a real Postgres."""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import OperationalError

from osu_coach.config import get_settings
from tests.support import alembic


@pytest.fixture(scope="module")
def test_url() -> Iterator[URL]:
    """A fresh `<db>_test` database, migrated to head, dropped afterwards.

    Each test module gets its own, so the dev database is never touched.
    """
    dev = make_url(get_settings().database_url)
    url = dev.set(database=f"{dev.database}_test")
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

    result = alembic(url, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    yield url

    with admin.connect() as conn:  # runs even if tests failed
        conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture(scope="module")
def engine(test_url: URL) -> Iterator[Engine]:
    eng = create_engine(test_url)
    yield eng
    eng.dispose()
