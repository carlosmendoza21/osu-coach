"""Integration tests for osu_coach.attributes against a real Postgres.

Run with:  uv run pytest -m integration
Needs:     Postgres (docker compose up -d postgres). Skips with the reason if it's missing.
Uses a separate `<db>_test` database (see conftest.py) and a fake osu! that answers with
the saved real attributes of map 22423, so only the database is real.
"""

from collections.abc import AsyncIterator, Iterator
from typing import cast

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from osu_coach.attributes import (
    AttributesResult,
    attributes_to_fetch,
    fetch_player_attributes,
    store_attributes,
)
from osu_coach.osu.client import OsuApiError, OsuClient, OsuNotFoundError
from osu_coach.osu.schemas import AttributesResponse, DifficultyAttributes
from tests.support import ALL_TABLES, load

pytestmark = pytest.mark.integration

NOMOD = AttributesResponse.model_validate(load("attributes_22423.json")).attributes
DT = AttributesResponse.model_validate(load("attributes_22423_dt.json")).attributes


@pytest.fixture
async def db(test_url: URL) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(test_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture(autouse=True)
def rows(engine: Engine) -> Iterator[None]:
    """Two players; maps 1, 2 and 4 (osu! standard) and 3 (taiko); their plays."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO players (osu_id, username, country, pp, playcount, accuracy) "
                "VALUES (2, 'peppy', 'AU', 1, 1, 99), (3, 'other', 'XX', 1, 1, 99)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO beatmaps (id, beatmapset_id, artist, title, creator, version, "
                "star_rating, ar, od, cs, hp, bpm, length_s, hit_length_s, count_circles, "
                "count_sliders, count_spinners, status, mode) VALUES (:id, 1, 'a', 't', 'c', "
                "'v', 5, 9, 8, 4, 5, 180, 90, 80, 1, 0, 0, 'ranked', :mode)"
            ),
            [{"id": 1, "mode": "osu"}, {"id": 2, "mode": "osu"}, {"id": 3, "mode": "taiko"},
             {"id": 4, "mode": "osu"}],
        )  # fmt: skip
        plays = [
            (1, 2, 1, "{}"),  # map 1, nomod
            (2, 2, 1, "{HD}"),  # map 1 with HD: another key
            (3, 2, 1, "{NC}"),  # map 1 with NC...
            (4, 2, 1, "{DT}"),  # ...and DT: one key between them
            (5, 2, 2, "{PF}"),  # map 2 with PF: nomod's key
            (6, 2, 3, "{}"),  # taiko: not analyzed
            (7, 3, 4, "{}"),  # another player's
        ]
        conn.execute(
            text(
                "INSERT INTO scores (id, player_id, beatmap_id, score, max_combo, count_300, "
                "count_100, count_50, count_miss, rank, pp, mods, date, accuracy, passed) "
                "VALUES (:id, :player, :map, 1, 1, 1, 0, 0, 0, 'S', 10, CAST(:mods AS varchar[]), "
                "now(), 1, true)"
            ),
            [{"id": i, "player": p, "map": m, "mods": mods} for i, p, m, mods in plays],
        )
    yield
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {ALL_TABLES}"))


async def to_fetch(db: AsyncEngine, osu_id: int = 2) -> list[tuple[int, str]]:
    async with async_sessionmaker(db)() as session:
        return await attributes_to_fetch(session, osu_id)


async def store(db: AsyncEngine, beatmap_id: int, key: str, attrs: DifficultyAttributes) -> None:
    async with async_sessionmaker(db)() as session:
        await store_attributes(session, beatmap_id, key, attrs)
        await session.commit()


def stored(engine: Engine) -> list[tuple[object, ...]]:
    with engine.connect() as conn:
        sql = "SELECT beatmap_id, mods, star_rating FROM beatmap_attributes ORDER BY 1, 2"
        return [tuple(r) for r in conn.execute(text(sql))]


class FakeOsu:
    """Answers attributes requests: DT's for a DT key, nomod's otherwise; or an error."""

    def __init__(self, errors: dict[int, Exception] | None = None) -> None:
        self.errors = errors or {}
        self.calls: list[tuple[int, list[str]]] = []

    async def get_beatmap_attributes(
        self, beatmap_id: int, mods: list[str]
    ) -> DifficultyAttributes:
        self.calls.append((beatmap_id, mods))
        if beatmap_id in self.errors:
            raise self.errors[beatmap_id]
        return DT if "DT" in mods else NOMOD


async def fetch(db: AsyncEngine, osu: FakeOsu, osu_id: int = 2) -> AttributesResult:
    async with async_sessionmaker(db)() as session:
        return await fetch_player_attributes(cast(OsuClient, osu), session, osu_id)


# ---------- Which pairs ----------


async def test_the_pairs_a_player_needs(db: AsyncEngine) -> None:
    # Map 1 under three keys (NC and DT share one), map 2 as nomod (PF dropped); not the
    # taiko map, not the other player's map.
    assert await to_fetch(db) == [(1, ""), (1, "DT"), (1, "HD"), (2, "")]


async def test_another_players_pairs(db: AsyncEngine) -> None:
    assert await to_fetch(db, 3) == [(4, "")]
    assert await to_fetch(db, 999) == []  # no plays at all


async def test_stored_pairs_drop_off(db: AsyncEngine) -> None:
    await store(db, 1, "DT", DT)
    await store(db, 2, "", NOMOD)

    assert await to_fetch(db) == [(1, ""), (1, "HD")]


async def test_a_pair_stored_for_another_player_counts(db: AsyncEngine) -> None:
    # Attributes belong to a map and mods, not a player: shared by everyone who played it.
    await store(db, 4, "", NOMOD)

    assert await to_fetch(db, 3) == []


# ---------- Fetching ----------


async def test_fetching_stores_every_pair(db: AsyncEngine, engine: Engine) -> None:
    osu = FakeOsu()

    assert await fetch(db, osu) == AttributesResult(fetched=4, not_found=0)
    # Each pair asked for with its normalized mods.
    assert osu.calls == [(1, []), (1, ["DT"]), (1, ["HD"]), (2, [])]
    assert stored(engine) == [
        (1, "", pytest.approx(3.5647, rel=1e-3)),
        (1, "DT", pytest.approx(4.982, rel=1e-3)),
        (1, "HD", pytest.approx(3.5647, rel=1e-3)),
        (2, "", pytest.approx(3.5647, rel=1e-3)),
    ]


async def test_fetching_again_asks_for_nothing(db: AsyncEngine) -> None:
    await fetch(db, FakeOsu())
    osu = FakeOsu()

    assert await fetch(db, osu) == AttributesResult(fetched=0, not_found=0)
    assert osu.calls == []


async def test_a_map_osu_no_longer_has_is_skipped(db: AsyncEngine, engine: Engine) -> None:
    result = await fetch(db, FakeOsu({2: OsuNotFoundError("gone")}))

    assert result == AttributesResult(fetched=3, not_found=1)
    assert [r[:2] for r in stored(engine)] == [(1, ""), (1, "DT"), (1, "HD")]


async def test_a_failure_part_way_keeps_what_was_fetched(db: AsyncEngine, engine: Engine) -> None:
    # osu! fails on map 2, the last pair: the three before it are already committed.
    with pytest.raises(OsuApiError):
        await fetch(db, FakeOsu({2: OsuApiError("osu! is down")}))
    assert len(stored(engine)) == 3

    # The next run only asks for what's missing.
    osu = FakeOsu()
    assert await fetch(db, osu) == AttributesResult(fetched=1, not_found=0)
    assert osu.calls == [(2, [])]


# ---------- The table ----------


async def test_storing_again_replaces_the_values(db: AsyncEngine, engine: Engine) -> None:
    await store(db, 1, "", NOMOD)
    await store(db, 1, "", DT)  # osu! recalculated the map

    assert stored(engine) == [(1, "", pytest.approx(4.982, rel=1e-3))]


async def test_deleting_a_map_deletes_its_attributes(db: AsyncEngine, engine: Engine) -> None:
    await store(db, 2, "", NOMOD)

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM scores WHERE beatmap_id = 2"))
        conn.execute(text("DELETE FROM beatmaps WHERE id = 2"))
    assert stored(engine) == []


async def test_attributes_need_their_map(db: AsyncEngine) -> None:
    with pytest.raises(IntegrityError, match="beatmap_attributes_beatmap_id_fkey"):
        await store(db, 999, "", NOMOD)


async def test_the_database_sets_the_fetch_time(db: AsyncEngine, engine: Engine) -> None:
    await store(db, 1, "", NOMOD)

    with engine.connect() as conn:
        age = conn.execute(text("SELECT now() - fetched_at FROM beatmap_attributes")).scalar_one()
    assert age.total_seconds() < 60
