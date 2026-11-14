// Unit tests for src/chartdata.ts: turning chart data into what each chart draws.
import { describe, expect, test } from 'vitest'

import { formatClock, histogram, hits, rollingUr, shade } from './chartdata'
import { note } from './test/fixtures'

describe('hits', () => {
  test('only notes hit, with a timing error', () => {
    const notes = [
      note({ idx: 0, result: '300' }),
      note({ idx: 1, result: '100' }),
      note({ idx: 2, result: '50' }),
      note({ idx: 3, result: 'miss', error_ms: null }), // never pressed
      note({ idx: 4, result: 'miss', error_ms: -300 }), // pressed too early: still a miss
    ]

    expect(hits(notes).map((n) => n.idx)).toEqual([0, 1, 2])
  })
})

describe('histogram', () => {
  const at = (...errors: number[]) => errors.map((e, i) => note({ idx: i, error_ms: e }))
  const counts = (limit: number, errors: number[], width = 2) =>
    Object.fromEntries(histogram(at(...errors), limit, width).filter((b) => b.count).map((b) => [b.from, b.count]))

  test('bins cover -limit to +limit in 2 ms steps, edges whole', () => {
    const bins = histogram([], 10)

    expect(bins).toHaveLength(10)
    expect(bins[0]).toEqual({ from: -10, to: -8, mid: -9, count: 0 })
    expect(bins[9]).toEqual({ from: 8, to: 10, mid: 9, count: 0 })
  })

  test("the limit is rounded up to whole bins, so 0 is always a bin's edge", () => {
    const bins = histogram([], 104.5) // gwb's 50 window

    expect(bins[0].from).toBe(-106)
    expect(bins[bins.length - 1].to).toBe(106)
    expect(bins.some((b) => b.from === 0)).toBe(true)
  })

  test.each([
    [[0], { 0: 1 }], // 0 to 2
    [[-0.5], { '-2': 1 }], // just early: the bin before 0
    [[1.99, 2], { 0: 1, 2: 1 }], // a bin includes its start, not its end
    [[-10, 9.9], { '-10': 1, 8: 1 }], // the outermost bins
  ])('errors %j land in their bins', (errors, expected) => {
    expect(counts(10, errors)).toEqual(expected)
  })

  test('errors past the limit go in the outermost bins, not lost', () => {
    expect(counts(10, [-500, 500, 12])).toEqual({ '-10': 1, 8: 2 })
  })

  test('misses are left out', () => {
    const notes = [note({ error_ms: 1 }), note({ idx: 1, result: 'miss', error_ms: null })]

    expect(histogram(notes, 10).reduce((n, b) => n + b.count, 0)).toBe(1)
  })

  test('another bin width', () => {
    const bins = histogram(at(0, 4, 5), 10, 5)

    expect(bins.map((b) => [b.from, b.count])).toEqual([[-10, 0], [-5, 0], [0, 2], [5, 1]])
  })

  test('a limit under one bin still gives a bin each side', () => {
    expect(histogram([], 0.5).map((b) => b.from)).toEqual([-2, 0])
  })
})

describe('rollingUr', () => {
  // Hits at 10 ms apart in time, alternating -e / +e: a spread of e, so UR 10e.
  const alternating = (n: number, e: number) =>
    Array.from({ length: n }, (_, i) => note({ idx: i, time_ms: i * 10, error_ms: i % 2 ? e : -e }))

  test('one point per hit once the window is full, at the newest hit', () => {
    const points = rollingUr(alternating(6, 3), 4)

    expect(points.map((p) => p.time_ms)).toEqual([30, 40, 50])
    expect(points.every((p) => Math.abs(p.ur - 30) < 1e-9)).toBe(true)
  })

  test('a looser section shows as a higher UR', () => {
    const steady = alternating(4, 2)
    const loose = Array.from({ length: 4 }, (_, i) =>
      note({ idx: 4 + i, time_ms: 40 + i * 10, error_ms: i % 2 ? 20 : -20 }),
    )

    const points = rollingUr([...steady, ...loose], 4)

    expect(points[0].ur).toBeCloseTo(20) // all steady
    expect(points[points.length - 1].ur).toBeCloseTo(200) // all loose
  })

  test('the population spread, as osu! uses', () => {
    // -5 and +5: a spread of 5 (n), not 7.07 (n - 1).
    expect(rollingUr(alternating(2, 5), 50)).toEqual([{ time_ms: 10, ur: 50 }])
  })

  test('fewer hits than the window: one point over all of them', () => {
    expect(rollingUr(alternating(3, 4), 50)).toHaveLength(1)
  })

  test('under two hits: nothing to show', () => {
    expect(rollingUr([note()], 50)).toEqual([])
    expect(rollingUr([], 50)).toEqual([])
  })

  test('misses are skipped, and hits are taken in time order', () => {
    const notes = [
      note({ idx: 2, time_ms: 30, error_ms: 4 }),
      note({ idx: 0, time_ms: 10, error_ms: -4 }),
      note({ idx: 1, time_ms: 20, result: 'miss', error_ms: null }),
    ]

    expect(rollingUr(notes, 2)).toEqual([{ time_ms: 30, ur: 40 }])
  })
})

test.each([
  [0, '0:00'],
  [999, '0:00'],
  [1000, '0:01'],
  [83_456, '1:23'],
  [183_470, '3:03'], // gwb's replay's last frame
  [600_000, '10:00'],
  [-500, '0:00'], // lazer starts a little before the song
])('formatClock %i -> %s', (ms, text) => {
  expect(formatClock(ms)).toBe(text)
})

describe('shade', () => {
  test.each([
    [0, 100, 0],
    [100, 100, 1], // the busiest cell is fully shaded
    [25, 100, 0.5], // square root: a quarter of the time is half the shade
    [1, 100, 0.1],
    [5, 0, 0], // nothing counted at all
  ])('%i of %i -> %f', (count, max, value) => {
    expect(shade(count, max)).toBeCloseTo(value, 10)
  })
})
