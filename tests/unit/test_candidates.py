"""Unit tests for osu_coach.candidates: the star bands, which difficulties a search page
gives a band, and reading pages until a band is full. Hand-made mapsets, a real search
page (tests/fixtures/osu/search_5_stars.json), and a fake osu! that serves pages. Storing
the candidates is in tests/integration/test_candidates_live.py.
"""

from itertools import pairwise
from typing import Any

import pytest

from osu_coach.candidates import BAND_WIDTH, MAX_PAGES, PER_BAND, bands, pick, search_band
from osu_coach.osu.schemas import SearchBeatmapset, SearchResponse
from tests.support import load

REAL_PAGE = SearchResponse.model_validate(load("search_5_stars.json"))
DIFFICULTY = load("search_5_stars.json")["beatmapsets"][0]["beatmaps"][0]


def mapset(
    set_id: int, *diffs: tuple[int, float] | tuple[int, float, str, str]
) -> SearchBeatmapset:
    """A mapset whose difficulties are (id, stars) or (id, stars, mode, status)."""
    beatmaps = [
        {**DIFFICULTY, "id": d[0], "beatmapset_id": set_id, "difficulty_rating": d[1],
         "mode": d[2] if len(d) > 2 else "osu", "status": d[3] if len(d) > 3 else "ranked"}
        for d in diffs
    ]  # fmt: skip
    return SearchBeatmapset.model_validate(
        {"id": set_id, "artist": "a", "title": f"set {set_id}", "creator": "c",
         "play_count": 1000 - set_id, "beatmaps": beatmaps}
    )  # fmt: skip


# ---------- Bands ----------


def test_bands() -> None:
    got = bands()

    assert got[0] == (2.0, 2.5)
    assert got[-1] == (8.5, 9.0)
    assert len(got) == 14
    # Each band starts where the last ended: no gaps, no overlaps, all the same width.
    assert all(a[1] == b[0] for a, b in pairwise(got))
    assert all(high - low == BAND_WIDTH for low, high in got)


# ---------- Picking a band's difficulties ----------

# Every case picks for the 5-5.5 star band (its middle is 5.25).
PICK_CASES = [
    # (sets, wanted, expected picked map ids)
    pytest.param([], 25, [], id="no-sets"),
    pytest.param([mapset(1, (10, 5.2))], 25, [10], id="one-fits"),
    pytest.param([mapset(1, (10, 4.9), (11, 5.6))], 25, [], id="none-in-band"),
    # Edge: the band includes its low end and stops short of its high end.
    pytest.param([mapset(1, (10, 5.0))], 25, [10], id="low-edge-in"),
    pytest.param([mapset(1, (10, 5.5))], 25, [], id="high-edge-out"),
    pytest.param([mapset(1, (10, 5.4999))], 25, [10], id="just-under-high"),
    # One per set: the difficulty nearest the band's middle.
    pytest.param([mapset(1, (10, 5.05), (11, 5.3), (12, 5.45))], 25, [11], id="nearest-middle"),
    # Only osu! standard, and only ranked (a loved difficulty can sit in a ranked set).
    pytest.param([mapset(1, (10, 5.25, "taiko", "ranked"), (11, 5.1))], 25, [11],
                 id="other-mode-skipped"),
    pytest.param([mapset(1, (10, 5.25, "osu", "loved"))], 25, [], id="not-ranked-skipped"),
    pytest.param([mapset(1, (10, 5.25, "fruits", "ranked"))], 25, [], id="only-other-modes"),
    # The sets' order (most played first) is kept, and it stops at `wanted`.
    pytest.param([mapset(i, (i * 10, 5.2)) for i in (3, 1, 2)], 25, [30, 10, 20], id="order-kept"),
    pytest.param([mapset(i, (i * 10, 5.2)) for i in range(1, 6)], 2, [10, 20],
                 id="stops-at-wanted"),
    # Edge: sets with nothing in the band don't use up `wanted`.
    pytest.param([mapset(1, (10, 3.0)), mapset(2, (20, 5.2)), mapset(3, (30, 5.3))], 2, [20, 30],
                 id="misses-dont-count"),
    pytest.param([mapset(1, (10, 5.2))], 0, [], id="none-wanted"),  # edge: nothing asked for
]  # fmt: skip


@pytest.mark.parametrize(("sets", "wanted", "expected"), PICK_CASES)
def test_pick(sets: list[SearchBeatmapset], wanted: int, expected: list[int]) -> None:
    picked = pick(sets, 5.0, 5.5, wanted)

    assert [b.id for b, _ in picked] == expected
    # Each pick comes with its own set (for the artist and title).
    assert all(b.beatmapset_id == s.id for b, s in picked)


def test_pick_from_a_real_page() -> None:
    # 4 most played sets with a 5-5.5 star difficulty; each set's nearest to 5.25.
    picked = pick(REAL_PAGE.beatmapsets, 5.0, 5.5, PER_BAND)

    assert [(b.id, round(b.difficulty_rating, 3)) for b, _ in picked] == [
        (712376, 5.104),  # Reol - No title [Insane]
        (1612454, 5.419),  # Make a Move: 5.42 beats 5.06 and 5.08
        (1764213, 5.129),  # Harumachi Clover: its only one in band
        (1836274, 5.211),  # quaver: 5.21 beats 5.30 and 5.37
    ]
    assert all(b.mode == "osu" for b, _ in picked)


# ---------- Reading pages ----------


class FakeSearch:
    """Serves search pages in order and records each call's (min, max, cursor)."""

    def __init__(self, pages: list[list[SearchBeatmapset]]) -> None:
        self.pages = pages
        self.calls: list[tuple[float, float, str | None]] = []

    async def search_beatmapsets(
        self, min_stars: float, max_stars: float, cursor: str | None = None
    ) -> SearchResponse:
        self.calls.append((min_stars, max_stars, cursor))
        i = len(self.calls) - 1
        last = i >= len(self.pages) - 1
        sets = self.pages[i] if i < len(self.pages) else []
        return SearchResponse(beatmapsets=sets, cursor_string=None if last else f"page{i + 1}")


async def search(pages: list[list[SearchBeatmapset]], wanted: int) -> tuple[list[int], FakeSearch]:
    osu = FakeSearch(pages)
    picked = await search_band(osu, 5.0, 5.5, wanted)  # type: ignore[arg-type]
    return [b.id for b, _ in picked], osu


def fits(*ids: int) -> list[SearchBeatmapset]:
    """A page of sets each with one difficulty in the 5-5.5 band (set i has map i*10)."""
    return [mapset(i, (i * 10, 5.2)) for i in ids]


async def test_one_page_is_enough() -> None:
    ids, osu = await search([fits(1, 2, 3), fits(4, 5)], wanted=2)

    assert ids == [10, 20]
    assert osu.calls == [(5.0, 5.5, None)]  # the next page isn't asked for


async def test_more_pages_until_the_band_is_full() -> None:
    ids, osu = await search([fits(1, 2), fits(3, 4), fits(5, 6)], wanted=3)

    assert ids == [10, 20, 30]
    # Each next page is asked for with the last page's cursor.
    assert osu.calls == [(5.0, 5.5, None), (5.0, 5.5, "page1")]


async def test_the_last_page_ends_the_search() -> None:
    ids, osu = await search([fits(1), fits(2)], wanted=25)

    assert ids == [10, 20]  # fewer than wanted: there are no more
    assert len(osu.calls) == 2


async def test_at_most_max_pages() -> None:
    pages = [fits(i) for i in range(1, 10)]

    ids, osu = await search(pages, wanted=25)

    assert len(osu.calls) == MAX_PAGES
    assert ids == [i * 10 for i in range(1, MAX_PAGES + 1)]


async def test_a_set_seen_on_an_earlier_page_isnt_picked_again() -> None:
    # Edge: pages can overlap when play counts change between requests.
    ids, _ = await search([fits(1, 2), fits(2, 3)], wanted=25)

    assert ids == [10, 20, 30]


async def test_pages_with_nothing_in_band_are_read_past() -> None:
    off_band = [mapset(i, (i * 10, 7.0)) for i in (1, 2)]

    ids, osu = await search([off_band, fits(3)], wanted=25)

    assert ids == [30]
    assert len(osu.calls) == 2


def test_the_fake_serves_like_osu() -> None:
    # The fake's pages are what osu!'s are: SearchResponse, a cursor until the last.
    page: Any = SearchResponse(beatmapsets=fits(1), cursor_string=None)
    assert page.beatmapsets[0].beatmaps[0].id == 10
