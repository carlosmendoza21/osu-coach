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

// The map a score was set on.
export interface ScoreMap {
  id: number
  beatmapset_id: number
  artist: string
  title: string
  version: string // the difficulty name, e.g. "Hard"
  star_rating: number
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
  beatmap: ScoreMap | null // null only if the map isn't stored
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

// GET /replays/{id}/charts
export interface ChartNote {
  idx: number
  time_ms: number // song time: the map's clock
  result: HitResult
  error_ms: number | null // real time; negative is early; null if never pressed
  aim_error_px: number | null
  x: number // as the player saw it (flipped with HR)
  y: number
  pattern: string
}

export interface ReplayCharts {
  rate: number // 1.5 with DT, 0.75 with HT
  windows: { great: number; ok: number; meh: number } // +- ms of real time
  notes: ChartNote[]
  cursor: { cols: number; rows: number; counts: number[]; off_playfield: number }
}

// GET /players/{osu_id}/skills
export type SkillName = 'aim' | 'speed' | 'accuracy' | 'consistency' | 'stamina' | 'reading'

export interface Skills extends Record<SkillName, number> {
  // each skill above is 0-100
  player_id: number
  strongest: SkillName
  weakest: SkillName
  plays_used: number // the plays it was scored from
  plays_skipped: number // plays without attributes yet, or with Relax and the like
  computed_at: string // ISO time
}

// GET /players/{osu_id}/recommendations
export interface RecommendedMap {
  id: number
  beatmapset_id: number
  artist: string
  title: string
  version: string // the difficulty name
  creator: string // the mapper
  star_rating: number
  bpm: number
  ar: number
  od: number
  hit_length_s: number // seconds of play
}

export interface Recommendation {
  rank: number // 1 is the best fit
  score: number // 0-1: how close the map is to the player's style
  reason: string // e.g. "AR 9; 4.3★, 0.6★ above your comfort level (3.7★)"
  beatmap: RecommendedMap
}

export interface Recommendations {
  player_id: number
  target_skill: SkillName | null // the weakest skill; null until recommendations are made
  comfort_stars: number | null // the level they're set from
  created_at: string | null // ISO time
  maps: Recommendation[] // best first; empty until made (or if none fit)
}

// POST /players/{osu_id}/report
export interface SkillNote {
  skill: SkillName
  score: number // 0-100
  evidence: string[] // sentences with real numbers
  explanation: string | null // the model's words on it; null in a template report
}

export interface PlanStep {
  skill: SkillName
  text: string
}

export interface ReportMap {
  beatmap_id: number
  name: string // "Artist - Title [Difficulty]"
  star_rating: number
  reason: string
}

export interface CoachReport {
  player_id: number
  username: string
  source: 'template' | 'llm' // who wrote the words
  summary: string
  strengths: SkillNote[]
  weaknesses: SkillNote[] // weakest first
  plan: PlanStep[]
  maps: ReportMap[]
  model: string | null // e.g. "ollama:qwen3:8b"; null when no model was tried
  tools_used: string[]
  fallback_reason: string | null // why a model's words weren't used
}

// The report as a stream (server-sent events): steps while the coach works, then the report.
export type CoachEvent =
  | { type: 'step'; text: string }
  | { type: 'report'; report: CoachReport }
  | { type: 'error'; detail: string }
