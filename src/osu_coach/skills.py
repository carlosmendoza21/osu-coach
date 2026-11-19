"""A player's skill profile: aim, speed, accuracy, consistency, stamina and reading, each
scored 0-100 from their top plays and osu!'s difficulty attributes for those maps.

How a skill is scored:
1. Each play gets a value for the skill (say, its map's aim difficulty, less for misses).
2. The plays' values are sorted, best first, and averaged with weights 1, 0.95, 0.95^2...
   as osu!'s pp does: the best plays count most, but one fluke can't set the score.
3. That average is placed on a 0-100 scale through hand-set reference points.

The reference points are estimates of where typical, good and top players' plays sit,
not fitted to data: a first version, to calibrate later against many players (the
data.ppy.sh dumps).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach.attributes import mods_key
from osu_coach.difficulty import UnsupportedModsError, play_conditions
from osu_coach.models import Beatmap, BeatmapAttributes, Player, Score
from osu_coach.models import SkillProfile as SkillProfileRow
from osu_coach.osufile import Difficulty

SKILLS = ("aim", "speed", "accuracy", "consistency", "stamina", "reading")
WEIGHT_DECAY = 0.95  # each play counts 95% as much as the one before it, as in pp
MISS_FACTOR = 0.97  # each miss takes 3% off a play's aim or speed value

# Raw value -> score: straight lines between these points, flat beyond the ends.
SCALES: dict[str, list[tuple[float, float]]] = {
    # osu!'s aim difficulty (about half a map's star rating): 1.5 is a 3-star map's aim,
    # 2.5 a 5-star's, 3.5 a 7-star's.
    "aim": [(0.5, 0), (1.5, 30), (2.5, 60), (3.5, 85), (4.5, 100)],
    "speed": [(0.5, 0), (1.2, 30), (2.0, 60), (3.0, 85), (4.0, 100)],
    # Accuracy, nudged for OD (see accuracy_value).
    "accuracy": [(0.85, 0), (0.92, 30), (0.96, 60), (0.985, 85), (1.0, 100)],
    # How much of a map was played clean (see consistency_value): 1 is a full combo.
    "consistency": [(0.2, 0), (0.5, 30), (0.8, 60), (0.95, 85), (1.0, 100)],
    # Hard-to-tap notes in a cleared map: a long stream map has several hundred.
    "stamina": [(0, 0), (60, 30), (200, 60), (500, 85), (1000, 100)],
    # The AR the player saw (with DT/HR), plus a bonus for HD and FL.
    "reading": [(7, 0), (8.5, 30), (9.5, 60), (10.5, 85), (11.3, 100)],
}


@dataclass(frozen=True)
class Play:
    """One play, with what scoring needs from its map (as played, with its mods)."""

    accuracy: float  # 0-1
    count_miss: int
    max_combo: int  # the play's
    map_max_combo: int  # the map's, with these mods
    aim_difficulty: float
    speed_difficulty: float
    speed_note_count: float
    ar: float  # as the player saw it: HR/EZ and DT/HT applied
    od: float  # the same
    mods: tuple[str, ...]


@dataclass(frozen=True)
class SkillProfile:
    scores: dict[str, float]  # skill -> 0-100, one decimal
    plays_used: int
    plays_skipped: int  # no attributes yet, or mods the scoring doesn't model

    @property
    def strongest(self) -> str:
        return max(SKILLS, key=lambda s: self.scores[s])

    @property
    def weakest(self) -> str:
        return min(SKILLS, key=lambda s: self.scores[s])


# ---------- Each play's value for each skill ----------


def miss_factor(play: Play) -> float:
    return MISS_FACTOR**play.count_miss


def aim_value(play: Play) -> float:
    return play.aim_difficulty * miss_factor(play)


def speed_value(play: Play) -> float:
    # Tapping well shows in accuracy too: a 90% play on a fast map tapped less of it.
    return play.speed_difficulty * miss_factor(play) * play.accuracy**2


def accuracy_value(play: Play) -> float:
    # The same accuracy is worth a little more on a stricter map: +0.5% per OD over 8.
    return play.accuracy + 0.005 * (play.od - 8)


def consistency_value(play: Play) -> float:
    # The share of the map's combo the player held, less for every miss.
    if play.map_max_combo <= 0:
        return 0.0
    return min(1.0, play.max_combo / play.map_max_combo) * 0.95**play.count_miss


def stamina_value(play: Play) -> float:
    # Hard-to-tap notes cleared: none count from a play with more than a few misses.
    return play.speed_note_count if play.count_miss <= 3 else 0.0


def reading_value(play: Play) -> float:
    bonus = (0.5 if "HD" in play.mods else 0.0) + (1.0 if "FL" in play.mods else 0.0)
    return play.ar + bonus


VALUES: dict[str, Callable[[Play], float]] = {
    "aim": aim_value,
    "speed": speed_value,
    "accuracy": accuracy_value,
    "consistency": consistency_value,
    "stamina": stamina_value,
    "reading": reading_value,
}


# ---------- Combining and scaling ----------


def weighted_top(values: Sequence[float]) -> float:
    """The values best first, averaged with weights 1, 0.95, 0.95^2... (0 for none)."""
    ordered = sorted(values, reverse=True)
    weights = [WEIGHT_DECAY**i for i in range(len(ordered))]
    if not ordered:
        return 0.0
    return sum(v * w for v, w in zip(ordered, weights, strict=True)) / sum(weights)


def to_score(raw: float, scale: Sequence[tuple[float, float]]) -> float:
    """A raw value on a 0-100 scale: straight lines between the points, flat outside."""
    if raw <= scale[0][0]:
        return scale[0][1]
    for (x0, y0), (x1, y1) in pairwise(scale):
        if raw <= x1:
            return y0 + (y1 - y0) * (raw - x0) / (x1 - x0)
    return scale[-1][1]


def skill_profile(plays: Sequence[Play], plays_skipped: int = 0) -> SkillProfile:
    """Score each skill from these plays (a player's top plays, say)."""
    scores = {
        skill: round(to_score(weighted_top([VALUES[skill](p) for p in plays]), SCALES[skill]), 1)
        if plays
        else 0.0
        for skill in SKILLS
    }
    return SkillProfile(scores, plays_used=len(plays), plays_skipped=plays_skipped)


# ---------- A player's plays, from the database ----------


async def load_plays(session: AsyncSession, osu_id: int) -> tuple[list[Play], int]:
    """A player's passed osu! standard plays that have their map's attributes stored, and
    how many were skipped (no attributes yet, or mods the scoring doesn't model)."""
    rows = (
        await session.execute(
            select(Score, Beatmap)
            .join(Beatmap, Beatmap.id == Score.beatmap_id)
            .where(Score.player_id == osu_id, Beatmap.mode == "osu", Score.passed)
        )
    ).all()
    stored = {
        (a.beatmap_id, a.mods): a
        for a in await session.scalars(
            select(BeatmapAttributes).where(
                BeatmapAttributes.beatmap_id.in_({beatmap.id for _, beatmap in rows})
            )
        )
    }
    plays: list[Play] = []
    skipped = 0
    for score, beatmap in rows:
        attrs = stored.get((beatmap.id, mods_key(score.mods)))
        if attrs is None:
            skipped += 1
            continue
        try:
            difficulty = Difficulty(
                hp=beatmap.hp, cs=beatmap.cs, od=beatmap.od, ar=beatmap.ar,
                slider_multiplier=1, slider_tick_rate=1,
            )  # fmt: skip
            conditions = play_conditions(difficulty, score.mods)
        except UnsupportedModsError:  # Relax, Autopilot...: not the player's own skill
            skipped += 1
            continue
        plays.append(
            Play(
                accuracy=score.accuracy,
                count_miss=score.count_miss,
                max_combo=score.max_combo,
                map_max_combo=attrs.max_combo,
                aim_difficulty=attrs.aim_difficulty,
                speed_difficulty=attrs.speed_difficulty,
                speed_note_count=attrs.speed_note_count,
                ar=conditions.effective_ar,
                od=conditions.effective_od,
                mods=tuple(score.mods),
            )
        )
    return plays, skipped


# ---------- A player's stored profile ----------


async def store_profile(session: AsyncSession, osu_id: int, profile: SkillProfile) -> None:
    """Store (or replace) a player's profile. Doesn't commit."""
    values = {
        "player_id": osu_id,
        **profile.scores,
        "strongest": profile.strongest,
        "weakest": profile.weakest,
        "plays_used": profile.plays_used,
        "plays_skipped": profile.plays_skipped,
    }
    stmt = insert(SkillProfileRow).values(**values)
    # computed_at by hand: onupdate doesn't run for an upsert's update.
    update: dict[str, Any] = {k: stmt.excluded[k] for k in values if k != "player_id"}
    update["computed_at"] = func.now()
    await session.execute(stmt.on_conflict_do_update(index_elements=["player_id"], set_=update))


async def update_profile(session: AsyncSession, osu_id: int) -> SkillProfile | None:
    """Score a player from their stored plays and store the profile. Commits. None for a
    player we don't have (the profile needs its player)."""
    if await session.get(Player, osu_id) is None:
        return None
    plays, skipped = await load_plays(session, osu_id)
    profile = skill_profile(plays, skipped)
    await store_profile(session, osu_id, profile)
    await session.commit()
    return profile
