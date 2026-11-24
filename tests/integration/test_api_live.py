"""Integration tests: the running app, end to end, over real HTTP.

Run with:  uv run pytest -m integration
Needs:     the full stack (docker compose up -d --build) with osu! credentials in .env, so
           the API, worker, Postgres, Redis and real osu! are all involved. Skips with the
           reason if the API isn't answering on localhost:8000.
Note: like the app itself, this syncs players and stores replays into the dev database.
"""

import time
from collections.abc import Iterator

import httpx
import pytest
from sqlalchemy import create_engine, text

from osu_coach.config import get_settings
from osu_coach.osufile import parse_osu
from tests.osrdata import osr, play
from tests.support import FIXTURES, load

pytestmark = pytest.mark.integration

API = "http://localhost:8000"
TIMEOUT_S = 60  # a first sync goes through the worker and osu!


@pytest.fixture(scope="module")
def api() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=API, timeout=TIMEOUT_S) as client:
        try:
            client.get("/health", timeout=3)
        except httpx.TransportError:
            pytest.skip(f"The API isn't running at {API}. Start it: docker compose up -d --build")
        yield client


def test_health(api: httpx.Client) -> None:
    resp = api.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_player_profile_and_plays(api: httpx.Client) -> None:
    resp = api.get("/players/peppy")

    assert resp.status_code == 200
    body = resp.json()
    assert (body["player"]["osu_id"], body["player"]["username"]) == (2, "peppy")
    pps = [s["pp"] for s in body["best"]]
    assert len(pps) == 50, "Day 2's goal: the top 50 plays, from the database"
    assert None not in pps
    assert pps == sorted(pps, reverse=True)
    assert len(body["recent"]) <= 20


def test_repeat_request_is_served_from_the_database(api: httpx.Client) -> None:
    first = api.get("/players/peppy").json()
    second = api.get("/players/PEPPY").json()

    # Same sync time: the second request didn't sync again (fresh within the window).
    assert second["player"]["updated_at"] == first["player"]["updated_at"]
    assert second == first


def test_unknown_player_is_404(api: httpx.Client) -> None:
    resp = api.get("/players/no-such-player-osu-coach-test")

    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


def test_a_players_skills(api: httpx.Client) -> None:
    api.get("/players/peppy")  # synced, and their attributes fetched and scored after
    deadline = time.monotonic() + TIMEOUT_S
    while (resp := api.get("/players/2/skills")).status_code == 409:
        assert time.monotonic() < deadline, "peppy's skill profile was never scored"
        time.sleep(1)

    assert resp.status_code == 200
    body = resp.json()
    skills = ("aim", "speed", "accuracy", "consistency", "stamina", "reading")
    assert all(0 <= body[s] <= 100 for s in skills)
    assert body["strongest"] == max(skills, key=lambda s: body[s])
    assert body["weakest"] == min(skills, key=lambda s: body[s])
    assert body["plays_used"] > 0


def test_unknown_players_skills_are_404(api: httpx.Client) -> None:
    resp = api.get(f"/players/{2**62}/skills")

    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


def test_a_players_recommendations(api: httpx.Client) -> None:
    api.get("/players/peppy")  # synced, then scored and recommended after
    deadline = time.monotonic() + TIMEOUT_S
    while (resp := api.get("/players/2/recommendations")).status_code == 409:
        assert time.monotonic() < deadline, "peppy's recommendations were never made"
        time.sleep(1)
    skills = api.get("/players/2/skills").json()

    assert resp.status_code == 200
    body = resp.json()
    assert body["target_skill"] == skills["weakest"]  # what's trained is the weakest skill
    assert 0 < len(body["maps"]) <= 10
    played = {s["beatmap_id"] for s in api.get("/players/peppy").json()["best"]}
    for m in body["maps"]:
        assert m["beatmap"]["id"] not in played  # nothing they've played
        assert "comfort level" in m["reason"]
    sets = [m["beatmap"]["beatmapset_id"] for m in body["maps"]]
    assert len(sets) == len(set(sets))  # one difficulty per mapset


def test_unknown_players_recommendations_are_404(api: httpx.Client) -> None:
    resp = api.get(f"/players/{2**62}/recommendations")

    assert resp.status_code == 404


def test_a_players_report(api: httpx.Client) -> None:
    api.get("/players/peppy")  # synced, scored and recommended (see the tests above)
    deadline = time.monotonic() + TIMEOUT_S
    while (resp := api.post("/players/2/report")).status_code == 409:
        assert time.monotonic() < deadline, "peppy's skills were never scored"
        time.sleep(1)
    skills = api.get("/players/2/skills").json()
    recs = api.get("/players/2/recommendations").json()

    assert resp.status_code == 200
    report = resp.json()
    assert report["source"] == "template"
    # It agrees with the other endpoints: the same weakest skill, scores and maps.
    names = ("aim", "speed", "accuracy", "consistency", "stamina", "reading")
    assert report["weaknesses"][0]["skill"] == skills["weakest"]
    assert report["strengths"][0]["skill"] == skills["strongest"]
    for n in report["strengths"] + report["weaknesses"]:
        assert n["score"] == skills[n["skill"]]
        assert n["skill"] in names
        # Every piece of evidence quotes a number.
        assert n["evidence"] and all(any(c.isdigit() for c in e) for e in n["evidence"])
    assert [m["beatmap_id"] for m in report["maps"]] == [m["beatmap"]["id"] for m in recs["maps"]]
    assert len(report["plan"]) == 3


def test_endpoint_is_documented(api: httpx.Client) -> None:
    paths = api.get("/openapi.json").json()["paths"]

    assert "/players/{username}" in paths
    assert "/players/{osu_id}/skills" in paths
    assert "/players/{osu_id}/recommendations" in paths
    assert "post" in paths["/players/{osu_id}/report"]


# ---------- Uploading a replay ----------

MAP_22423 = load("beatmap.json")["checksum"]


def dev_db(sql: str) -> None:
    engine = create_engine(get_settings().database_url)
    try:
        with engine.begin() as conn:
            conn.execute(text(sql))
    finally:
        engine.dispose()


def forget_analysis(player: str) -> None:
    """Clear the analysis of a test player's replay from earlier runs: an analyzed replay
    needs nothing more, so a repeat upload would queue no jobs."""
    dev_db(
        "DELETE FROM hit_results WHERE replay_id IN "
        f"(SELECT id FROM replays WHERE player_name = '{player}')"
    )
    dev_db(
        "UPDATE replays SET analyzed_at = NULL, unstable_rate = NULL, beatmap_id = NULL "
        f"WHERE player_name = '{player}'"
    )


def dev_value(sql: str) -> object:
    engine = create_engine(get_settings().database_url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql)).scalar()
    finally:
        engine.dispose()


def map_objects() -> object:
    engine = create_engine(get_settings().database_url)
    try:
        with engine.connect() as conn:
            sql = "SELECT count(*) FROM hit_objects WHERE beatmap_id = 22423"
            return conn.execute(text(sql)).scalar()
    finally:
        engine.dispose()


def test_uploading_a_replay_stores_it_and_its_map(api: httpx.Client) -> None:
    # Day 3's goal: uploading a replay stores its map, the map's hit objects, and its
    # frames. A built replay on map 22423, whose objects are removed first so the upload
    # has to fetch them.
    forget_analysis("osu-coach-test")
    dev_db("DELETE FROM hit_objects WHERE beatmap_id = 22423")
    data = osr(checksum=MAP_22423, player="osu-coach-test")

    resp = api.post("/replays/upload", files={"file": ("test.osr", data)})

    assert resp.status_code in (200, 201)  # 200 if an earlier run uploaded the same file
    body = resp.json()
    assert body["created"] is (resp.status_code == 201)
    assert body["replay"]["beatmap_checksum"] == MAP_22423
    assert body["replay"]["frame_count"] == 3
    assert body["map_job_id"] is not None  # the map's objects were missing: fetched

    deadline = time.monotonic() + TIMEOUT_S
    while map_objects() != 254:
        assert time.monotonic() < deadline, "the worker didn't store the map's objects"
        time.sleep(1)

    # Now stored: uploading again finds the same replay and queues nothing.
    again = api.post("/replays/upload", files={"file": ("test.osr", data)})
    assert again.status_code == 200
    assert again.json()["replay"]["id"] == body["replay"]["id"]
    assert again.json()["map_job_id"] is None


def test_a_bad_upload_is_refused(api: httpx.Client) -> None:
    resp = api.post("/replays/upload", files={"file": ("bad.osr", b"not a replay")})

    assert resp.status_code == 422
    assert resp.json() == {"detail": "not a valid .osr file (cut short or damaged)"}


def test_uploading_a_replay_analyzes_it(api: httpx.Client) -> None:
    # Day 4's goal end to end: a replay that plays map 22423 (taps 6 ms either side) is
    # uploaded; the map is fetched, then the replay judged and its analysis stored, with
    # no one running anything. The map's objects and any earlier run's analysis go first.
    osu_file = parse_osu((FIXTURES / "22423.osu").read_text(encoding="utf-8"))
    judged = sum(1 for h in osu_file.hit_objects if h.kind != "spinner")
    frames = play(osu_file.hit_objects, errors=(-6, 6))
    data = osr(checksum=MAP_22423, player="osu-coach-analysis-test", frames=frames, seed=None)
    forget_analysis("osu-coach-analysis-test")
    dev_db("DELETE FROM hit_objects WHERE beatmap_id = 22423")

    body = api.post("/replays/upload", files={"file": ("test.osr", data)}).json()
    replay_id = body["replay"]["id"]
    # The map was missing: fetched first, then the analysis (a chain).
    assert body["map_job_id"] is not None
    assert body["analysis_job_id"] is not None

    deadline = time.monotonic() + TIMEOUT_S
    ur_sql = f"SELECT unstable_rate FROM replays WHERE id = {replay_id}"
    while (ur := dev_value(ur_sql)) is None:
        assert time.monotonic() < deadline, "the worker didn't analyze the replay"
        time.sleep(1)
    assert ur == pytest.approx(60, abs=0.1)
    results_sql = f"SELECT count(*) FROM hit_results WHERE replay_id = {replay_id}"
    assert dev_value(results_sql) == judged

    # Analyzed now: uploading again shows the analysis and queues nothing.
    again = api.post("/replays/upload", files={"file": ("test.osr", data)}).json()
    assert (again["map_job_id"], again["analysis_job_id"]) == (None, None)
    assert again["replay"]["unstable_rate"] == pytest.approx(60, abs=0.1)
    assert again["replay"]["analyzed_at"] is not None

    # And the replay page's data: the analysis with its map and results by pattern.
    detail = api.get(f"/replays/{replay_id}").json()
    assert detail["status"] == "analyzed"
    assert detail["beatmap"]["title"] == "Tori no Uta -Ethereal House Mix-"
    assert sum(detail["judged"].values()) == judged
    assert sum(sum(c.values()) for c in detail["by_pattern"].values()) == judged

    # And the charts' data: every judged note with its timing, and the cursor's grid.
    charts = api.get(f"/replays/{replay_id}/charts").json()
    assert len(charts["notes"]) == judged
    assert {n["error_ms"] for n in charts["notes"]} == {-6.0, 6.0}
    grid = charts["cursor"]
    assert len(grid["counts"]) == grid["cols"] * grid["rows"] == 64 * 48
    assert sum(grid["counts"]) + grid["off_playfield"] == 2 * judged  # down and up per tap


def test_an_unknown_replay_is_404(api: httpx.Client) -> None:
    resp = api.get("/replays/999999999")

    assert resp.status_code == 404
