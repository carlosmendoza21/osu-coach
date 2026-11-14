// Sample API data for tests, shaped like the real responses (src/types.ts).
import type { ChartNote, PlayerResponse, ReplayCharts, ReplayDetail, Score } from '../types'

export function score(changes: Partial<Score> = {}): Score {
  return {
    id: 1,
    beatmap_id: 22423,
    score: 1_234_567,
    max_combo: 450,
    count_300: 254,
    count_100: 0,
    count_50: 0,
    count_miss: 0,
    rank: 'X',
    pp: 73.2,
    mods: ['PF'],
    date: '2014-03-24T12:00:00Z',
    accuracy: 1,
    passed: true,
    beatmap: {
      id: 22423,
      beatmapset_id: 3720,
      artist: 'Lix',
      title: 'Tori no Uta -Ethereal House Mix-',
      version: 'Hard',
      star_rating: 3.56467,
    },
    ...changes,
  }
}

export function playerResponse(changes: Partial<PlayerResponse['player']> = {}): PlayerResponse {
  return {
    player: {
      osu_id: 2,
      username: 'peppy',
      country: 'AU',
      global_rank: 817157,
      pp: 1184.4,
      playcount: 7816,
      accuracy: 96.8512,
      updated_at: new Date(Date.now() - 2 * 60_000).toISOString(), // 2 minutes ago
      ...changes,
    },
    best: [
      score(),
      score({ id: 2, rank: 'S', pp: 56.1, accuracy: 0.9848, mods: [], count_100: 11 }),
    ],
    recent: [score({ id: 3, rank: 'F', passed: false, pp: null, count_miss: 12 })],
  }
}

export function replayDetail(changes: Partial<ReplayDetail> = {}): ReplayDetail {
  return {
    replay: {
      id: 2,
      beatmap_checksum: '345dd69cceb62b0d952fcc3e90000225',
      score_id: 7383778441,
      player_name: 'mrekk',
      mods: [],
      lazer: true,
      count_300: 2224,
      count_100: 14,
      count_50: 0,
      count_miss: 0,
      score: 988492,
      max_combo: 2666,
      perfect: true,
      played_at: '2026-08-31T14:05:51Z',
      frame_count: 18098,
      parsed_at: '2026-11-04T04:26:07Z',
      unstable_rate: 66.879,
      mean_error_ms: -2.24,
      mean_aim_error_px: 11.31,
      analyzed_at: '2026-11-04T04:26:09Z',
    },
    beatmap: {
      id: 4904540,
      beatmapset_id: 2296316,
      artist: 'xi',
      title: 'FREEDOM DiVE',
      creator: 'mapper',
      version: 'Arles',
      star_rating: 9.68,
      ar: 10,
      od: 10,
      cs: 4,
      hp: 5,
      bpm: 222.22,
      length_s: 258,
    },
    status: 'analyzed',
    judged: { '300': 2224, '100': 14, '50': 0, miss: 0 },
    matches_official: true,
    by_pattern: {
      other: { '300': 187, '100': 3, '50': 0, miss: 0 },
      stream: { '300': 1438, '100': 5, '50': 0, miss: 0 },
      jump: { '300': 263, '100': 0, '50': 0, miss: 0 },
      slider: { '300': 336, '100': 6, '50': 0, miss: 0 },
    },
    ...changes,
  }
}

/** The same replay before its analysis: no stats, counts or patterns yet. */
export function pendingDetail(withMap = true): ReplayDetail {
  const d = replayDetail()
  return {
    ...d,
    replay: { ...d.replay, unstable_rate: null, mean_error_ms: null, mean_aim_error_px: null, analyzed_at: null },
    beatmap: withMap ? d.beatmap : null,
    status: 'pending',
    judged: null,
    matches_official: null,
    by_pattern: {},
  }
}

export function note(changes: Partial<ChartNote> = {}): ChartNote {
  return {
    idx: 0,
    time_ms: 1000,
    result: '300',
    error_ms: 0,
    aim_error_px: 5,
    x: 256,
    y: 192,
    pattern: 'stream',
    ...changes,
  }
}

/** Chart data like gwb's Split EX: OD 9.5, a few 100s, a 50 and misses. */
export function chartsData(changes: Partial<ReplayCharts> = {}): ReplayCharts {
  const cols = 4
  const rows = 3
  return {
    rate: 1,
    windows: { great: 22.5, ok: 63.5, meh: 104.5 },
    notes: [
      note({ idx: 0, time_ms: 1000, error_ms: -2 }),
      note({ idx: 1, time_ms: 1200, error_ms: 3 }),
      note({ idx: 2, time_ms: 1400, result: '100', error_ms: -40, x: 100, y: 100 }),
      note({ idx: 3, time_ms: 1600, result: '50', error_ms: 80, x: 400, y: 50 }),
      note({ idx: 4, time_ms: 1800, result: 'miss', error_ms: null, aim_error_px: null, x: 50, y: 300 }),
      note({ idx: 5, time_ms: 2000, error_ms: 1 }),
    ],
    cursor: { cols, rows, counts: [0, 1, 4, 0, 9, 16, 0, 0, 0, 0, 0, 1], off_playfield: 3 },
    ...changes,
  }
}
