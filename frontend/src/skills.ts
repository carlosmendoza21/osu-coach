// The skill profile's data, ready to show: pure functions, no React.
import type { SkillName, Skills } from './types'

// Round the radar from the top, clockwise: the two tapping skills (speed, stamina) and the
// two precision skills (accuracy, consistency) side by side.
export const SKILL_ORDER: readonly SkillName[] = ['aim', 'speed', 'stamina', 'accuracy', 'consistency', 'reading']

export const SKILL_LABELS: Record<SkillName, string> = {
  aim: 'Aim',
  speed: 'Speed',
  stamina: 'Stamina',
  accuracy: 'Accuracy',
  consistency: 'Consistency',
  reading: 'Reading',
}

export interface RadarPoint {
  skill: string // its label
  score: number // 0-100
}

/** The radar's points, in SKILL_ORDER. */
export function radarPoints(skills: Skills): RadarPoint[] {
  return SKILL_ORDER.map((name) => ({ skill: SKILL_LABELS[name], score: skills[name] }))
}

/** "from 58 plays", or "from 1 play (3 not scored yet)". */
export function playsNote(skills: Skills): string {
  const used = `from ${skills.plays_used} ${skills.plays_used === 1 ? 'play' : 'plays'}`
  return skills.plays_skipped > 0 ? `${used} (${skills.plays_skipped} not scored yet)` : used
}
