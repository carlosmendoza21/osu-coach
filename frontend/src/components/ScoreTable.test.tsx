// Unit tests for the plays table.
import { render, screen, within } from '@testing-library/react'
import { expect, test } from 'vitest'

import { score } from '../test/fixtures'
import ScoreTable from './ScoreTable'

function cells(row: HTMLElement): string[] {
  return within(row)
    .getAllByRole('cell')
    .map((c) => c.textContent ?? '')
}

test('a play reads as osu! shows it', () => {
  render(<ScoreTable scores={[score()]} caption="Top plays (1)" />)

  expect(screen.getByRole('table', { name: 'Top plays (1)' })).toBeInTheDocument()
  const [, row] = screen.getAllByRole('row') // the first row is the header
  expect(cells(row)).toEqual([
    'Lix - Tori no Uta -Ethereal House Mix- [Hard] 3.56★',
    'X',
    '73pp',
    '100.00%',
    '+PF',
    '254 / 0 / 0 / 0',
    '450x',
    'Mar 24, 2014',
  ])
})

test('the columns are named', () => {
  render(<ScoreTable scores={[score()]} caption="Plays" />)

  const headers = screen.getAllByRole('columnheader').map((h) => h.textContent)
  expect(headers).toEqual(['Map', 'Grade', 'pp', 'Accuracy', 'Mods', 'Hits', 'Combo', 'Date'])
})

test("the map links to its osu! page, in a new tab", () => {
  render(<ScoreTable scores={[score()]} caption="Plays" />)

  const link = screen.getByRole('link', { name: /Tori no Uta/ })
  expect(link).toHaveAttribute('href', 'https://osu.ppy.sh/b/22423')
  expect(link).toHaveAttribute('target', '_blank')
  expect(link).toHaveAttribute('rel', 'noreferrer') // the new tab can't reach back
})

test('a play whose map isn\'t stored shows the map id', () => {
  render(<ScoreTable scores={[score({ beatmap: null, beatmap_id: 75 })]} caption="Plays" />)

  const [, row] = screen.getAllByRole('row')
  expect(cells(row)[0]).toBe('map 75')
  expect(screen.queryByRole('link')).toBeNull()
})

test('a failed play is an F, dimmed, with no pp', () => {
  const failed = score({ passed: false, rank: 'F', pp: null, count_miss: 12 })
  render(<ScoreTable scores={[failed]} caption="Plays" />)

  const [, row] = screen.getAllByRole('row')
  expect(row).toHaveClass('failed')
  expect(cells(row)[1]).toBe('F')
  expect(cells(row)[2]).toBe('-')
  expect(within(row).getByText('12')).toHaveClass('miss') // misses stand out
})

test("a passed play isn't dimmed, and keeps its grade's colour", () => {
  render(<ScoreTable scores={[score({ rank: 'S' })]} caption="Plays" />)

  const [, row] = screen.getAllByRole('row')
  expect(row).not.toHaveClass('failed')
  expect(within(row).getByText('S')).toHaveClass('grade', 'grade-S')
})

test('every play is a row, in the order given', () => {
  const plays = [score({ id: 1, pp: 73 }), score({ id: 2, pp: 56 }), score({ id: 3, pp: 40 })]
  render(<ScoreTable scores={plays} caption="Plays" />)

  const rows = screen.getAllByRole('row').slice(1)
  expect(rows.map((r) => cells(r)[2])).toEqual(['73pp', '56pp', '40pp'])
})

test('no plays: a line saying so, not an empty table', () => {
  render(<ScoreTable scores={[]} caption="Recent plays (0)" />)

  expect(screen.queryByRole('table')).toBeNull()
  expect(screen.getByText('No plays to show.')).toBeInTheDocument()
})
