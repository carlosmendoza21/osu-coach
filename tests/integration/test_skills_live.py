"""Integration tests for osu_coach.skills against a real Postgres: loading a player's
plays, and storing their profile.

Run with:  uv run pytest -m integration
Needs:     Postgres (docker compose up -d postgres). Skips with the reason if it's missing.
Uses a separate `<db>_test` database (see conftest.py), with plays and attributes written
in by hand.
"""

from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from osu_coach.skills import (
    SKILLS,
    Play,
    SkillProfile,
    load_plays,
    skill_profile,
    store_profile,
    update_profile,
)
from tests.support import ALL_TABLES

pytestmark = pytest.mark.integration


@pytest.fixture
async def db(test_url: URL) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(test_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture(autouse=True)
def rows(engine: Engine) -> Iterator[None]:
    """Player 2 and another; maps 1 and 2 (AR 9, OD 8) and 3 (taiko); plays; attributes."""
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
            [{"id": 1, "mode": "osu"}, {"id": 2, "mode": "osu"}, {"id": 3, "mode": "taiko"}],
        )
        conn.execute(
            text(
                "INSERT INTO beatmap_attributes (beatmap_id, mods, star_rating, max_combo, "
                "aim_difficulty, speed_difficulty, speed_note_count, slider_factor, "
                "aim_difficult_strain_count, speed_difficult_strain_count) "
                "VALUES (:map, :mods, 5, :combo, :aim, :speed, :notes, 1, 10, 10)"
            ),
            [
                {"map": 1, "mods": "", "combo": 400, "aim": 2.0, "speed": 1.5, "notes": 100},
                {"map": 1, "mods": "DT", "combo": 400, "aim": 2.8, "speed": 2.2, "notes": 110},
                {"map": 2, "mods": "RX", "combo": 300, "aim": 1.0, "speed": 0.1, "notes": 0},
                {"map": 2, "mods": "", "combo": 300, "aim": 1.5, "speed": 1.2, "notes": 80},
            ],
        )
        plays = [
            # id, player, map, mods, passed, accuracy, misses, combo
            (1, 2, 1, "{}", True, 0.98, 1, 380),  # used
            (2, 2, 1, "{NC}", True, 0.95, 0, 400),  # used: NC is stored under DT
            (3, 2, 1, "{HD}", True, 0.99, 0, 400),  # skipped: no HD attributes yet
            (4, 2, 2, "{RX}", True, 1.0, 0, 300),  # skipped: Relax isn't the player's aim
            (5, 2, 2, "{}", False, 0.8, 9, 50),  # left out: failed
            (6, 2, 3, "{}", True, 0.99, 0, 500),  # left out: taiko
            (7, 3, 2, "{}", True, 0.99, 0, 300),  # another player's
        ]
        conn.execute(
            text(
                "INSERT INTO scores (id, player_id, beatmap_id, score, max_combo, count_300, "
                "count_100, count_50, count_miss, rank, pp, mods, date, accuracy, passed) "
                "VALUES (:id, :player, :map, 1, :combo, 1, 0, 0, :miss, 'S', 10, "
                "CAST(:mods AS varchar[]), now(), :acc, :passed)"
            ),
            [
                {"id": i, "player": p, "map": m, "mods": mods, "passed": ok, "acc": acc,
                 "miss": miss, "combo": combo}
                for i, p, m, mods, ok, acc, miss, combo in plays
            ],
        )  # fmt: skip
    yield
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {ALL_TABLES}"))


async def load(db: AsyncEngine, osu_id: int = 2) -> tuple[list[Play], int]:
    async with async_sessionmaker(db)() as session:
        return await load_plays(session, osu_id)


async def test_which_plays_are_used(db: AsyncEngine) -> None:
    plays, skipped = await load(db)

    # The nomod and NC plays of map 1; the HD play (no attributes) and the RX play are
    # skipped; the failed play, the taiko play and the other player's aren't counted.
    assert sorted(p.mods for p in plays) == [(), ("NC",)]
    assert skipped == 2


async def test_a_play_carries_its_maps_attributes_as_played(db: AsyncEngine) -> None:
    plays, _ = await load(db)
    nomod = next(p for p in plays if p.mods == ())
    nc = next(p for p in plays if p.mods == ("NC",))

    assert (nomod.accuracy, nomod.count_miss, nomod.max_combo) == (0.98, 1, 380)
    assert (nomod.map_max_combo, nomod.aim_difficulty, nomod.speed_note_count) == (400, 2.0, 100)
    assert (nomod.ar, nomod.od) == pytest.approx((9.0, 8.0))
    # NC: the DT attributes, and the AR and OD the player saw at 1.5x speed.
    assert (nc.aim_difficulty, nc.speed_difficulty) == (2.8, 2.2)
    assert nc.ar == pytest.approx(10.33, abs=0.01)
    assert nc.od == pytest.approx(9.78, abs=0.01)


async def test_another_player(db: AsyncEngine) -> None:
    plays, skipped = await load(db, 3)

    assert [(p.aim_difficulty, p.mods) for p in plays] == [(1.5, ())]
    assert skipped == 0


async def test_a_player_without_plays(db: AsyncEngine) -> None:
    assert await load(db, 999) == ([], 0)


async def test_a_profile_from_the_database(db: AsyncEngine) -> None:
    plays, skipped = await load(db)

    profile = skill_profile(plays, skipped)

    assert (profile.plays_used, profile.plays_skipped) == (2, 2)
    assert set(profile.scores) == set(SKILLS)
    assert all(0 < s <= 100 for s in profile.scores.values())


# ---------- Storing a profile ----------


def a_profile(aim: float = 50.0, used: int = 10) -> SkillProfile:
    scores = {"aim": aim, "speed": 40.0, "accuracy": 70.0, "consistency": 60.0,
              "stamina": 30.0, "reading": 20.0}  # fmt: skip
    return SkillProfile(scores, plays_used=used, plays_skipped=1)


def stored(engine: Engine) -> list[dict[str, object]]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT * FROM skill_profiles ORDER BY player_id"))
        return [dict(r._mapping) for r in rows]


async def store(db: AsyncEngine, osu_id: int, profile: SkillProfile) -> None:
    async with async_sessionmaker(db)() as session:
        await store_profile(session, osu_id, profile)
        await session.commit()


async def test_a_profile_is_stored(db: AsyncEngine, engine: Engine) -> None:
    await store(db, 2, a_profile())

    [row] = stored(engine)
    computed_at = row.pop("computed_at")
    assert row == {
        "player_id": 2, "aim": 50.0, "speed": 40.0, "accuracy": 70.0, "consistency": 60.0,
        "stamina": 30.0, "reading": 20.0, "strongest": "accuracy", "weakest": "reading",
        "plays_used": 10, "plays_skipped": 1,
    }  # fmt: skip
    assert computed_at is not None  # set by the database


async def test_storing_again_replaces_it(db: AsyncEngine, engine: Engine) -> None:
    await store(db, 2, a_profile())
    [first] = stored(engine)

    await store(db, 2, a_profile(aim=90.0, used=12))

    [second] = stored(engine)  # still one row
    assert (second["aim"], second["strongest"], second["plays_used"]) == (90.0, "aim", 12)
    # A new time: when the profile was scored, not first stored.
    assert second["computed_at"] > first["computed_at"]  # type: ignore[operator]


async def test_players_have_their_own_profiles(db: AsyncEngine, engine: Engine) -> None:
    await store(db, 2, a_profile(aim=10.0))
    await store(db, 3, a_profile(aim=90.0))

    assert [(r["player_id"], r["aim"]) for r in stored(engine)] == [(2, 10.0), (3, 90.0)]


async def test_a_profile_needs_its_player(db: AsyncEngine) -> None:
    with pytest.raises(IntegrityError):
        await store(db, 999, a_profile())


async def test_deleting_a_player_deletes_their_profile(db: AsyncEngine, engine: Engine) -> None:
    await store(db, 2, a_profile())
    await store(db, 3, a_profile())

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM scores WHERE player_id = 2"))
        conn.execute(text("DELETE FROM players WHERE osu_id = 2"))

    assert [r["player_id"] for r in stored(engine)] == [3]


# ---------- Scoring and storing a player ----------


async def update(db: AsyncEngine, osu_id: int) -> SkillProfile | None:
    async with async_sessionmaker(db)() as session:
        return await update_profile(session, osu_id)


async def test_a_player_is_scored_from_their_plays(db: AsyncEngine, engine: Engine) -> None:
    profile = await update(db, 2)

    # The same as scoring the plays loaded, and stored (committed: read on another connection).
    assert profile is not None
    assert profile == skill_profile(*await load(db))
    [row] = stored(engine)
    assert {s: row[s] for s in SKILLS} == profile.scores
    assert (row["plays_used"], row["plays_skipped"]) == (2, 2)
    assert (row["strongest"], row["weakest"]) == (profile.strongest, profile.weakest)


async def test_scoring_again_follows_new_plays(db: AsyncEngine, engine: Engine) -> None:
    await update(db, 2)
    with engine.begin() as conn:  # the HD play's attributes arrive
        conn.execute(
            text(
                "INSERT INTO beatmap_attributes (beatmap_id, mods, star_rating, max_combo, "
                "aim_difficulty, speed_difficulty, speed_note_count, slider_factor, "
                "aim_difficult_strain_count, speed_difficult_strain_count) "
                "VALUES (1, 'HD', 5, 400, 2.1, 1.5, 100, 1, 10, 10)"
            )
        )

    profile = await update(db, 2)

    assert profile is not None
    [row] = stored(engine)
    # The HD play now counts: 3 used, only the Relax play skipped.
    assert (profile.plays_used, profile.plays_skipped) == (3, 1)
    assert (row["plays_used"], row["plays_skipped"]) == (3, 1)


async def test_a_player_without_plays_gets_zeros(db: AsyncEngine, engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO players (osu_id, username, country, pp, playcount, accuracy) "
                "VALUES (4, 'new', 'XX', 0, 0, 0)"
            )
        )

    profile = await update(db, 4)

    assert profile is not None
    assert set(profile.scores.values()) == {0.0}
    assert stored(engine)[0]["plays_used"] == 0


async def test_an_unknown_player_is_not_scored(db: AsyncEngine, engine: Engine) -> None:
    assert await update(db, 999) is None
    assert stored(engine) == []
