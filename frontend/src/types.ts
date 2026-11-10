// The API's responses, as the backend sends them (see src/osu_coach/api/ in the repo).
// Dates arrive as ISO 8601 strings; null where the backend's field is optional.

export type HitResult = '300' | '100' | '50' | 'miss'
export type HitCounts = Record<HitResult, number>

// ---------- GET /players/{username} ----------

export interface Player {
  osu_id: number
  username: string
  country: string
  global_rank: number | null // null for inactive players
  pp: number
  playcount: number
  accuracy: number // a percent, e.g. 96.85
  updated_at: string // when it was last synced from osu!
}

export interface Score {
  id: number
  beatmap_id: number
  score: number
  max_combo: number
  count_300: number
  count_100: number
  count_50: number
  count_miss: number
  rank: string // "X", "S", "A"...
  pp: number | null // null for failed plays and unranked maps
  mods: string[]
  date: string
  accuracy: number // 0-1
  passed: boolean
}

export interface PlayerResponse {
  player: Player
  best: Score[] // top plays by pp
  recent: Score[] // latest plays, failed ones included
}

// ---------- Replays ----------

export interface Replay {
  id: number
  beatmap_checksum: string
  score_id: number | null
  player_name: string
  mods: string[]
  lazer: boolean
  count_300: number
  count_100: number
  count_50: number
  count_miss: number
  score: number
  max_combo: number
  perfect: boolean
  played_at: string
  frame_count: number
  parsed_at: string
  // Null until the replay is analyzed.
  unstable_rate: number | null
  mean_error_ms: number | null // negative: early on average
  mean_aim_error_px: number | null
  analyzed_at: string | null
}

export interface Beatmap {
  id: number
  beatmapset_id: number
  artist: string
  title: string
  creator: string
  version: string // the difficulty name, e.g. "Hard"
  star_rating: number
  ar: number
  od: number
  cs: number
  hp: number
  bpm: number
  length_s: number
}

// POST /replays/upload
export interface UploadResponse {
  replay: Replay
  created: boolean // false if this exact file was uploaded before
  map_job_id: string | null
  analysis_job_id: string | null
}

// GET /replays/{id}
export interface ReplayDetail {
  replay: Replay
  beatmap: Beatmap | null // null until the map is stored
  status: 'pending' | 'analyzed'
  judged: HitCounts | null // our counts; null while pending
  matches_official: boolean | null
  by_pattern: Record<string, HitCounts> // {"stream": {"300": 532, ...}}
}
