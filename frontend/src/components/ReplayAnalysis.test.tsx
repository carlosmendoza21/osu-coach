// Unit tests for the analysis: the play header, stats, counts and patterns.
import { render, screen, within } from '@testing-library/react'
import { expect, test } from 'vitest'

import { pendingDetail, replayDetail } from '../test/fixtures'
import ReplayAnalysis, { ReplayHeader } from './ReplayAnalysis'

function rowTexts(table: HTMLElement): string[][] {
  return within(table)
    .getAllByRole('row')
    .slice(1) // the header row
    .map((row) => Array.from(row.children).map((c) => c.textContent ?? ''))
}

// ---------- The header ----------

test('the header names the map, linked to osu!, and the play', () => {
  render(<ReplayHeader detail={replayDetail()} />)

  const link = screen.getByRole('link', { name: 'xi - FREEDOM DiVE [Arles]' })
  expect(link).toHaveAttribute('href', 'https://osu.ppy.sh/b/4904540')
  expect(link).toHaveAttribute('rel', 'noreferrer')
  expect(screen.getByRole('region', { name: 'Play' })).toHaveTextContent(
    'Played by mrekk · NM · 9.68★ · Aug 31, 2026',
  )
})

test('with mods', () => {
  const d = replayDetail()
  render(<ReplayHeader detail={{ ...d, replay: { ...d.replay, mods: ['HD', 'DT'] } }} />)

  expect(screen.getByRole('region', { name: 'Play' })).toHaveTextContent('Played by mrekk · +HDDT')
})

test('before the map is fetched: said so, and no stars', () => {
  render(<ReplayHeader detail={pendingDetail(false)} />)

  const play = screen.getByRole('region', { name: 'Play' })
  expect(play).toHaveTextContent('Map not fetched yet')
  expect(play).not.toHaveTextContent('★')
  expect(screen.queryByRole('link')).toBeNull()
})

// ---------- The summary ----------

test('the summary', () => {
  render(<ReplayAnalysis detail={replayDetail()} />)

  const summary = screen.getByRole('region', { name: 'Summary' })
  expect(summary).toHaveTextContent('Accuracy99.58%')
  expect(summary).toHaveTextContent('UR66.9')
  expect(summary).toHaveTextContent('Timing2.2 ms early')
  expect(summary).toHaveTextContent('Aim error11.3 px')
  expect(summary).toHaveTextContent('Max combo2666x')
})

test("UR and aim error say what they mean when hovered", () => {
  render(<ReplayAnalysis detail={replayDetail()} />)

  expect(screen.getByText('UR')).toHaveAttribute('title', expect.stringContaining('Lower is steadier'))
  expect(screen.getByText('Aim error')).toHaveAttribute('title', expect.stringContaining('center'))
})

// ---------- Counts ----------

test("osu!'s counts and ours, side by side, matching", () => {
  render(<ReplayAnalysis detail={replayDetail()} />)

  const counts = screen.getByRole('table', { name: /Hit counts/ })
  expect(rowTexts(counts)).toEqual([
    ['osu!', '2224', '14', '0', '0'],
    ['Our analysis', '2224', '14', '0', '0'],
  ])
  expect(within(counts).getByText('✓ matches osu!')).toHaveClass('match', 'ok')
})

test("when they differ, it's said, with the known reason", () => {
  render(<ReplayAnalysis detail={replayDetail({ matches_official: false })} />)

  const counts = screen.getByRole('table', { name: /Hit counts/ })
  expect(counts).toHaveTextContent("differs from osu! (spinners aren't judged yet)")
  expect(within(counts).queryByText('✓ matches osu!')).toBeNull()
})

test('without our counts (not analyzed), only osu!\'s row and no verdict', () => {
  render(<ReplayAnalysis detail={replayDetail({ judged: null, matches_official: null })} />)

  const counts = screen.getByRole('table', { name: 'Hit counts' })
  expect(rowTexts(counts)).toEqual([['osu!', '2224', '14', '0', '0']])
})

// ---------- Patterns ----------

test('patterns come in a fixed order, each with its accuracy', () => {
  render(<ReplayAnalysis detail={replayDetail()} />)

  // The API sent them as other, stream, jump, slider.
  expect(rowTexts(screen.getByRole('table', { name: 'By pattern' }))).toEqual([
    ['jump', '263', '0', '0', '0', '100.00%'],
    ['stream', '1438', '5', '0', '0', '99.77%'],
    ['slider', '336', '6', '0', '0', '98.83%'],
    ['other', '187', '3', '0', '0', '98.95%'],
  ])
})

test('misses stand out, and an unknown pattern goes last', () => {
  const detail = replayDetail({
    by_pattern: {
      tech: { '300': 5, '100': 0, '50': 0, miss: 1 },
      stream: { '300': 532, '100': 14, '50': 2, miss: 8 },
      jump: { '300': 115, '100': 0, '50': 0, miss: 0 },
    },
  })
  render(<ReplayAnalysis detail={detail} />)

  const table = screen.getByRole('table', { name: 'By pattern' })
  expect(rowTexts(table).map((r) => r[0])).toEqual(['jump', 'stream', 'tech'])
  expect(within(table).getByText('8')).toHaveClass('miss')
  expect(within(table).getAllByText('0').every((c) => !c.classList.contains('miss'))).toBe(true)
})

test("when the counts differ, each row keeps its own", () => {
  const detail = replayDetail({
    judged: { '300': 2223, '100': 14, '50': 1, miss: 0 }, // one 300 judged a 50
    matches_official: false,
  })
  render(<ReplayAnalysis detail={detail} />)

  expect(rowTexts(screen.getByRole('table', { name: /Hit counts/ }))).toEqual([
    ['osu!', '2224', '14', '0', '0'],
    ['Our analysis', '2223', '14', '1', '0'],
  ])
})
