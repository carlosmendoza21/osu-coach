"""Practice maps for a player: maps that train their weakest skill, a little above the
level they're comfortable at, and close to the kind of maps they already play.

1. What to train: the weakest skill in their stored skill profile.
2. At what level: their comfort level is the weighted star rating of their top plays
   (with their mods, as osu! rates them), weighted 1, 0.95, 0.95^2... as pp does. Maps
   for aim, speed, stamina and reading come from a little above it; accuracy and
   consistency are practised at it (a harder map won't teach either).
3. Which maps: of the maps in range, the ones highest in what the skill needs (jumps and
   wide gaps for aim, streams for speed, long streams for stamina, AR for reading, OD
   for accuracy, length for consistency) are taken first. Of those, the ones nearest
   the player's style are chosen: the average pattern vector of the maps they've
   played, compared with pgvector, so the practice still feels like their kind of map.
   Maps the player has played are left out, and only one difficulty per mapset is kept.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, insert, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach.attributes import mods_key
from osu_coach.models import Beatmap, BeatmapAttributes, MapVector, Recommendation, Score
from osu_coach.models import SkillProfile as SkillProfileRow
from osu_coach.skills import weighted_top
from osu_coach.vectors import FIELDS, PatternVector

COUNT = 10  # maps recommended
POOL = 2 * COUNT  # the maps highest in the skill's need, to choose from by style
# Star range, relative to the comfort level: a step up for most skills...
STEP_UP = (0.2, 0.7)
# ...and at the comfort level for these.
AT_LEVEL = (-0.4, 0.1)
AT_LEVEL_SKILLS = frozenset({"accuracy", "consistency"})
WIDEN_BY = 0.5  # the range grows this much each side when it has too few maps
MAX_WIDENINGS = 3


@dataclass(frozen=True)
class Pick:
    beatmap: Beatmap
    vector: PatternVector
    distance: float  # from the player's style, in pattern-vector units


# What each skill needs from a map, as a number (higher is more of it).
NEEDS: dict[str, Callable[[Pick], float]] = {
    "aim": lambda p: p.vector["jump"] + p.vector["spacing_4_up"],
    "speed": lambda p: p.vector["stream"],
    "stamina": lambda p: p.vector["stream"] * p.vector["long_stream"],  # notes in long streams
    "reading": lambda p: p.beatmap.ar,
    "accuracy": lambda p: p.beatmap.od,
    "consistency": lambda p: p.beatmap.hit_length_s,
}


# ---------- The player ----------


async def comfort_stars(session: AsyncSession, osu_id: int) -> float | None:
    """The weighted star rating of the player's passed osu! standard plays, with their
    mods. None if none of their plays has its attributes stored."""
    plays = await session.execute(
        select(Score.beatmap_id, Score.mods)
        .join(Beatmap, Beatmap.id == Score.beatmap_id)
        .where(Score.player_id == osu_id, Score.passed, Beatmap.mode == "osu")
    )
    pairs = [(beatmap_id, mods_key(mods)) for beatmap_id, mods in plays]
    if not pairs:
        return None
    stored = {
        (a.beatmap_id, a.mods): a.star_rating
        for a in await session.scalars(
            select(BeatmapAttributes).where(BeatmapAttributes.beatmap_id.in_({b for b, _ in pairs}))
        )
    }
    stars = [stored[p] for p in pairs if p in stored]
    return round(weighted_top(stars), 2) if stars else None


async def played_maps(session: AsyncSession, osu_id: int) -> set[int]:
    rows = await session.scalars(select(Score.beatmap_id).where(Score.player_id == osu_id))
    return set(rows)


async def player_style(session: AsyncSession, played: set[int]) -> list[float] | None:
    """The average pattern vector of the maps played. None if none has a vector."""
    vectors = (
        await session.scalars(select(MapVector.vector).where(MapVector.beatmap_id.in_(played)))
    ).all()
    if not vectors:
        return None
    return [sum(float(v[i]) for v in vectors) / len(vectors) for i in range(len(FIELDS))]


def star_range(comfort: float, skill: str, widenings: int = 0) -> tuple[float, float]:
    low, high = AT_LEVEL if skill in AT_LEVEL_SKILLS else STEP_UP
    extra = widenings * WIDEN_BY
    return round(comfort + low - extra, 2), round(comfort + high + extra, 2)


# ---------- Choosing ----------


async def maps_in_range(
    session: AsyncSession, style: Sequence[float], low: float, high: float, played: set[int]
) -> list[Pick]:
    """Ranked osu! standard maps with a vector, from low to high stars, not played,
    nearest the player's style first (pgvector's L2 distance)."""
    distance = MapVector.vector.l2_distance(list(style))
    rows = await session.execute(
        select(Beatmap, MapVector.vector, distance)
        .join(MapVector, MapVector.beatmap_id == Beatmap.id)
        .where(
            Beatmap.status == "ranked",
            Beatmap.mode == "osu",
            Beatmap.star_rating >= low,
            Beatmap.star_rating < high,
            Beatmap.id.not_in(played) if played else true(),
        )
        .order_by(distance, Beatmap.id)
    )
    return [Pick(b, PatternVector(tuple(float(x) for x in v)), float(d)) for b, v, d in rows.all()]


def one_per_mapset(picks: Sequence[Pick]) -> list[Pick]:
    """The first pick of each mapset, in order."""
    seen: set[int] = set()
    kept: list[Pick] = []
    for p in picks:
        if p.beatmap.beatmapset_id not in seen:
            seen.add(p.beatmap.beatmapset_id)
            kept.append(p)
    return kept


def choose(picks: Sequence[Pick], skill: str, count: int = COUNT) -> list[Pick]:
    """The maps to recommend from those in range: the POOL highest in the skill's need
    (maps with none of it left out), then the `count` of those nearest the style."""
    need = NEEDS[skill]
    best = sorted(
        (p for p in one_per_mapset(picks) if need(p) > 0), key=lambda p: (-need(p), p.distance)
    )[:POOL]
    return sorted(best, key=lambda p: p.distance)[:count]


async def hardest_map(session: AsyncSession) -> float | None:
    """The highest star rating of any map that could be recommended."""
    return await session.scalar(
        select(func.max(Beatmap.star_rating))
        .join(MapVector, MapVector.beatmap_id == Beatmap.id)
        .where(Beatmap.status == "ranked", Beatmap.mode == "osu")
    )


# ---------- Reasons ----------


def pattern_note(skill: str, pick: Pick) -> str:
    v, b = pick.vector, pick.beatmap
    match skill:
        case "aim":
            return f"{v['jump']:.0%} jumps, {v['spacing_4_up']:.0%} of gaps over 4 circle widths"
        case "speed":
            return f"{v['stream']:.0%} stream notes at {b.bpm:.0f} BPM"
        case "stamina":
            notes = v["stream"] * v["long_stream"]
            return f"{notes:.0%} of its notes in streams of 16+ at {b.bpm:.0f} BPM"
        case "reading":
            return f"AR {b.ar:g}"
        case "accuracy":
            return f"OD {b.od:g}"
        case "consistency":
            return (
                f"{b.hit_length_s // 60}:{b.hit_length_s % 60:02d} of play to hold a combo through"
            )
    raise ValueError(f"unknown skill: {skill}")


def reason(skill: str, pick: Pick, comfort: float) -> str:
    """Why this map: its numbers for the skill, and its level against the player's."""
    stars = pick.beatmap.star_rating
    diff = stars - comfort
    if abs(diff) < 0.05:
        level = f"right at your comfort level ({comfort:.1f}★)"
    else:
        direction = "above" if diff > 0 else "below"
        level = f"{abs(diff):.1f}★ {direction} your comfort level ({comfort:.1f}★)"
    return f"{pattern_note(skill, pick)}; {stars:.1f}★, {level}"


# ---------- For a player ----------


@dataclass(frozen=True)
class Recommendations:
    skill: str  # the one trained: the weakest
    comfort: float
    star_range: tuple[float, float]  # where the maps came from (after any widening)
    picks: list[Pick]


async def recommend(session: AsyncSession, osu_id: int) -> Recommendations | None:
    """Practice maps for a scored player. None without a skill profile, a comfort level
    or a style (no plays with attributes or vectors yet)."""
    profile = await session.get(SkillProfileRow, osu_id)
    if profile is None:
        return None
    comfort = await comfort_stars(session, osu_id)
    played = await played_maps(session, osu_id)
    style = await player_style(session, played)
    if comfort is None or style is None:
        return None
    skill = profile.weakest
    hardest = await hardest_map(session)
    if hardest is None:
        return Recommendations(skill, comfort, (comfort, comfort), [])
    # A level above every map (DT players' can be): practise on the hardest there are.
    # The step-up range is then centred on the hardest map, so it's inside the range
    # (whose top end is left out).
    level = min(comfort, hardest - sum(STEP_UP) / 2)

    # Widen the range until it has enough maps with the skill's need to recommend (the
    # pool thins out at the ends, and some levels have few long streams). Enough for
    # COUNT, not POOL: nearer the player's level matters more than more to choose from.
    for widenings in range(MAX_WIDENINGS + 1):
        low, high = star_range(level, skill, widenings)
        picks = one_per_mapset(await maps_in_range(session, style, low, high, played))
        if sum(NEEDS[skill](p) > 0 for p in picks) >= COUNT:
            break
    return Recommendations(skill, comfort, (low, high), choose(picks, skill))


async def update_recommendations(session: AsyncSession, osu_id: int) -> Recommendations | None:
    """Recommend again and replace the stored recommendations. Commits: with a session
    that expires objects on commit (the default), the picks' maps need reloading before
    their fields are read."""
    result = await recommend(session, osu_id)
    await session.execute(delete(Recommendation).where(Recommendation.player_id == osu_id))
    if result is not None:
        rows: list[dict[str, Any]] = [
            {
                "player_id": osu_id,
                "beatmap_id": p.beatmap.id,
                "rank": i + 1,
                "target_skill": result.skill,
                "score": round(1 / (1 + p.distance), 4),  # 1: exactly their style
                "reason": reason(result.skill, p, result.comfort),
                "comfort_stars": result.comfort,
            }
            for i, p in enumerate(result.picks)
        ]
        if rows:
            await session.execute(insert(Recommendation), rows)
    await session.commit()
    return result
