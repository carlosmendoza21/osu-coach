"""Integration tests: the running app, end to end, over real HTTP.

Run with:  uv run pytest -m integration
Needs:     the full stack (docker compose up -d --build) with osu! credentials in .env, so
           the API, worker, Postgres, Redis and real osu! are all involved. Skips with the
           reason if the API isn't answering on localhost:8000.
Note: like the app itself, this syncs players into the dev database.
"""

from collections.abc import Iterator

import httpx
import pytest

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


def test_endpoint_is_documented(api: httpx.Client) -> None:
    paths = api.get("/openapi.json").json()["paths"]

    assert "/players/{username}" in paths
