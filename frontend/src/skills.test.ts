// Unit tests for src/skills.ts: the skill profile's data, ready to show.
import { describe, expect, test } from 'vitest'

import { SKILL_LABELS, SKILL_ORDER, playsNote, radarPoints } from './skills'
import { skills } from './test/fixtures'

describe('the skills', () => {
  test('the radar goes round all six, each once', () => {
    expect([...SKILL_ORDER].sort()).toEqual(['accuracy', 'aim', 'consistency', 'reading', 'speed', 'stamina'])
  })

  test('aim is at the top, and the related skills sit side by side', () => {
    expect(SKILL_ORDER[0]).toBe('aim')
    // Neighbours round the circle (the last is next to the first).
    const next = (a: string) => SKILL_ORDER[(SKILL_ORDER.indexOf(a as never) + 1) % SKILL_ORDER.length]
    expect(next('speed')).toBe('stamina')
    expect(next('accuracy')).toBe('consistency')
  })

  test('every skill has a label', () => {
    expect(Object.keys(SKILL_LABELS).sort()).toEqual([...SKILL_ORDER].sort())
    for (const name of SKILL_ORDER) expect(SKILL_LABELS[name]).toMatch(/^[A-Z][a-z]+$/)
  })
})

describe('radarPoints', () => {
  test('one point per skill, in the radar order, with its score', () => {
    expect(radarPoints(skills())).toEqual([
      { skill: 'Aim', score: 41.1 },
      { skill: 'Speed', score: 32.9 },
      { skill: 'Stamina', score: 38.3 },
      { skill: 'Accuracy', score: 69.4 },
      { skill: 'Consistency', score: 80.9 },
      { skill: 'Reading', score: 8 },
    ])
  })

  test.each([
    // Edge: the ends of the scale are kept as they are.
    ['all zeros (a player without plays)', 0],
    ['all 100', 100],
  ])('%s', (_name, value) => {
    const all = skills({ aim: value, speed: value, accuracy: value, consistency: value, stamina: value, reading: value })

    expect(radarPoints(all).map((p) => p.score)).toEqual([value, value, value, value, value, value])
  })

  test('only the scores matter: the other fields are left out', () => {
    expect(Object.keys(radarPoints(skills())[0]).sort()).toEqual(['score', 'skill'])
  })
})

describe('playsNote', () => {
  test.each([
    [50, 0, 'from 50 plays'],
    [1, 0, 'from 1 play'], // edge: one play, not "1 plays"
    [0, 0, 'from 0 plays'], // edge: none scored yet
    [58, 3, 'from 58 plays (3 not scored yet)'],
    [1, 1, 'from 1 play (1 not scored yet)'],
    [0, 4, 'from 0 plays (4 not scored yet)'], // edge: every play waiting for attributes
  ])('%i used, %i skipped: "%s"', (used, skipped, note) => {
    expect(playsNote(skills({ plays_used: used, plays_skipped: skipped }))).toBe(note)
  })
})
