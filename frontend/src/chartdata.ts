// Turning a replay's chart data (GET /replays/{id}/charts) into what each chart draws.
// Pure functions, kept apart from the components so they're easy to test.
import type { ChartNote } from './types'

/** The notes that were hit (300, 100 or 50), each with its timing error. */
export function hits(notes: ChartNote[]): (ChartNote & { error_ms: number })[] {
  return notes.filter(
    (n): n is ChartNote & { error_ms: number } => n.result !== 'miss' && n.error_ms !== null,
  )
}

export interface Bin {
  from: number // ms, inclusive
  to: number // ms, exclusive
  mid: number // the middle, where the bar is drawn
  count: number
}

/**
 * Hits counted by timing error, in bins `width` ms wide from -limit to +limit (limit
 * rounded up to whole bins, so 0 always falls on a bin edge). Errors beyond go in the
 * outermost bins.
 */
export function histogram(notes: ChartNote[], limit: number, width = 2): Bin[] {
  const edge = Math.max(width, Math.ceil(limit / width) * width)
  const n = (2 * edge) / width
  const bins: Bin[] = Array.from({ length: n }, (_, i) => {
    const from = -edge + i * width
    return { from, to: from + width, mid: from + width / 2, count: 0 }
  })
  for (const { error_ms } of hits(notes)) {
    const i = Math.floor((error_ms + edge) / width)
    bins[Math.min(n - 1, Math.max(0, i))].count += 1
  }
  return bins
}

export interface UrPoint {
  time_ms: number // the newest note in the window (the map's clock)
  ur: number // 10 x the spread of the window's timing errors
}

/** Population standard deviation, as osu!'s UR uses. */
function spread(values: number[]): number {
  const mean = values.reduce((a, b) => a + b, 0) / values.length
  return Math.sqrt(values.reduce((a, v) => a + (v - mean) ** 2, 0) / values.length)
}

/**
 * UR over a sliding window of `size` hits, along the map: one point per hit once the
 * window is full. Fewer hits than that: one point over all of them (or none).
 */
export function rollingUr(notes: ChartNote[], size = 50): UrPoint[] {
  const timed = hits(notes).sort((a, b) => a.time_ms - b.time_ms)
  if (timed.length < 2) return []
  const window = Math.min(size, timed.length)
  const points: UrPoint[] = []
  for (let end = window; end <= timed.length; end++) {
    const slice = timed.slice(end - window, end)
    points.push({ time_ms: slice[slice.length - 1].time_ms, ur: spread(slice.map((h) => h.error_ms)) * 10 })
  }
  return points
}

/** 83456 ms -> "1:23", for a time axis. */
export function formatClock(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1000))
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}

/**
 * How strongly to shade a heatmap cell, 0-1. Square-root scaled: a few cells where the
 * cursor rested a long time (spinners, breaks) would otherwise make everything else pale.
 */
export function shade(count: number, max: number): number {
  if (max <= 0 || count <= 0) return 0
  return Math.sqrt(count / max)
}
