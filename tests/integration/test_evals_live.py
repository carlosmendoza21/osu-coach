"""Integration tests for osu_coach.evals.cases against a real Postgres with pgvector:
exporting a player as an eval case, and checking that case.

Run with:  uv run pytest -m integration
Needs:     Postgres with pgvector (docker compose up -d postgres). Skips with the reason
           if it's missing. Uses a separate `<db>_test` database (see conftest.py), with
           the hand-made players, plays and maps of test_recommender_live.py.
"""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from osu_coach.coach import gather_facts, template_report
from osu_coach.evals.cases import (
    export_case,
    load_facts,
    practice_snapshot,
    read_cases,
    write_case,
)
from osu_coach.evals.checks import check_case
from osu_coach.recommender import update_recommendations
from osu_coach.vectors import FIELDS
from tests.integration.test_recommender_live import STYLE_A, STYLE_B, World, vec
from tests.support import ALL_TABLES

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def world(engine: Engine) -> Iterator[World]:
    """Player 2 (p2, weakest: reading) played maps 1-4 (comfort 5.51); maps 101-112 are
    the pool, 5.7-6.15 stars. Player 3 isn't scored."""
    w = World(engine)
    w.player(2, weakest="reading")
    w.player(3)
    w.map(1, 5.0, vector=STYLE_A)
    w.map(2, 4.5, vector=STYLE_B)
    w.map(3, 7.0, vector=STYLE_B)
    w.play(1, 2, 1, attr_stars=5.0)
    w.play(2, 2, 2, "{DT}", attr_stars=6.0, key="DT")
    w.play(3, 2, 3, passed=False, attr_stars=9.0)
    for i in range(1, 13):
        w.map(100 + i, 5.7 + i * 0.035, ar=8 + i / 10, vector=vec(jump=0.2 + i / 100, stars=0.6))
    yield w
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {ALL_TABLES}"))


@pytest.fixture
async def db(test_url: URL) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(test_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(db: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(db, expire_on_commit=False)() as s:
        yield s


async def test_a_players_case(session: AsyncSession) -> None:
    case = await export_case(session, 2)

    assert case is not None
    assert case.name == "p2"  # World names players p<id>
    assert case.facts.username == "p2"
    assert case.facts.scores["reading"] == 50.0  # the hand-made profile
    assert len(case.facts.plays) == 2  # the passed osu! standard plays with attributes
    # The practice maps, picked for the weakest skill from the pool, with what checks need.
    p = case.practice
    assert (p.target_skill, p.comfort_stars, p.hardest_in_pool) == ("reading", 5.51, 7.0)
    assert len(p.maps) == 10
    assert [m.rank for m in p.maps] == list(range(1, 11))
    assert {m.beatmap_id for m in p.maps} <= set(range(101, 113))
    assert not any(m.played for m in p.maps)
    first = p.maps[0]
    assert set(first.vector) == set(FIELDS)
    assert first.vector["stars"] == pytest.approx(0.6)
    assert first.reason.startswith(f"AR {first.ar:g};")
    # The facts' maps are the same maps, in the same order.
    assert [m.beatmap_id for m in case.facts.maps] == [m.beatmap_id for m in p.maps]


async def test_export_picks_the_maps_again(
    world: World, engine: Engine, session: AsyncSession
) -> None:
    # No practice maps stored yet: export makes (and stores) them first.
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM recommendations")).scalar() == 0

    case = await export_case(session, 2)

    assert case is not None and len(case.practice.maps) == 10
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM recommendations")).scalar() == 10


async def test_a_played_map_is_marked(world: World, session: AsyncSession) -> None:
    # Edge: the player has played a map the recommender would pick (it shouldn't pick it).
    world.play(10, 2, 101)

    case = await export_case(session, 2)

    assert case is not None
    assert 101 not in {m.beatmap_id for m in case.practice.maps}


async def test_the_snapshot_marks_a_played_map(world: World, session: AsyncSession) -> None:
    # A map played after the maps were picked: the snapshot must say so (the check then
    # fails it). Taken without picking again, as export would.
    result = await update_recommendations(session, 2)
    assert result is not None
    played = result.picks[0].beatmap.id
    world.play(11, 2, played)

    practice = await practice_snapshot(session, 2)

    assert [m.beatmap_id for m in practice.maps if m.played] == [played]


async def test_no_case_for_an_unscored_or_unknown_player(session: AsyncSession) -> None:
    assert await export_case(session, 3) is None
    assert await export_case(session, 999) is None


async def test_the_case_round_trips_and_checks(tmp_path: Path, session: AsyncSession) -> None:
    case = await export_case(session, 2)
    assert case is not None
    write_case(case, tmp_path)

    [loaded] = read_cases(tmp_path)

    assert loaded == case
    # The facts load back as the coach gathers them, and the template report passes.
    facts = await gather_facts(session, 2)
    assert load_facts(loaded.facts) == facts
    result = check_case(loaded, template_report(load_facts(loaded.facts)))
    assert result.passed, [c for c in result.checks if not c.passed]
