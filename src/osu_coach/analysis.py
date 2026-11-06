"""Analyzing a replay: judge it against its map, then store its hit-by-hit results and
summary (UR, timing bias, aim).

Timing stats are in real time: with DT the song runs 1.5x as fast, so an error of 15 ms
of song time is 10 ms of the player's time. That's how osu! shows UR for speed mods.
"""

import asyncio
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach.difficulty import HitResult, play_conditions
from osu_coach.judge import Judgement, counts, judge
from osu_coach.models import BeatmapObject, Replay, ReplayHit
from osu_coach.osrfile import Frame
from osu_coach.osu.files import BeatmapFiles
from osu_coach.osufile import parse_osu
from osu_coach.replays import load_frames, stored_map

INSERT_BATCH = 1000  # rows per INSERT; a long map has a few thousand objects


class ReplayNotFoundError(LookupError):
    """No stored replay has this id."""


class MapNotStoredError(LookupError):
    """The replay's map (that exact version, with its hit objects) isn't stored yet."""


@dataclass(frozen=True)
class ReplayStats:
    counts: dict[HitResult, int]
    # From the hits only (300s, 100s, 50s): a miss has no timing to measure. None with
    # fewer than two hits, where a spread means nothing.
    unstable_rate: float | None  # 10 x the standard deviation of the timing errors
    mean_error_ms: float | None  # negative: early on average
    mean_aim_error_px: float | None  # cursor to circle center, osu! pixels


def replay_stats(judgements: Sequence[Judgement], rate: float = 1.0) -> ReplayStats:
    """Summarize a judged replay. `rate` is the song speed (1.5 with DT, 0.75 with HT)."""
    hits = [j for j in judgements if j.result != "miss" and j.error_ms is not None]
    errors = [j.error_ms / rate for j in hits if j.error_ms is not None]
    aims = [j.distance_px for j in hits if j.distance_px is not None]
    return ReplayStats(
        counts=counts(judgements),
        # pstdev: the spread of these hits themselves, as osu! computes UR.
        unstable_rate=statistics.pstdev(errors) * 10 if len(errors) >= 2 else None,
        mean_error_ms=statistics.fmean(errors) if len(errors) >= 2 else None,
        mean_aim_error_px=statistics.fmean(aims) if len(aims) >= 2 else None,
    )


async def store_analysis(
    session: AsyncSession,
    replay_id: int,
    beatmap_id: int,
    judgements: Sequence[Judgement],
    stats: ReplayStats,
) -> None:
    """Replace a replay's stored results and summary with these, in one transaction.

    Running it again gives the same rows: a replay analyzed again (say, after the
    judging improves) never keeps results from the earlier run.
    """
    await session.execute(delete(ReplayHit).where(ReplayHit.replay_id == replay_id))
    rows = [
        {
            "replay_id": replay_id,
            "idx": j.idx,
            "result": j.result,
            "hit_ms": j.hit_ms,
            "error_ms": j.error_ms,
            "aim_error_px": j.distance_px,
            "forced": j.forced,
        }
        for j in judgements
    ]
    for start in range(0, len(rows), INSERT_BATCH):
        await session.execute(insert(ReplayHit), rows[start : start + INSERT_BATCH])
    await session.execute(
        update(Replay)
        .where(Replay.id == replay_id)
        .values(
            beatmap_id=beatmap_id,
            unstable_rate=stats.unstable_rate,
            mean_error_ms=stats.mean_error_ms,
            mean_aim_error_px=stats.mean_aim_error_px,
            analyzed_at=func.now(),
        )
    )
    await session.commit()


async def judged_counts(session: AsyncSession, replay_id: int) -> dict[str, int] | None:
    """Our 300/100/50/miss counts for an analyzed replay, from its stored results.

    None if it has no stored results (not analyzed yet).
    """
    stmt = (
        select(ReplayHit.result, func.count())
        .where(ReplayHit.replay_id == replay_id)
        .group_by(ReplayHit.result)
    )
    found = {result: count for result, count in await session.execute(stmt)}
    if not found:
        return None
    return {r: found.get(r, 0) for r in ("300", "100", "50", "miss")}


async def results_by_pattern(session: AsyncSession, replay_id: int) -> dict[str, dict[str, int]]:
    """How each pattern (jump, stream, slider...) went: {pattern: {result: count}}.

    Joins the stored results with the map's hit objects, through the map the replay was
    analyzed against. Empty for a replay not analyzed yet.
    """
    stmt = (
        select(BeatmapObject.pattern, ReplayHit.result, func.count())
        .join(Replay, Replay.id == ReplayHit.replay_id)
        .join(
            BeatmapObject,
            (BeatmapObject.beatmap_id == Replay.beatmap_id) & (BeatmapObject.idx == ReplayHit.idx),
        )
        .where(ReplayHit.replay_id == replay_id)
        .group_by(BeatmapObject.pattern, ReplayHit.result)
    )
    by_pattern: dict[str, dict[str, int]] = {}
    for pattern, result, count in await session.execute(stmt):
        by_pattern.setdefault(pattern, {"300": 0, "100": 0, "50": 0, "miss": 0})[result] = count
    return by_pattern


async def analyze_replay(
    session: AsyncSession, files: BeatmapFiles, data_dir: Path, replay_id: int
) -> dict[str, Any]:
    """Judge a stored replay against its stored map, and store the results and summary.

    Returns a small JSON summary. Raises ReplayNotFoundError, MapNotStoredError (fetch the
    map first: see osu_coach.replays.fetch_replay_map), or UnsupportedModsError (from
    osu_coach.difficulty) for mods the judging doesn't model. Analyzing again replaces the
    earlier results.
    """
    replay = await session.get(Replay, replay_id)
    if replay is None:
        raise ReplayNotFoundError(f"no replay {replay_id}")
    beatmap_id = await stored_map(session, replay.beatmap_checksum)
    if beatmap_id is None:
        raise MapNotStoredError(f"replay {replay_id}'s map isn't stored yet")

    # The .osu file is on disk from when the map was fetched; get() checks it against the
    # checksum, and downloads it again only if it's missing or doesn't match.
    map_path = await files.get(beatmap_id, replay.beatmap_checksum)
    text = await asyncio.to_thread(map_path.read_text, encoding="utf-8")
    osu = parse_osu(text)
    table = await asyncio.to_thread(load_frames, data_dir, replay.frames_path)
    frames = [Frame(*row) for row in table.iter_rows()]

    conditions = play_conditions(osu.difficulty, replay.mods)
    judgements = judge(osu, frames, conditions)
    stats = replay_stats(judgements, conditions.rate)
    # Read before storing: the commit expires the loaded row, and reloading it here
    # would need another query.
    official = {
        "300": replay.count_300,
        "100": replay.count_100,
        "50": replay.count_50,
        "miss": replay.count_miss,
    }
    await store_analysis(session, replay_id, beatmap_id, judgements, stats)

    return {
        "replay_id": replay_id,
        "beatmap_id": beatmap_id,
        "counts": stats.counts,
        # Whether our judging agrees with osu!'s own counts in the file: the check that
        # the analysis is right (it can't be yet for maps with spinners).
        "matches_official": stats.counts == official,
        "unstable_rate": stats.unstable_rate,
    }
