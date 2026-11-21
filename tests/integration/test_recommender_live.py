"""Integration tests for osu_coach.recommender against a real Postgres with pgvector, and
GET /players/{osu_id}/recommendations over it: a player's comfort level and style, the
maps in range, recommending, storing, and the endpoint's answers.

Run with:  uv run pytest -m integration
Needs:     Postgres with pgvector (docker compose up -d postgres). Skips with the reason
           if it's missing. Uses a separate `<db>_test` database (see conftest.py).
"""

from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
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

from osu_coach.db import get_session
from osu_coach.main import create_app
from osu_coach.recommender import (
    COUNT,
    comfort_stars,
    hardest_map,
    maps_in_range,
    played_maps,
    player_style,
    recommend,
    update_recommendations,
)
from osu_coach.vectors import FIELDS
from tests.support import ALL_TABLES

pytestmark = pytest.mark.integration


def vec(**fields: float) -> list[float]:
    return [fields.get(f, 0.0) for f in FIELDS]


class World:
    """Writes players, maps, plays and profiles into the test database."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def sql(self, statement: str, params: Any = None) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(statement), params or {})

    def player(self, osu_id: int, weakest: str | None = None) -> None:
        self.sql(
            "INSERT INTO players (osu_id, username, country, pp, playcount, accuracy) "
            "VALUES (:id, :name, 'XX', 1, 1, 99)",
            {"id": osu_id, "name": f"p{osu_id}"},
        )
        if weakest:
            self.sql(
                "INSERT INTO skill_profiles (player_id, aim, speed, accuracy, consistency, "
                "stamina, reading, strongest, weakest, plays_used, plays_skipped) "
                "VALUES (:id, 50, 50, 50, 50, 50, 50, 'aim', :weakest, 2, 0)",
                {"id": osu_id, "weakest": weakest},
            )

    def map(
        self,
        map_id: int,
        stars: float,
        *,
        ar: float = 9,
        od: float = 8,
        length: int = 120,
        set_id: int | None = None,
        status: str = "ranked",
        mode: str = "osu",
        vector: list[float] | None = None,
    ) -> None:
        self.sql(
            "INSERT INTO beatmaps (id, beatmapset_id, artist, title, creator, version, "
            "star_rating, ar, od, cs, hp, bpm, length_s, hit_length_s, count_circles, "
            "count_sliders, count_spinners, status, mode) VALUES (:id, :set, 'artist', "
            ":title, 'mapper', 'Insane', :stars, :ar, :od, 4, 5, 180, :length, :length, 1, "
            "0, 0, :status, :mode)",
            {
                "id": map_id,
                "set": set_id or map_id,
                "title": f"map {map_id}",
                "stars": stars,
                "ar": ar,
                "od": od,
                "length": length,
                "status": status,
                "mode": mode,
            },
        )
        if vector is not None:
            self.sql(
                "INSERT INTO map_vectors (beatmap_id, vector, version) "
                "VALUES (:id, CAST(:v AS vector), 1)",
                {"id": map_id, "v": str(vector)},
            )

    def play(
        self,
        score_id: int,
        player: int,
        map_id: int,
        mods: str = "{}",
        passed: bool = True,
        attr_stars: float | None = None,
        key: str = "",
    ) -> None:
        self.sql(
            "INSERT INTO scores (id, player_id, beatmap_id, score, max_combo, count_300, "
            "count_100, count_50, count_miss, rank, pp, mods, date, accuracy, passed) VALUES "
            "(:id, :player, :map, 1, 100, 1, 0, 0, 0, 'S', 10, CAST(:mods AS varchar[]), now(), "
            "0.98, :passed)",
            {"id": score_id, "player": player, "map": map_id, "mods": mods, "passed": passed},
        )
        if attr_stars is not None:
            self.sql(
                "INSERT INTO beatmap_attributes (beatmap_id, mods, star_rating, max_combo, "
                "aim_difficulty, speed_difficulty, speed_note_count, slider_factor, "
                "aim_difficult_strain_count, speed_difficult_strain_count) VALUES (:map, :key, "
                ":stars, 100, 2, 2, 50, 1, 10, 10) ON CONFLICT DO NOTHING",
                {"map": map_id, "key": key, "stars": attr_stars},
            )


STYLE_A = vec(jump=0.2, stream=0.1, bpm=0.6, stars=0.5)
STYLE_B = vec(jump=0.4, stream=0.3, bpm=0.6, stars=0.6)


@pytest.fixture(autouse=True)
def world(engine: Engine) -> Iterator[World]:
    """Player 2 (weakest: reading) played map 1 nomod (5.0 stars) and map 2 with DT (6.0
    as played); a failed play and a taiko play don't count. Comfort: (6 + 5 x 0.95) /
    1.95 = 5.51, so reading's range is 5.71-6.21. Maps 101-112 are the pool in range."""
    w = World(engine)
    w.player(2, weakest="reading")
    w.map(1, 5.0, vector=STYLE_A)
    w.map(2, 4.5, vector=STYLE_B)
    w.map(3, 7.0, vector=STYLE_B)  # failed on
    w.map(4, 5.0, mode="taiko")
    w.play(1, 2, 1, attr_stars=5.0)
    w.play(2, 2, 2, "{DT}", attr_stars=6.0, key="DT")
    w.play(3, 2, 3, passed=False, attr_stars=9.0)
    w.play(4, 2, 4, attr_stars=9.0)
    # The pool: 12 maps from 5.75 to 6.15 stars, AR rising with the id.
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
    # Objects stay loaded after a commit, so a test can read the picks
    # update_recommendations returns (it commits before returning).
    async with async_sessionmaker(db, expire_on_commit=False)() as s:
        yield s


def stored(engine: Engine, osu_id: int = 2) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM recommendations WHERE player_id = :id ORDER BY rank"),
            {"id": osu_id},
        )
        return [dict(r._mapping) for r in rows]


# ---------- The player ----------


async def test_comfort_level(session: AsyncSession) -> None:
    # The DT play counts at its DT rating (6.0); the failed and taiko plays don't count.
    assert await comfort_stars(session, 2) == 5.51


async def test_a_plays_mods_are_matched_by_their_key(world: World, session: AsyncSession) -> None:
    # Player 9's one play is NC with HD: its attributes are stored under "DT,HD" (NC
    # plays as DT; keys are sorted), not the play's own "NC,HD".
    world.player(9)
    world.play(40, 9, 1, "{NC,HD}", attr_stars=6.5, key="DT,HD")

    assert await comfort_stars(session, 9) == 6.5


async def test_a_play_without_attributes_is_left_out(world: World, session: AsyncSession) -> None:
    world.map(5, 8.0, vector=STYLE_A)
    world.play(5, 2, 5)  # no attributes stored for it

    assert await comfort_stars(session, 2) == 5.51


async def test_no_comfort_level_without_plays(world: World, session: AsyncSession) -> None:
    world.player(3)

    assert await comfort_stars(session, 3) is None


async def test_the_maps_played_include_every_play(session: AsyncSession) -> None:
    # Failed and taiko plays too: a map they've failed isn't a new map to them.
    assert await played_maps(session, 2) == {1, 2, 3, 4}


async def test_style_is_the_average_of_the_played_maps_vectors(session: AsyncSession) -> None:
    style = await player_style(session, {1, 2, 4})  # 4 has no vector: left out

    assert style == pytest.approx([(a + b) / 2 for a, b in zip(STYLE_A, STYLE_B, strict=True)])


async def test_no_style_without_vectors(session: AsyncSession) -> None:
    assert await player_style(session, {4}) is None
    assert await player_style(session, set()) is None


# ---------- The maps in range ----------


async def test_maps_in_range_nearest_the_style_first(world: World, session: AsyncSession) -> None:
    picks = await maps_in_range(session, vec(jump=0.2, stars=0.6), 5.7, 6.2, {1, 2})

    ids = [p.beatmap.id for p in picks]
    assert ids == list(range(101, 113))  # the jump share drifts away from 0.2 with the id
    assert [p.distance for p in picks] == sorted(p.distance for p in picks)
    assert picks[0].vector["jump"] == pytest.approx(0.21)


async def test_what_maps_in_range_leaves_out(world: World, session: AsyncSession) -> None:
    world.map(201, 6.0, status="loved", vector=STYLE_A)
    world.map(202, 6.0, mode="taiko", vector=STYLE_A)
    world.map(203, 6.0)  # no vector
    world.map(204, 5.69, vector=STYLE_A)  # just below the range
    world.map(205, 6.2, vector=STYLE_A)  # edge: the top end is left out
    world.map(206, 5.7, vector=STYLE_A)  # edge: the bottom end is in

    picks = await maps_in_range(session, STYLE_A, 5.7, 6.2, played={101, 102})

    ids = {p.beatmap.id for p in picks}
    assert ids == {206} | set(range(103, 113))


async def test_the_hardest_map(world: World, session: AsyncSession) -> None:
    world.map(300, 9.9, status="loved", vector=STYLE_A)  # not a candidate
    world.map(301, 9.5)  # no vector: not a candidate

    assert await hardest_map(session) == 7.0  # map 3


# ---------- Recommending ----------


async def test_recommending_reading(session: AsyncSession) -> None:
    result = await recommend(session, 2)

    assert result is not None
    assert (result.skill, result.comfort, result.star_range) == ("reading", 5.51, (5.71, 6.21))
    # 12 in range; the 10 with the highest AR (103-112), nearest the style first.
    assert sorted(p.beatmap.id for p in result.picks) == list(range(103, 113))
    assert len(result.picks) == COUNT


async def test_no_recommendations_without_a_profile(world: World, session: AsyncSession) -> None:
    world.player(3)
    world.play(10, 3, 1, attr_stars=5.0)

    assert await recommend(session, 3) is None


async def test_no_recommendations_without_plays(world: World, session: AsyncSession) -> None:
    world.player(4, weakest="aim")

    assert await recommend(session, 4) is None


async def test_the_range_widens_until_enough_maps_fit(world: World, session: AsyncSession) -> None:
    # Weakest: stamina. Nothing in 5.71-6.21 has long streams; 10 maps at 7.0 do, so the
    # range widens twice (5.21-6.71, then 4.71-7.21) to reach them.
    world.sql("UPDATE skill_profiles SET weakest = 'stamina' WHERE player_id = 2")
    for i in range(1, 11):
        world.map(300 + i, 6.9 + i / 100, vector=vec(stream=0.5, long_stream=0.4 + i / 100))

    result = await recommend(session, 2)

    assert result is not None
    assert result.star_range == (4.71, 7.21)
    assert sorted(p.beatmap.id for p in result.picks) == list(range(301, 311))


async def test_a_level_above_every_map_gets_the_hardest(
    world: World, session: AsyncSession
) -> None:
    # Player 5 plays at 12 stars with DT: every map in the pool is below that. The range
    # is centred on the hardest map (10.0): 9.55 + 0.2 to 9.55 + 0.7.
    world.player(5, weakest="reading")
    world.map(400, 9.0, vector=STYLE_A)
    world.play(20, 5, 400, "{DT}", attr_stars=12.0, key="DT")
    for i in range(1, 11):
        world.map(400 + i, 9.76 + i * 0.024, vector=STYLE_A)  # 9.784-10.0; 10.0 the hardest

    result = await recommend(session, 5)

    assert result is not None
    assert result.comfort == 12.0  # their real level, for the reasons
    # No widening needed: all 10 fit in the first range, the hardest map (410) included.
    assert result.star_range == (9.75, 10.25)
    assert {p.beatmap.id for p in result.picks} == set(range(401, 411))


# ---------- Storing ----------


async def test_recommendations_are_stored_best_first(engine: Engine, session: AsyncSession) -> None:
    result = await update_recommendations(session, 2)

    rows = stored(engine)  # committed: read on another connection
    assert result is not None
    assert [r["rank"] for r in rows] == list(range(1, COUNT + 1))
    assert [r["beatmap_id"] for r in rows] == [p.beatmap.id for p in result.picks]
    assert {r["target_skill"] for r in rows} == {"reading"}
    assert {r["comfort_stars"] for r in rows} == {5.51}
    assert all(0 < r["score"] <= 1 for r in rows)
    assert [r["score"] for r in rows] == sorted((r["score"] for r in rows), reverse=True)
    first = next(p for p in result.picks if p.beatmap.id == rows[0]["beatmap_id"])
    stars = first.beatmap.star_rating
    level = f"{stars - 5.51:.1f}★ above your comfort level (5.5★)"
    assert rows[0]["reason"] == f"AR {first.beatmap.ar:g}; {stars:.1f}★, {level}"


async def test_making_them_again_replaces_them(
    world: World, engine: Engine, session: AsyncSession
) -> None:
    await update_recommendations(session, 2)
    world.sql("UPDATE skill_profiles SET weakest = 'accuracy' WHERE player_id = 2")

    await update_recommendations(session, 2)

    rows = stored(engine)
    assert len(rows) <= COUNT
    assert {r["target_skill"] for r in rows} == {"accuracy"}


async def test_no_longer_recommendable_clears_them(
    world: World, engine: Engine, session: AsyncSession
) -> None:
    await update_recommendations(session, 2)
    world.sql("DELETE FROM skill_profiles WHERE player_id = 2")

    assert await update_recommendations(session, 2) is None
    assert stored(engine) == []


async def test_another_players_are_left_alone(
    world: World, engine: Engine, session: AsyncSession
) -> None:
    world.player(6, weakest="reading")
    world.play(30, 6, 1, attr_stars=5.0)
    await update_recommendations(session, 6)

    await update_recommendations(session, 2)

    assert len(stored(engine, 6)) == COUNT


async def test_deleting_the_player_or_a_map_deletes_them(
    world: World, engine: Engine, session: AsyncSession
) -> None:
    await update_recommendations(session, 2)
    gone = stored(engine)[0]["beatmap_id"]

    world.sql("DELETE FROM beatmaps WHERE id = :id", {"id": gone})
    assert gone not in [r["beatmap_id"] for r in stored(engine)]
    assert len(stored(engine)) == COUNT - 1

    world.sql("DELETE FROM scores WHERE player_id = 2")
    world.sql("DELETE FROM players WHERE osu_id = 2")
    assert stored(engine) == []


# ---------- GET /players/{osu_id}/recommendations ----------


async def get(db: AsyncEngine, path: str) -> httpx.Response:
    app = create_app()

    async def session() -> AsyncIterator[AsyncSession]:
        async with async_sessionmaker(db)() as s:
            yield s

    app.dependency_overrides[get_session] = session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


async def test_the_endpoint(world: World, db: AsyncEngine, session: AsyncSession) -> None:
    result = await update_recommendations(session, 2)
    assert result is not None
    # Reverse the stored ranks, so rank order is neither the picks' nor the map ids'.
    world.sql("UPDATE recommendations SET rank = 11 - rank WHERE player_id = 2")
    by_rank = [p.beatmap.id for p in reversed(result.picks)]

    resp = await get(db, "/players/2/recommendations")

    assert resp.status_code == 200
    body = resp.json()
    assert result is not None
    assert (body["player_id"], body["target_skill"], body["comfort_stars"]) == (2, "reading", 5.51)
    assert body["created_at"] is not None
    assert [m["rank"] for m in body["maps"]] == list(range(1, COUNT + 1))
    assert [m["beatmap"]["id"] for m in body["maps"]] == by_rank  # rank 1 first
    first = body["maps"][0]
    assert set(first) == {"rank", "score", "reason", "beatmap"}
    assert set(first["beatmap"]) == {
        "id",
        "beatmapset_id",
        "artist",
        "title",
        "version",
        "creator",
        "star_rating",
        "bpm",
        "ar",
        "od",
        "hit_length_s",
    }
    assert first["reason"].startswith("AR ")


@pytest.mark.parametrize(
    ("setup", "path", "status", "detail"),
    [
        (None, "/players/999/recommendations", 404, "player 999 not found"),
        ("unscored", "/players/7/recommendations", 409, "recommendations aren't ready yet"),
        (None, "/players/abc/recommendations", 422, None),
    ],
    ids=["unknown-player", "not-scored-yet", "not-an-id"],
)
async def test_the_endpoints_errors(
    world: World, db: AsyncEngine, setup: str | None, path: str, status: int, detail: str | None
) -> None:
    if setup == "unscored":
        world.player(7)

    resp = await get(db, path)

    assert resp.status_code == status
    if detail:
        assert resp.json()["detail"] == detail


async def test_scored_but_none_made_yet(world: World, db: AsyncEngine) -> None:
    # Edge: a player scored before recommendations existed, until their next sync.
    world.player(8, weakest="aim")

    resp = await get(db, "/players/8/recommendations")

    assert resp.status_code == 200
    assert resp.json() == {
        "player_id": 8,
        "target_skill": None,
        "comfort_stars": None,
        "created_at": None,
        "maps": [],
    }
