// Live tests: src/api.ts against the real API, through a real Vite dev server started
// with our vite.config.ts, so its /api proxy is what's tested too.
//
// Run with:  npm run test:live
// Needs:     the API on localhost:8000 (docker compose up -d), with osu! credentials for
//            the player test. Skips if the API isn't answering. The replay test uploads
//            the real replays in ../data/replays/ if there are any.
import { readdirSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { type ViteDevServer, createServer } from 'vite'
import { afterAll, beforeAll, describe, expect, test, vi } from 'vitest'

import {
  ApiError,
  getHealth,
  getPlayer,
  getRecommendations,
  getReplay,
  getReplayCharts,
  getSkills,
  uploadReplay,
} from '../src/api'
import type {
  Beatmap,
  ChartNote,
  Player,
  Recommendation,
  RecommendedMap,
  Recommendations,
  Replay,
  ReplayCharts,
  ReplayDetail,
  Score,
  ScoreMap,
  Skills,
  UploadResponse,
} from '../src/types'

const API = 'http://localhost:8000'
const apiUp = await fetch(`${API}/health`).then((r) => r.ok, () => false)

// Every field of each type, as a checked list: `satisfies` makes TypeScript refuse a
// missing or extra name, so these always match src/types.ts. The tests then check the
// real responses have exactly these fields: no more, no fewer.
const PLAYER = { osu_id: 1, username: 1, country: 1, global_rank: 1, pp: 1, playcount: 1,
  accuracy: 1, updated_at: 1 } satisfies Record<keyof Player, 1>
const SCORE = { id: 1, beatmap_id: 1, score: 1, max_combo: 1, count_300: 1, count_100: 1,
  count_50: 1, count_miss: 1, rank: 1, pp: 1, mods: 1, date: 1, accuracy: 1,
  passed: 1, beatmap: 1 } satisfies Record<keyof Score, 1>
const SCORE_MAP = { id: 1, beatmapset_id: 1, artist: 1, title: 1, version: 1,
  star_rating: 1 } satisfies Record<keyof ScoreMap, 1>
const REPLAY = { id: 1, beatmap_checksum: 1, score_id: 1, player_name: 1, mods: 1, lazer: 1,
  count_300: 1, count_100: 1, count_50: 1, count_miss: 1, score: 1, max_combo: 1, perfect: 1,
  played_at: 1, frame_count: 1, parsed_at: 1, unstable_rate: 1, mean_error_ms: 1,
  mean_aim_error_px: 1, analyzed_at: 1 } satisfies Record<keyof Replay, 1>
const BEATMAP = { id: 1, beatmapset_id: 1, artist: 1, title: 1, creator: 1, version: 1,
  star_rating: 1, ar: 1, od: 1, cs: 1, hp: 1, bpm: 1,
  length_s: 1 } satisfies Record<keyof Beatmap, 1>
const UPLOAD = { replay: 1, created: 1, map_job_id: 1,
  analysis_job_id: 1 } satisfies Record<keyof UploadResponse, 1>
const CHARTS = { rate: 1, windows: 1, notes: 1, cursor: 1 } satisfies Record<keyof ReplayCharts, 1>
const NOTE = { idx: 1, time_ms: 1, result: 1, error_ms: 1, aim_error_px: 1, x: 1, y: 1,
  pattern: 1 } satisfies Record<keyof ChartNote, 1>
const DETAIL = { replay: 1, beatmap: 1, status: 1, judged: 1, matches_official: 1,
  by_pattern: 1 } satisfies Record<keyof ReplayDetail, 1>
const RECS = { player_id: 1, target_skill: 1, comfort_stars: 1, created_at: 1,
  maps: 1 } satisfies Record<keyof Recommendations, 1>
const REC = { rank: 1, score: 1, reason: 1, beatmap: 1 } satisfies Record<keyof Recommendation, 1>
const REC_MAP = { id: 1, beatmapset_id: 1, artist: 1, title: 1, version: 1, creator: 1,
  star_rating: 1, bpm: 1, ar: 1, od: 1, hit_length_s: 1 } satisfies Record<keyof RecommendedMap, 1>
const SKILLS = { player_id: 1, aim: 1, speed: 1, accuracy: 1, consistency: 1, stamina: 1,
  reading: 1, strongest: 1, weakest: 1, plays_used: 1, plays_skipped: 1,
  computed_at: 1 } satisfies Record<keyof Skills, 1>

function fields(value: object): string[] {
  return Object.keys(value).sort()
}
function expected(list: object): string[] {
  return Object.keys(list).sort()
}

const REPLAYS = resolve(import.meta.dirname, '../../data/replays')
const realReplays = (() => {
  try {
    return readdirSync(REPLAYS).filter((f) => f.endsWith('.osr'))
  } catch {
    return []
  }
})()

describe.skipIf(!apiUp)('through the dev server, against the real API', () => {
  let server: ViteDevServer

  beforeAll(async () => {
    server = await createServer({
      configFile: resolve(import.meta.dirname, '../vite.config.ts'),
      server: { port: 0 }, // any free port
      logLevel: 'silent',
    })
    await server.listen()
    const base = server.resolvedUrls?.local[0]
    if (!base) throw new Error('the dev server has no address')
    // src/api.ts asks for "/api/..." as a page would; send that to the dev server.
    const realFetch = globalThis.fetch
    vi.stubGlobal('fetch', (url: string, init?: RequestInit) => realFetch(new URL(url, base), init))
  })

  afterAll(async () => {
    vi.unstubAllGlobals()
    await server?.close()
  })

  test('the proxy reaches the API', async () => {
    expect(await getHealth()).toEqual({ status: 'ok' })
  })

  test("an unknown replay is the API's 404, with its reason", async () => {
    const error = await getReplay(999_999_999).catch((e: unknown) => e)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(404)
    expect((error as ApiError).message).toBe('replay 999999999 not found')
  })

  test('an upload reaches the parser (the form field name is right)', async () => {
    // A wrong field name would be "Field required"; this is the parser's own reason.
    const error = await uploadReplay(new File(['not a replay'], 'bad.osr')).catch((e: unknown) => e)

    expect((error as ApiError).status).toBe(422)
    expect((error as ApiError).message).toBe('not a valid .osr file (cut short or damaged)')
  })

  test('a player has exactly the fields src/types.ts declares', async () => {
    const response = await getPlayer('peppy')

    expect(fields(response)).toEqual(['best', 'player', 'recent'])
    expect(fields(response.player)).toEqual(expected(PLAYER))
    expect(response.best.length).toBeGreaterThan(0)
    for (const score of [...response.best, ...response.recent]) {
      expect(fields(score)).toEqual(expected(SCORE))
      // The sync stores every play's map, so each one comes with it.
      expect(score.beatmap).not.toBeNull()
      expect(fields(score.beatmap ?? {})).toEqual(expected(SCORE_MAP))
      expect(score.beatmap?.id).toBe(score.beatmap_id)
    }
  })

  test("a player's skills have exactly the fields src/types.ts declares", async () => {
    // peppy is synced above; the first scoring follows the attributes fetch.
    const { player } = await getPlayer('peppy')
    let skills: Skills | undefined
    for (let tries = 0; skills === undefined; tries++) {
      try {
        skills = await getSkills(player.osu_id)
      } catch (e) {
        if (!(e instanceof ApiError && e.status === 409) || tries >= 60) throw e
        await new Promise((r) => setTimeout(r, 1000))
      }
    }

    expect(fields(skills)).toEqual(expected(SKILLS))
    expect(skills.player_id).toBe(player.osu_id)
    const names = ['aim', 'speed', 'accuracy', 'consistency', 'stamina', 'reading'] as const
    for (const name of names) expect(skills[name]).toBeGreaterThanOrEqual(0)
    expect(names).toContain(skills.strongest)
    expect(names).toContain(skills.weakest)
  }, 90_000)

  test("a player's practice maps have exactly the fields src/types.ts declares", async () => {
    // peppy is synced above; the practice maps follow the scoring.
    const { player } = await getPlayer('peppy')
    // Not picked yet is a 409, or (for a player scored before they existed) none made.
    let recs: Recommendations | undefined
    for (let tries = 0; recs === undefined; tries++) {
      try {
        const answer = await getRecommendations(player.osu_id)
        if (answer.target_skill !== null) recs = answer
      } catch (e) {
        if (!(e instanceof ApiError && e.status === 409)) throw e
      }
      if (recs === undefined) {
        if (tries >= 60) throw new Error("peppy's practice maps were never picked")
        await new Promise((r) => setTimeout(r, 1000))
      }
    }

    expect(fields(recs)).toEqual(expected(RECS))
    expect(recs.player_id).toBe(player.osu_id)
    expect(recs.maps.length).toBeGreaterThan(0)
    for (const rec of recs.maps) {
      expect(fields(rec)).toEqual(expected(REC))
      expect(fields(rec.beatmap)).toEqual(expected(REC_MAP))
    }
    expect(recs.maps.map((m) => m.rank)).toEqual(recs.maps.map((_, i) => i + 1))
  }, 90_000)

  test("an unknown player's skills are the API's 404, with its reason", async () => {
    const error = await getSkills(999_999_999).catch((e: unknown) => e)

    expect((error as ApiError).status).toBe(404)
    expect((error as ApiError).message).toBe('player 999999999 not found')
  })

  test.skipIf(realReplays.length === 0)(
    'an uploaded replay and its detail have exactly the declared fields',
    async () => {
      const bytes = readFileSync(resolve(REPLAYS, realReplays[0]))
      const upload = await uploadReplay(new File([bytes], realReplays[0]))

      expect(fields(upload)).toEqual(expected(UPLOAD))
      expect(fields(upload.replay)).toEqual(expected(REPLAY))

      // Checked again until analyzed, as the replay page does (a few seconds at most).
      let detail = await getReplay(upload.replay.id)
      for (let i = 0; detail.status === 'pending' && i < 30; i++) {
        await new Promise((r) => setTimeout(r, 2000))
        detail = await getReplay(upload.replay.id)
      }
      expect(detail.status).toBe('analyzed')
      expect(fields(detail)).toEqual(expected(DETAIL))
      for (const counts of Object.values(detail.by_pattern)) {
        expect(fields(counts)).toEqual(['100', '300', '50', 'miss'])
      }
      expect(fields(detail.replay)).toEqual(expected(REPLAY))
      if (detail.beatmap) expect(fields(detail.beatmap)).toEqual(expected(BEATMAP))
      expect(fields(detail.judged ?? {})).toEqual(['100', '300', '50', 'miss'])
      expect(detail.matches_official).toBe(true) // our counts equal osu!'s

      // Its charts: the declared fields, and the same notes the analysis counted.
      const charts = await getReplayCharts(upload.replay.id)
      expect(fields(charts)).toEqual(expected(CHARTS))
      expect(fields(charts.windows)).toEqual(['great', 'meh', 'ok'])
      expect(fields(charts.cursor)).toEqual(['cols', 'counts', 'off_playfield', 'rows'])
      for (const n of charts.notes) expect(fields(n)).toEqual(expected(NOTE))
      const judged = detail.judged ?? { '300': 0, '100': 0, '50': 0, miss: 0 }
      expect(charts.notes).toHaveLength(judged['300'] + judged['100'] + judged['50'] + judged.miss)
      expect(charts.notes.filter((n) => n.result === 'miss')).toHaveLength(judged.miss)
      expect(charts.cursor.counts).toHaveLength(charts.cursor.cols * charts.cursor.rows)
    },
  )
})
