// Unit tests for src/format.ts: how numbers and dates read on the page.
import { describe, expect, test } from 'vitest'

import {
  formatAccuracy,
  formatCount,
  formatDate,
  formatMods,
  formatPercent,
  formatPp,
  formatRank,
  formatStars,
  timeAgo,
} from './format'

describe('formatPp', () => {
  test.each([
    [1184.4, '1,184pp'],
    [1184.5, '1,185pp'], // rounded, not cut
    [0, '0pp'],
    [12345.6, '12,346pp'], // thousands separated
    [null, '-'], // failed plays and unranked maps have no pp
  ])('%s -> %s', (pp, text) => {
    expect(formatPp(pp)).toBe(text)
  })
})

describe('accuracy', () => {
  test.each([
    [1, '100.00%'],
    [0.9848, '98.48%'],
    [0.98765, '98.77%'], // two decimals, rounded
    [0, '0.00%'],
  ])('a score fraction %s -> %s', (fraction, text) => {
    expect(formatAccuracy(fraction)).toBe(text)
  })

  test.each([
    [96.8512, '96.85%'],
    [100, '100.00%'],
  ])('a player percent %s -> %s', (percent, text) => {
    // A player's accuracy already comes as a percent: not multiplied again.
    expect(formatPercent(percent)).toBe(text)
  })
})

describe('formatRank', () => {
  test.each([
    [1, '#1'],
    [817157, '#817,157'],
    [null, 'unranked'], // inactive players have no rank
  ])('%s -> %s', (rank, text) => {
    expect(formatRank(rank)).toBe(text)
  })
})

describe('formatMods', () => {
  test.each([
    [[], 'NM'],
    [['HD'], '+HD'],
    [['HD', 'DT'], '+HDDT'], // in the order osu! gives them
    [['PF'], '+PF'],
  ])('%j -> %s', (mods, text) => {
    expect(formatMods(mods)).toBe(text)
  })
})

test.each([
  [7816, '7,816'],
  [0, '0'],
  [1234567, '1,234,567'],
])('formatCount %s -> %s', (n, text) => {
  expect(formatCount(n)).toBe(text)
})

test.each([
  [3.56467, '3.56★'],
  [10, '10.00★'],
  [3.565, '3.56★'], // binary floats: 3.565 is a hair under, as toFixed sees it
])('formatStars %s -> %s', (stars, text) => {
  expect(formatStars(stars)).toBe(text)
})

test('formatDate gives month, day and year', () => {
  // Midday UTC, so it's the same date in any time zone the tests run in.
  expect(formatDate('2014-03-24T12:00:00Z')).toBe('Mar 24, 2014')
})

describe('timeAgo', () => {
  const now = new Date('2026-11-10T12:00:00Z')
  const ago = (seconds: number) => new Date(now.getTime() - seconds * 1000).toISOString()

  test.each([
    [0, 'just now'],
    [59, 'just now'],
    [60, '1 minute ago'], // singular
    [119, '1 minute ago'], // whole units, rounded down
    [120, '2 minutes ago'],
    [3600, '1 hour ago'],
    [5 * 3600, '5 hours ago'],
    [24 * 3600, '1 day ago'],
    [3 * 24 * 3600, '3 days ago'],
    [31 * 24 * 3600, '1 month ago'],
    [400 * 24 * 3600, '1 year ago'],
    [-30, 'just now'], // a clock a little ahead of the server's: never "in the future"
  ])('%i seconds -> %s', (seconds, text) => {
    expect(timeAgo(ago(seconds), now)).toBe(text)
  })
})
