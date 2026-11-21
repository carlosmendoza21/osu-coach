"""Integration tests for osu_coach.candidates.collect_candidates against a real Postgres:
the candidates' map rows, which still need fetching, and a failure part-way. osu! is a
fake serving hand-made search pages, one per band.

Run with:  uv run pytest -m integration
Needs:     Postgres (docker compose up -d postgres). Skips with the reason if it's missing.
           Uses a separate `<db>_test` database (see conftest.py).
"""

from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from osu_coach.candidates import CandidatesResult, bands, collect_candidates
from osu_coach.osu.client import OsuApiError
from osu_coach.osu.schemas import SearchBeatmapset, SearchResponse
from tests.support import ALL_TABLES, load

pytestmark = pytest.mark.integration

DIFFICULTY = load("search_5_stars.json")["beatmapsets"][0]["beatmaps"][0]


def mapset(set_id: int, map_id: int, stars: float) -> SearchBeatmapset:
    beatmap = {**DIFFICULTY, "id": map_id, "beatmapset_id": set_id, "difficulty_rating": stars,
               "version": f"diff {map_id}", "max_combo": 500 + map_id,
               "checksum": f"{map_id:032x}"}  # checksums are unique: one per map  # fmt: skip
    return SearchBeatmapset.model_validate(
        {"id": set_id, "artist": f"artist {set_id}", "title": f"title {set_id}",
         "creator": "mapper", "play_count": 1, "beatmaps": [beatmap]}
    )  # fmt: skip


class FakeSearch:
    """One page per band: two sets in the band (map ids 100*band + 1 and + 2). Fails on
    the band starting at `fail_at`, if given."""

    def __init__(self, fail_at: float | None = None) -> None:
        self.fail_at = fail_at
        self.calls: list[tuple[float, float]] = []

    async def search_beatmapsets(
        self, min_stars: float, max_stars: float, cursor: str | None = None
    ) -> SearchResponse:
        self.calls.append((min_stars, max_stars))
        if min_stars == self.fail_at:
            raise OsuApiError("osu! is down")
        n = band_number(min_stars)
        middle = min_stars + 0.25
        sets = [mapset(n * 100 + i, n * 100 + i, middle) for i in (1, 2)]
        return SearchResponse(beatmapsets=sets, cursor_string=None)


def band_number(low: float) -> int:
    return [b[0] for b in bands()].index(low) + 1  # 1 for 2.0-2.5 ... 14 for 8.5-9.0


ALL_IDS = sorted(n * 100 + i for n in range(1, 15) for i in (1, 2))


@pytest.fixture
async def db(test_url: URL) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(test_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture(autouse=True)
def cleanup(engine: Engine) -> Iterator[None]:
    yield
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {ALL_TABLES}"))


async def collect(db: AsyncEngine, osu: FakeSearch) -> CandidatesResult:
    async with async_sessionmaker(db)() as session:
        return await collect_candidates(osu, session)  # type: ignore[arg-type]


def stored_ids(engine: Engine) -> list[int]:
    with engine.connect() as conn:
        return list(conn.execute(text("SELECT id FROM beatmaps ORDER BY id")).scalars())


async def test_every_band_is_searched_once(db: AsyncEngine) -> None:
    osu = FakeSearch()

    await collect(db, osu)

    assert osu.calls == bands()


async def test_the_candidates_are_stored_and_all_need_fetching(
    db: AsyncEngine, engine: Engine
) -> None:
    result = await collect(db, FakeSearch())

    assert result.maps == 28  # 2 in each of 14 bands
    assert result.to_fetch == ALL_IDS  # nothing has objects yet; sorted
    assert stored_ids(engine) == ALL_IDS


async def test_a_candidates_row(db: AsyncEngine, engine: Engine) -> None:
    await collect(db, FakeSearch())

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT beatmapset_id, artist, title, version, star_rating, max_combo, "
                 "status, mode, checksum FROM beatmaps WHERE id = 702")
        ).one()  # fmt: skip
    # Band 7 is 5.0-5.5: the set's artist and title, the difficulty's own fields.
    assert tuple(row) == (702, "artist 702", "title 702", "diff 702", 5.25, 1202, "ranked", "osu",
                          "0" * 29 + "2be")  # 702 in hex  # fmt: skip


async def test_maps_with_objects_arent_fetched_again(db: AsyncEngine, engine: Engine) -> None:
    await collect(db, FakeSearch())
    with engine.begin() as conn:  # map 301 has been fetched since
        conn.execute(
            text(
                "INSERT INTO hit_objects (beatmap_id, idx, kind, time_ms, end_time_ms, x, y, "
                "spacing, angle, time_since_prev_ms, pattern) "
                "VALUES (301, 0, 'circle', 0, 0, 0, 0, NULL, NULL, NULL, 'other')"
            )
        )

    result = await collect(db, FakeSearch())

    assert result.maps == 28  # still a candidate
    assert 301 not in result.to_fetch
    assert len(result.to_fetch) == 27


async def test_maps_outside_the_candidates_are_left_alone(db: AsyncEngine, engine: Engine) -> None:
    # A played map without objects isn't this function's to fetch (the sync queues those).
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO beatmaps (id, beatmapset_id, artist, title, creator, version, "
                "star_rating, ar, od, cs, hp, bpm, length_s, hit_length_s, count_circles, "
                "count_sliders, count_spinners, status, mode) VALUES (5, 5, 'a', 't', 'c', "
                "'v', 5, 9, 8, 4, 5, 180, 90, 80, 1, 0, 0, 'ranked', 'osu')"
            )
        )

    result = await collect(db, FakeSearch())

    assert 5 not in result.to_fetch
    assert 5 in stored_ids(engine)


async def test_running_again_adds_no_rows(db: AsyncEngine, engine: Engine) -> None:
    await collect(db, FakeSearch())
    await collect(db, FakeSearch())

    assert stored_ids(engine) == ALL_IDS


async def test_a_failure_part_way_keeps_the_bands_done(db: AsyncEngine, engine: Engine) -> None:
    # osu! fails on the 4th band (3.5-4.0): bands 1-3 were committed as they were found.
    with pytest.raises(OsuApiError):
        await collect(db, FakeSearch(fail_at=3.5))

    assert stored_ids(engine) == [101, 102, 201, 202, 301, 302]
