"""Candidate maps for the recommender: well-known ranked osu! standard maps at every level.

Until now the only stored maps are ones synced players have played. The recommender needs
maps to suggest at any player's level, so this searches osu! band by band (2-2.5 stars,
2.5-3, ... up to 9) for the most played ranked mapsets, and keeps a few dozen
difficulties per band: one per mapset, so a band isn't one song's difficulties.

The maps' rows are stored here; their objects and pattern vectors come from fetch_beatmap,
queued by the worker for each map that doesn't have them yet.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach.models import Beatmap, BeatmapObject
from osu_coach.osu.client import OsuClient
from osu_coach.osu.schemas import SearchBeatmap, SearchBeatmapset
from osu_coach.sync import beatmap_values, upsert_beatmaps

BAND_WIDTH = 0.5
LOWEST, HIGHEST = 2.0, 9.0  # below 2 stars there's little to practise; above 9, few maps
PER_BAND = 25  # difficulties kept per band
MAX_PAGES = 3  # search pages per band (50 mapsets each) before settling for fewer


def bands() -> list[tuple[float, float]]:
    """The star bands, lowest first: (2.0, 2.5), (2.5, 3.0), ... (8.5, 9.0)."""
    count = round((HIGHEST - LOWEST) / BAND_WIDTH)
    return [(LOWEST + i * BAND_WIDTH, LOWEST + (i + 1) * BAND_WIDTH) for i in range(count)]


def pick(
    sets: Sequence[SearchBeatmapset], low: float, high: float, wanted: int
) -> list[tuple[SearchBeatmap, SearchBeatmapset]]:
    """Up to `wanted` difficulties from low up to (not including) high stars, in the sets'
    order (most played first), one per set: the ranked osu! standard difficulty nearest
    the band's middle."""
    middle = (low + high) / 2
    picked: list[tuple[SearchBeatmap, SearchBeatmapset]] = []
    for s in sets:
        if len(picked) >= wanted:
            break
        fits = [
            b
            for b in s.beatmaps
            if b.mode == "osu" and b.status == "ranked" and low <= b.difficulty_rating < high
        ]
        if fits:
            picked.append((min(fits, key=lambda b: abs(b.difficulty_rating - middle)), s))
    return picked


async def search_band(
    osu: OsuClient, low: float, high: float, wanted: int = PER_BAND
) -> list[tuple[SearchBeatmap, SearchBeatmapset]]:
    """The band's picks, reading more search pages until there are enough (or none left)."""
    picked: list[tuple[SearchBeatmap, SearchBeatmapset]] = []
    seen: set[int] = set()  # mapsets already picked from, in case pages overlap
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        page = await osu.search_beatmapsets(low, high, cursor)
        new_sets = [s for s in page.beatmapsets if s.id not in seen]
        for b, s in pick(new_sets, low, high, wanted - len(picked)):
            picked.append((b, s))
            seen.add(s.id)
        cursor = page.cursor_string
        if len(picked) >= wanted or cursor is None:
            break
    return picked


@dataclass(frozen=True)
class CandidatesResult:
    maps: int  # candidates found, over every band
    to_fetch: list[int]  # those without objects yet: fetch_beatmap's work, by id


async def collect_candidates(osu: OsuClient, session: AsyncSession) -> CandidatesResult:
    """Find and store candidate maps in every band. Returns the ones still to fetch.

    Each band's maps are committed as they're found: if osu! fails part-way, the bands
    done are kept.
    """
    found: list[int] = []
    for low, high in bands():
        picked = await search_band(osu, low, high)
        await upsert_beatmaps(session, [beatmap_values(b, s, b.max_combo) for b, s in picked])
        await session.commit()
        found.extend(b.id for b, _ in picked)

    has_objects = exists().where(BeatmapObject.beatmap_id == Beatmap.id)
    to_fetch = await session.scalars(
        select(Beatmap.id).where(Beatmap.id.in_(found), ~has_objects).order_by(Beatmap.id)
    )
    return CandidatesResult(maps=len(set(found)), to_fetch=list(to_fetch))
