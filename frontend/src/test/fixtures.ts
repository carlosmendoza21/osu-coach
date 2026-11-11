// Sample API data for tests, shaped like the real responses (src/types.ts).
import type { PlayerResponse, Score } from '../types'

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
