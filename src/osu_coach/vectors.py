"""A map's pattern vector: what kind of map it is, as a few numbers between 0 and 1.

The recommender compares these to find maps heavy in the pattern a player needs to
practise. Each number is a share or a scaled value, so they're comparable across maps of
any length, and no single one outweighs the rest:

- jump, stream, slider: the share of the map's objects with that pattern label
  (osu_coach.patterns); "other" and spinners make up the rest
- long_stream: the share of stream notes in runs of LONG_STREAM_NOTES or more (stamina)
- spacing_*: how far apart consecutive objects are, in circle diameters (so a map's CS
  doesn't change it), as the share of measured gaps in each band
- bpm: the map's BPM / 300, at most 1
- stars: its star rating / 10, at most 1

The features are of the map as written (no mods): DT is a different map to practise, and
is handled by the recommender, not here.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import exists, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach.models import Beatmap, BeatmapObject, MapVector
from osu_coach.patterns import circle_radius

# Bump when the vector's makeup changes: stored vectors of an older version are rebuilt.
VECTOR_VERSION = 1

FIELDS = (
    "jump",
    "stream",
    "slider",
    "long_stream",
    "spacing_under_1",  # overlapping or touching: streams, stacks
    "spacing_1_2",
    "spacing_2_3",
    "spacing_3_4",
    "spacing_4_up",  # wide jumps
    "bpm",
    "stars",
)
DIMENSIONS = len(FIELDS)

LONG_STREAM_NOTES = 16  # a stream this long is a stamina test, not a burst
SPACING_BANDS = (1.0, 2.0, 3.0, 4.0)  # band edges, in circle diameters
MAX_BPM = 300.0
MAX_STARS = 10.0


class MapObject(Protocol):
    """What the vector needs from each object: both a freshly parsed object
    (patterns.ObjectFeatures) and a stored one (models.BeatmapObject) have these."""

    @property
    def pattern(self) -> str: ...
    @property
    def spacing(self) -> float | None: ...


@dataclass(frozen=True)
class PatternVector:
    values: tuple[float, ...]  # in FIELDS order

    def __getitem__(self, field: str) -> float:
        return self.values[FIELDS.index(field)]

    def as_dict(self) -> dict[str, float]:
        return dict(zip(FIELDS, self.values, strict=True))


def share(part: int, whole: int) -> float:
    return part / whole if whole else 0.0


def long_stream_share(patterns: Sequence[str]) -> float:
    """The share of stream notes that are in runs of LONG_STREAM_NOTES or more."""
    in_long = total = run = 0
    for p in [*patterns, ""]:  # the "" ends a run at the end of the map
        if p == "stream":
            run += 1
            continue
        total += run
        if run >= LONG_STREAM_NOTES:
            in_long += run
        run = 0
    return share(in_long, total)


def spacing_bands(spacings: Sequence[float], diameter: float) -> list[float]:
    """The share of gaps in each band of SPACING_BANDS (in diameters), lowest first."""
    counts = [0] * (len(SPACING_BANDS) + 1)
    for s in spacings:
        d = s / diameter
        counts[sum(d >= edge for edge in SPACING_BANDS)] += 1
    return [share(c, len(spacings)) for c in counts]


def pattern_vector(
    objects: Sequence[MapObject], cs: float, bpm: float, star_rating: float
) -> PatternVector:
    """The pattern vector of a map's objects (in map order) and its CS, BPM and stars."""
    patterns = [o.pattern for o in objects]
    n = len(patterns)
    spacings = [o.spacing for o in objects if o.spacing is not None]
    values = [
        share(patterns.count("jump"), n),
        share(patterns.count("stream"), n),
        share(patterns.count("slider"), n),
        long_stream_share(patterns),
        *spacing_bands(spacings, 2 * circle_radius(cs)),
        min(max(bpm, 0.0) / MAX_BPM, 1.0),
        min(max(star_rating, 0.0) / MAX_STARS, 1.0),
    ]
    return PatternVector(tuple(round(v, 4) for v in values))


# ---------- Stored vectors ----------


async def store_vector(session: AsyncSession, beatmap_id: int, vector: PatternVector) -> None:
    """Store (or replace) a map's vector. Doesn't commit."""
    stmt = insert(MapVector).values(
        beatmap_id=beatmap_id, vector=list(vector.values), version=VECTOR_VERSION
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["beatmap_id"],
            # built_at by hand: onupdate doesn't run for an upsert's update.
            set_={
                "vector": stmt.excluded.vector,
                "version": stmt.excluded.version,
                "built_at": func.now(),
            },
        )
    )


async def maps_without_vectors(session: AsyncSession) -> list[int]:
    """Maps with stored objects but no vector of the current version, by id."""
    has_objects = exists().where(BeatmapObject.beatmap_id == Beatmap.id)
    current = exists().where(
        MapVector.beatmap_id == Beatmap.id, MapVector.version == VECTOR_VERSION
    )
    rows = await session.scalars(
        select(Beatmap.id).where(has_objects, ~current).order_by(Beatmap.id)
    )
    return list(rows)


async def build_vector(session: AsyncSession, beatmap_id: int) -> PatternVector:
    """Build and store a map's vector from its stored objects. Doesn't commit."""
    beatmap = await session.get_one(Beatmap, beatmap_id)
    objects = (
        await session.scalars(
            select(BeatmapObject)
            .where(BeatmapObject.beatmap_id == beatmap_id)
            .order_by(BeatmapObject.idx)
        )
    ).all()
    vector = pattern_vector(objects, beatmap.cs, beatmap.bpm, beatmap.star_rating)
    await store_vector(session, beatmap_id, vector)
    return vector


async def build_missing_vectors(session: AsyncSession) -> int:
    """Build every map's missing or outdated vector, committing each. How many were built."""
    built = 0
    for beatmap_id in await maps_without_vectors(session):
        await build_vector(session, beatmap_id)
        await session.commit()
        built += 1
    return built
