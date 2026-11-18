"""osu!'s difficulty attributes (aim, speed, star rating...) for the maps a player played,
with the mods they played them with: what the skill profile is scored from.

Each (map, mods) pair is fetched once and kept. The mods are normalized first, so plays
that differ only by mods that can't change difficulty share one row.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach.models import Beatmap, BeatmapAttributes, Score
from osu_coach.osu.client import IncompleteAttributesError, OsuClient, OsuNotFoundError
from osu_coach.osu.schemas import DifficultyAttributes

log = logging.getLogger(__name__)

# Mods that never change a map's difficulty. Everything else is kept: HD, for one, does
# change osu!'s star rating (it's harder to read).
NO_DIFFICULTY = frozenset({"NF", "SD", "PF"})
# Mods that play the same as another: NC is DT with a pitch change, DC is HT's.
SAME_AS = {"NC": "DT", "DC": "HT"}


def difficulty_mods(mods: Iterable[str]) -> tuple[str, ...]:
    """The mods that matter for difficulty, normalized and sorted: ["NC", "HD", "SD"] ->
    ("DT", "HD")."""
    return tuple(sorted({SAME_AS.get(m, m) for m in mods} - NO_DIFFICULTY))


def mods_key(mods: Iterable[str]) -> str:
    """How a mod set is stored: "" for none, "DT,HD"."""
    return ",".join(difficulty_mods(mods))


async def attributes_to_fetch(session: AsyncSession, osu_id: int) -> list[tuple[int, str]]:
    """The (map, mods key) pairs of a player's osu! standard plays not stored yet, sorted."""
    plays = await session.execute(
        select(Score.beatmap_id, Score.mods)
        .join(Beatmap, Beatmap.id == Score.beatmap_id)
        .where(Score.player_id == osu_id, Beatmap.mode == "osu")
    )
    wanted = {(beatmap_id, mods_key(mods)) for beatmap_id, mods in plays}
    if not wanted:
        return []
    stored = await session.execute(
        select(BeatmapAttributes.beatmap_id, BeatmapAttributes.mods).where(
            BeatmapAttributes.beatmap_id.in_({b for b, _ in wanted})
        )
    )
    return sorted(wanted - {(b, m) for b, m in stored})


async def store_attributes(
    session: AsyncSession, beatmap_id: int, key: str, attrs: DifficultyAttributes
) -> None:
    """Store (or replace) one map's attributes for one mods key. Doesn't commit."""
    values = {
        "beatmap_id": beatmap_id,
        "mods": key,
        "star_rating": attrs.star_rating,
        "max_combo": attrs.max_combo,
        "aim_difficulty": attrs.aim_difficulty,
        "speed_difficulty": attrs.speed_difficulty,
        "speed_note_count": attrs.speed_note_count,
        "slider_factor": attrs.slider_factor,
        "aim_difficult_strain_count": attrs.aim_difficult_strain_count,
        "speed_difficult_strain_count": attrs.speed_difficult_strain_count,
    }
    stmt = insert(BeatmapAttributes).values(**values)
    update = {k: stmt.excluded[k] for k in values if k not in ("beatmap_id", "mods")}
    await session.execute(
        stmt.on_conflict_do_update(index_elements=["beatmap_id", "mods"], set_=update)
    )


@dataclass(frozen=True)
class AttributesResult:
    fetched: int
    not_found: int  # maps osu! no longer has
    # Maps osu! has only a star rating for (unranked): asked again on the next run, in
    # case they've been ranked since.
    incomplete: int = 0


async def fetch_player_attributes(
    osu: OsuClient, session: AsyncSession, osu_id: int
) -> AttributesResult:
    """Fetch and store the attributes a player's plays still need.

    Committed after each pair: if osu! fails part-way, what was fetched is kept, and the
    next run only asks for the rest. A map osu! doesn't know any more, or has only a star
    rating for (an unranked map), is skipped and counted.
    """
    fetched = not_found = incomplete = 0
    for beatmap_id, key in await attributes_to_fetch(session, osu_id):
        mods = key.split(",") if key else []
        try:
            attrs = await osu.get_beatmap_attributes(beatmap_id, mods)
        except OsuNotFoundError:
            log.info("map %s is gone from osu!: no attributes for it", beatmap_id)
            not_found += 1
            continue
        except IncompleteAttributesError:
            log.info("map %s has only a star rating on osu! (unranked)", beatmap_id)
            incomplete += 1
            continue
        await store_attributes(session, beatmap_id, key, attrs)
        await session.commit()
        fetched += 1
    return AttributesResult(fetched, not_found, incomplete)
