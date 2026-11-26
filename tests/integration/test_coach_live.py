"""Integration tests for osu_coach.coach against a real Postgres: gathering a player's
facts (profile, plays, replays matched by name, practice maps), and POST
/players/{osu_id}/report over them.

Run with:  uv run pytest -m integration
Needs:     Postgres (docker compose up -d postgres). Skips with the reason if it's missing.
           Uses a separate `<db>_test` database (see conftest.py).
"""

import json
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

from osu_coach.api.coach import get_llm
from osu_coach.coach import gather_facts, replay_facts, template_report
from osu_coach.db import get_session
from osu_coach.llm import LLMClient, LLMError, Message, Reply, Tool
from osu_coach.main import create_app
from tests.support import ALL_TABLES

pytestmark = pytest.mark.integration

SCORES = {
    "aim": 90.0,
    "speed": 40.0,
    "accuracy": 75.0,
    "consistency": 30.0,
    "stamina": 55.0,
    "reading": 60.0,
}
PATTERNS = ["jump", "jump", "stream", "slider"]  # map 1's objects


def sql(engine: Engine, statement: str, params: Any = None) -> None:
    with engine.begin() as conn:
        conn.execute(text(statement), params or {})


def add_map(engine: Engine, map_id: int, title: str = "Title") -> None:
    sql(
        engine,
        "INSERT INTO beatmaps (id, beatmapset_id, artist, title, creator, version, star_rating, "
        "ar, od, cs, hp, bpm, length_s, hit_length_s, count_circles, count_sliders, "
        "count_spinners, status, mode) VALUES (:id, :id, 'Artist', :title, 'Mapper', 'Insane', "
        "5.123, 9, 8, 4, 5, 180, 90, 80, 3, 1, 0, 'ranked', 'osu')",
        {"id": map_id, "title": title},
    )


def add_replay(
    engine: Engine,
    replay_id: int,
    name: str,
    ur: float | None,
    results: list[str] | None,
    analyzed: bool = True,
) -> None:
    sql(
        engine,
        "INSERT INTO replays (id, file_sha256, beatmap_checksum, player_name, mods, lazer, "
        "game_version, count_300, count_100, count_50, count_geki, count_katu, count_miss, "
        "score, max_combo, perfect, played_at, frame_count, frames_path, beatmap_id, "
        "unstable_rate, analyzed_at) OVERRIDING SYSTEM VALUE VALUES (:id, :sha, 'c', :name, "
        "'{}', true, 1, 0, 0, 0, 0, 0, 0, 0, 0, false, now(), 1, 'f', 1, :ur, "
        "CASE WHEN :analyzed THEN now() END)",
        {"id": replay_id, "sha": f"{replay_id:064x}", "name": name, "ur": ur, "analyzed": analyzed},
    )
    for idx, result in enumerate(results or []):
        sql(
            engine,
            "INSERT INTO hit_results (replay_id, idx, result, forced) VALUES (:r, :i, :res, false)",
            {"r": replay_id, "i": idx, "res": result},
        )


@pytest.fixture(autouse=True)
def world(engine: Engine) -> Iterator[None]:
    """Player 2 (peppy) is scored, with one play on map 1 and practice maps 10 and 11;
    player 3 isn't scored. Replays: 1 and 2 are peppy's (in any letter case) and
    analyzed; 3 is peppy's but not analyzed; 4 is someone else's."""
    for osu_id, name in ((2, "peppy"), (3, "other")):
        sql(
            engine,
            "INSERT INTO players (osu_id, username, country, pp, playcount, accuracy) "
            "VALUES (:id, :name, 'XX', 1, 1, 99)",
            {"id": osu_id, "name": name},
        )
    sql(
        engine,
        "INSERT INTO skill_profiles (player_id, aim, speed, accuracy, consistency, stamina, "
        "reading, strongest, weakest, plays_used, plays_skipped) VALUES (2, :aim, :speed, "
        ":accuracy, :consistency, :stamina, :reading, 'aim', 'consistency', 1, 0)",
        SCORES,
    )
    add_map(engine, 1)
    for idx, pattern in enumerate(PATTERNS):
        sql(
            engine,
            "INSERT INTO hit_objects (beatmap_id, idx, kind, time_ms, end_time_ms, x, y, "
            "pattern) VALUES (1, :i, 'circle', :t, :t, 0, 0, :p)",
            {"i": idx, "t": idx * 100, "p": pattern},
        )
    sql(
        engine,
        "INSERT INTO scores (id, player_id, beatmap_id, score, max_combo, count_300, "
        "count_100, count_50, count_miss, rank, pp, mods, date, accuracy, passed) "
        "VALUES (1, 2, 1, 1, 400, 1, 0, 0, 1, 'A', 10, '{}', now(), 0.97, true)",
    )
    sql(
        engine,
        "INSERT INTO beatmap_attributes (beatmap_id, mods, star_rating, max_combo, "
        "aim_difficulty, speed_difficulty, speed_note_count, slider_factor, "
        "aim_difficult_strain_count, speed_difficult_strain_count) "
        "VALUES (1, '', 5, 500, 2.5, 2.0, 120, 1, 10, 10)",
    )
    for map_id, rank in ((10, 2), (11, 1)):
        add_map(engine, map_id, title=f"Practice {map_id}")
        sql(
            engine,
            "INSERT INTO recommendations (player_id, beatmap_id, rank, target_skill, "
            "score, reason, comfort_stars) VALUES (2, :m, :r, 'consistency', 0.8, "
            ":reason, 5.0)",
            {"m": map_id, "r": rank, "reason": f"reason {map_id}"},
        )
    add_replay(engine, 1, "PEPPY", 80.0, ["miss", "300", "300", "miss"])  # a jump, the slider
    add_replay(engine, 2, "peppy", 100.0, ["300", "300", "300", "300"])
    add_replay(engine, 3, "peppy", None, None, analyzed=False)
    add_replay(engine, 4, "someone", 50.0, ["miss", "miss", "miss", "miss"])
    yield
    sql(engine, f"TRUNCATE {ALL_TABLES}")


@pytest.fixture
async def db(test_url: URL) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(test_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(db: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(db)() as s:
        yield s


# ---------- Replays ----------


async def test_replays_matched_by_name(session: AsyncSession) -> None:
    replays = await replay_facts(session, "peppy")

    # Replays 1 and 2: any letter case; not 3 (not analyzed) or 4 (someone else's).
    assert replays is not None
    assert replays.count == 2
    assert replays.mean_ur == 90.0
    # Each replay judged 2 jumps, 1 stream and 1 slider; replay 1 missed a jump and the slider.
    assert replays.notes == {"jump": 4, "stream": 2, "slider": 2}
    assert replays.miss_rates == {"jump": 0.25, "stream": 0.0, "slider": 0.5}


async def test_no_replays(session: AsyncSession) -> None:
    assert await replay_facts(session, "other") is None


async def test_a_replay_without_ur(engine: Engine, session: AsyncSession) -> None:
    # Edge: analyzed but with no timing to measure (all misses, say): no UR to average.
    add_replay(engine, 5, "other", None, ["miss"])

    replays = await replay_facts(session, "other")

    assert replays is not None
    assert (replays.count, replays.mean_ur) == (1, None)


# ---------- All the facts ----------


async def test_a_players_facts(session: AsyncSession) -> None:
    facts = await gather_facts(session, 2)

    assert facts is not None
    assert (facts.player_id, facts.username) == (2, "peppy")
    assert facts.scores == SCORES
    assert len(facts.plays) == 1
    assert (facts.plays[0].aim_difficulty, facts.plays[0].count_miss) == (2.5, 1)
    assert facts.replays is not None and facts.replays.count == 2
    # The practice maps in rank order (11 is rank 1), named and with their reasons.
    assert [(m.beatmap_id, m.name, m.star_rating, m.reason) for m in facts.maps] == [
        (11, "Artist - Practice 11 [Insane]", 5.12, "reason 11"),
        (10, "Artist - Practice 10 [Insane]", 5.12, "reason 10"),
    ]


@pytest.mark.parametrize("osu_id", [3, 999], ids=["not-scored", "unknown"])
async def test_no_facts(session: AsyncSession, osu_id: int) -> None:
    assert await gather_facts(session, osu_id) is None


async def test_a_player_without_practice_maps(engine: Engine, session: AsyncSession) -> None:
    sql(engine, "DELETE FROM recommendations")

    facts = await gather_facts(session, 2)

    assert facts is not None and facts.maps == []


# ---------- POST /players/{osu_id}/report ----------


async def call(
    db: AsyncEngine, method: str, path: str, llm: LLMClient | None = None
) -> httpx.Response:
    """A request to the app over this database. The model is always set here (none by
    default): a COACH_LLM in a developer's .env must not reach these tests."""
    app = create_app()

    async def session() -> AsyncIterator[AsyncSession]:
        async with async_sessionmaker(db)() as s:
            yield s

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_llm] = lambda: llm
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path)


async def test_the_report(db: AsyncEngine, session: AsyncSession) -> None:
    resp = await call(db, "POST", "/players/2/report")

    assert resp.status_code == 200
    body = resp.json()
    facts = await gather_facts(session, 2)
    assert facts is not None
    assert body == template_report(facts).model_dump()  # the template, sent as it is
    assert body["source"] == "template"
    assert [w["skill"] for w in body["weaknesses"]] == ["consistency", "speed", "stamina"]
    assert body["summary"].endswith("from 1 plays and 2 uploaded replay(s).")
    assert [m["beatmap_id"] for m in body["maps"]] == [11, 10]


@pytest.mark.parametrize(
    ("method", "path", "status", "detail"),
    [
        ("POST", "/players/999/report", 404, "player 999 not found"),
        ("POST", "/players/3/report", 409, "this player's skills aren't scored yet"),
        ("POST", "/players/abc/report", 422, None),
        ("GET", "/players/2/report", 405, None),  # written on request: a POST
    ],
    ids=["unknown-player", "not-scored", "not-an-id", "get"],
)
async def test_the_reports_errors(
    db: AsyncEngine, method: str, path: str, status: int, detail: str | None
) -> None:
    resp = await call(db, method, path)

    assert resp.status_code == status
    if detail:
        assert resp.json()["detail"] == detail


# ---------- With a model (a fake one) ----------


class FakeModel:
    """Writes at once: words for whatever weaknesses it's asked about, with no numbers.
    Or fails, if given an error."""

    name = "fake:model"

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def chat(self, messages: list[Message], tools: list[Tool] | None = None,
                   schema: dict[str, Any] | None = None) -> Reply:  # fmt: skip
        if self.error:
            raise self.error
        if schema is None:
            return Reply("Ready.", [], {"role": "assistant", "content": "Ready."})
        skills = schema["properties"]["weaknesses"]["items"]["properties"]["skill"]["enum"]
        words = {
            "summary": "Keep it up.",
            "weaknesses": [{"skill": s, "explanation": f"Work on {s}."} for s in skills],
            "plan": [{"skill": s, "text": f"Practise {s}."} for s in skills],
        }
        return Reply(json.dumps(words), [], {"role": "assistant", "content": json.dumps(words)})


async def test_the_report_written_by_a_model(db: AsyncEngine) -> None:
    resp = await call(db, "POST", "/players/2/report", llm=FakeModel())

    assert resp.status_code == 200
    body = resp.json()
    assert (body["source"], body["model"], body["fallback_reason"]) == ("llm", "fake:model", None)
    assert body["summary"] == "Keep it up."
    # The facts are still code's: the weaknesses in order, with their scores and maps.
    assert [(w["skill"], w["score"], w["explanation"]) for w in body["weaknesses"]] == [
        ("consistency", 30.0, "Work on consistency."),
        ("speed", 40.0, "Work on speed."),
        ("stamina", 55.0, "Work on stamina."),
    ]
    assert [m["beatmap_id"] for m in body["maps"]] == [11, 10]


async def test_a_model_that_fails_gives_the_template(
    db: AsyncEngine, session: AsyncSession
) -> None:
    resp = await call(db, "POST", "/players/2/report", llm=FakeModel(LLMError("ConnectError")))

    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "template"
    assert body["fallback_reason"] == "the model couldn't be reached (ConnectError)"
    facts = await gather_facts(session, 2)
    assert facts is not None
    assert body["summary"] == template_report(facts).summary


async def test_no_model_is_asked_for_an_unscored_player(db: AsyncEngine) -> None:
    # A 409 before any model call: a model that would fail must not turn it into a 200.
    resp = await call(
        db, "POST", "/players/3/report", llm=FakeModel(LLMError("must not be called"))
    )

    assert resp.status_code == 409
