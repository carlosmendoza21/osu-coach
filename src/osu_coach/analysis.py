"""A judged replay's summary (UR, timing bias, aim), stored with its hit-by-hit results.

Timing stats are in real time: with DT the song runs 1.5x as fast, so an error of 15 ms
of song time is 10 ms of the player's time. That's how osu! shows UR for speed mods.
"""

import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach.difficulty import HitResult
from osu_coach.judge import Judgement, counts
from osu_coach.models import BeatmapObject, Replay, ReplayHit

INSERT_BATCH = 1000  # rows per INSERT; a long map has a few thousand objects


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
