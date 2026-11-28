"""Eval cases: a player's coaching facts and practice maps, saved as JSON.

A case holds everything a report and its checks need, taken from the database once:
- the coaching facts (scores, plays, replays, the maps' reasons), as the coach sees them
- the practice maps as the recommender chose them: each map's stars, AR, OD, BPM,
  length and pattern vector, whether the player has played it, and the comfort level
  and hardest map in the pool they were chosen against

So evals give the same answer every run, need no database, and can run in CI.
"""

import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach.coach import CoachFacts, ReplayFacts, ReportMap, gather_facts
from osu_coach.models import Beatmap, MapVector, Recommendation
from osu_coach.recommender import hardest_map, played_maps, update_recommendations
from osu_coach.skills import Play
from osu_coach.vectors import FIELDS

CASES_DIR = Path("evals/cases")


# ---------- The saved form ----------


class SavedPlay(BaseModel):
    accuracy: float
    count_miss: int
    max_combo: int
    map_max_combo: int
    aim_difficulty: float
    speed_difficulty: float
    speed_note_count: float
    ar: float
    od: float
    mods: list[str]


class SavedReplays(BaseModel):
    count: int
    mean_ur: float | None
    miss_rates: dict[str, float]
    notes: dict[str, int]


class SavedFacts(BaseModel):
    player_id: int
    username: str
    scores: dict[str, float]
    plays: list[SavedPlay]
    replays: SavedReplays | None
    maps: list[ReportMap]


class PracticeMap(BaseModel):
    """A recommended map, with what the checks need to judge it."""

    beatmap_id: int
    beatmapset_id: int
    rank: int
    star_rating: float
    ar: float
    od: float
    bpm: float
    hit_length_s: int
    vector: dict[str, float]  # vectors.FIELDS -> value
    played: bool  # by this player (it shouldn't be)
    reason: str


class Practice(BaseModel):
    """The practice maps, as the recommender chose them."""

    target_skill: str | None  # None: none were made
    comfort_stars: float | None
    hardest_in_pool: float | None  # the hardest map that could have been picked
    maps: list[PracticeMap]


class Case(BaseModel):
    name: str  # the file's name: the player's, made safe
    exported_at: datetime
    facts: SavedFacts
    practice: Practice


# ---------- Facts in and out ----------


def save_facts(facts: CoachFacts) -> SavedFacts:
    r = facts.replays
    return SavedFacts(
        player_id=facts.player_id,
        username=facts.username,
        scores=dict(facts.scores),
        plays=[
            SavedPlay(
                accuracy=p.accuracy,
                count_miss=p.count_miss,
                max_combo=p.max_combo,
                map_max_combo=p.map_max_combo,
                aim_difficulty=p.aim_difficulty,
                speed_difficulty=p.speed_difficulty,
                speed_note_count=p.speed_note_count,
                ar=p.ar,
                od=p.od,
                mods=list(p.mods),
            )
            for p in facts.plays
        ],
        replays=None
        if r is None
        else SavedReplays(count=r.count, mean_ur=r.mean_ur, miss_rates=r.miss_rates, notes=r.notes),
        maps=list(facts.maps),
    )


def load_facts(saved: SavedFacts) -> CoachFacts:
    r = saved.replays
    return CoachFacts(
        player_id=saved.player_id,
        username=saved.username,
        scores=dict(saved.scores),
        plays=[
            Play(
                p.accuracy,
                p.count_miss,
                p.max_combo,
                p.map_max_combo,
                p.aim_difficulty,
                p.speed_difficulty,
                p.speed_note_count,
                p.ar,
                p.od,
                tuple(p.mods),
            )
            for p in saved.plays
        ],
        replays=None
        if r is None
        else ReplayFacts(count=r.count, mean_ur=r.mean_ur, miss_rates=r.miss_rates, notes=r.notes),
        maps=list(saved.maps),
    )


# ---------- Export from the database ----------


async def practice_snapshot(session: AsyncSession, osu_id: int) -> Practice:
    rows = (
        await session.execute(
            select(Recommendation, Beatmap, MapVector.vector)
            .join(Beatmap, Beatmap.id == Recommendation.beatmap_id)
            .join(MapVector, MapVector.beatmap_id == Beatmap.id, isouter=True)
            .where(Recommendation.player_id == osu_id)
            .order_by(Recommendation.rank)
        )
    ).all()
    played = await played_maps(session, osu_id)
    maps = [
        PracticeMap(
            beatmap_id=b.id,
            beatmapset_id=b.beatmapset_id,
            rank=r.rank,
            star_rating=b.star_rating,
            ar=b.ar,
            od=b.od,
            bpm=b.bpm,
            hit_length_s=b.hit_length_s,
            vector={} if v is None else dict(zip(FIELDS, (float(x) for x in v), strict=True)),
            played=b.id in played,
            reason=r.reason,
        )
        for r, b, v in rows
    ]
    first = rows[0][0] if rows else None
    return Practice(
        target_skill=first.target_skill if first else None,
        comfort_stars=first.comfort_stars if first else None,
        hardest_in_pool=await hardest_map(session),
        maps=maps,
    )


def safe_name(username: str) -> str:
    """A file name from a username: letters, digits, - and _ only."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", username).strip("_") or "player"


async def export_case(session: AsyncSession, osu_id: int) -> Case | None:
    """A case for this player, or None if they aren't scored yet.

    The practice maps are picked again first: the pool changes as maps are fetched, and
    the case must describe the pool its maps were picked from (the hardest map in it
    sets the star range). Picking again commits the new maps to the database.
    """
    await update_recommendations(session, osu_id)
    facts = await gather_facts(session, osu_id)
    if facts is None:
        return None
    return Case(
        name=safe_name(facts.username),
        exported_at=datetime.now(UTC),
        facts=save_facts(facts),
        practice=await practice_snapshot(session, osu_id),
    )


def write_case(case: Case, directory: Path = CASES_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{case.name}.json"
    # "\n" on every system: Windows would otherwise write "\r\n".
    path.write_text(case.model_dump_json(indent=1) + "\n", encoding="utf-8", newline="\n")
    return path


def read_cases(directory: Path = CASES_DIR) -> list[Case]:
    """Every case in the folder, sorted by file name (capitals first, on any system)."""
    return [
        Case.model_validate_json(p.read_text(encoding="utf-8"))
        # By the name as a plain string: Windows paths compare ignoring case, Linux
        # paths don't, and the order must be the same everywhere.
        for p in sorted(directory.glob("*.json"), key=lambda p: p.name)
    ]
