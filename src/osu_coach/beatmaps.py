"""Fetch one map completely: its details, its .osu file, and its hit objects' features."""

from dataclasses import asdict, dataclass

from sqlalchemy import delete, insert
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach.models import BeatmapObject
from osu_coach.osu.client import OsuClient
from osu_coach.osu.files import BeatmapFiles
from osu_coach.osufile import parse_osu
from osu_coach.patterns import ObjectFeatures, object_features
from osu_coach.sync import beatmap_values, upsert_beatmaps


class UnsupportedModeError(ValueError):
    """The map is for another game mode (taiko, catch, mania); only osu! standard is analyzed."""


class BeatmapMismatchError(ValueError):
    """The .osu file and osu!'s details disagree, so the stored data would be wrong."""


@dataclass(frozen=True)
class FetchResult:
    beatmap_id: int
    version: str  # the difficulty name, e.g. "Hard"
    objects: int  # hit objects stored


async def store_hit_objects(
    session: AsyncSession, beatmap_id: int, features: list[ObjectFeatures]
) -> None:
    """Replace a map's stored hit objects with these.

    Delete then insert, in the caller's transaction: running it again gives the same rows,
    and a map whose file changed never keeps objects from the old version.
    """
    await session.execute(delete(BeatmapObject).where(BeatmapObject.beatmap_id == beatmap_id))
    if features:
        # A list of rows: SQLAlchemy sends them in batches, so even maps with thousands of
        # objects stay under Postgres's limit on parameters per statement.
        rows = [{"beatmap_id": beatmap_id, **asdict(f)} for f in features]
        await session.execute(insert(BeatmapObject), rows)


async def fetch_beatmap(
    osu: OsuClient, files: BeatmapFiles, session: AsyncSession, beatmap_id: int
) -> FetchResult:
    """Fetch, check and store everything about one map, in one transaction."""
    beatmap = await osu.get_beatmap(beatmap_id)
    if beatmap.mode != "osu":
        raise UnsupportedModeError(f"beatmap {beatmap_id} is a {beatmap.mode} map")

    # Downloaded (or taken from the cache) only if its MD5 matches osu!'s checksum.
    path = await files.get(beatmap.id, beatmap.checksum)
    parsed = parse_osu(path.read_text(encoding="utf-8"))

    # One more check before anything is written: the file must have the objects osu!
    # says the map has. A mismatch means the file and the details are different versions.
    counts = {
        kind: sum(o.kind == kind for o in parsed.hit_objects)
        for kind in ("circle", "slider", "spinner")
    }
    expected = {
        "circle": beatmap.count_circles,
        "slider": beatmap.count_sliders,
        "spinner": beatmap.count_spinners,
    }
    if counts != expected:
        raise BeatmapMismatchError(f"beatmap {beatmap_id}: file has {counts}, osu! says {expected}")

    features = object_features(parsed)
    await upsert_beatmaps(session, [beatmap_values(beatmap, beatmap.beatmapset, beatmap.max_combo)])
    await store_hit_objects(session, beatmap.id, features)
    await session.commit()  # all or nothing: the map and its objects together

    return FetchResult(beatmap_id=beatmap.id, version=beatmap.version, objects=len(features))
