// Unit tests for src/heatmap.ts: what gets drawn on the canvas. A fake 2D context records
// each call (jsdom has no canvas of its own).
import { expect, test } from 'vitest'

import { MARK_COLORS, PLAYFIELD_HEIGHT, PLAYFIELD_WIDTH, draw } from './heatmap'
import { chartsData, note } from './test/fixtures'

type Call = [string, ...unknown[]]

function fakeContext() {
  const calls: Call[] = []
  const ctx: Record<string, unknown> = { fillStyle: '', strokeStyle: '', lineWidth: 1 }
  for (const name of ['clearRect', 'fillRect', 'beginPath', 'moveTo', 'lineTo', 'arc', 'stroke']) {
    ctx[name] = (...args: unknown[]) => {
      // Record the style in effect for fills and strokes.
      if (name === 'fillRect') calls.push([name, ctx.fillStyle, ...args])
      else if (name === 'stroke') calls.push([name, ctx.strokeStyle])
      else calls.push([name, ...args])
    }
  }
  return { ctx: ctx as unknown as CanvasRenderingContext2D, calls }
}

test('the playfield is cleared and filled first', () => {
  const { ctx, calls } = fakeContext()

  draw(ctx, chartsData())

  expect(calls[0]).toEqual(['clearRect', 0, 0, PLAYFIELD_WIDTH, PLAYFIELD_HEIGHT])
  expect(calls[1]).toEqual(['fillRect', '#1f1d24', 0, 0, 512, 384])
})

test('each cell the cursor visited is shaded, and only those', () => {
  const { ctx, calls } = fakeContext()

  draw(ctx, chartsData()) // a 4 x 3 grid: cells of 128 x 128 px

  const cells = calls.filter((c) => c[0] === 'fillRect').slice(1)
  expect(cells.map((c) => c.slice(2))).toEqual([
    [128, 0, 128, 128], // row 0, col 1: 1
    [256, 0, 128, 128], // row 0, col 2: 4
    [0, 128, 128, 128], // row 1, col 0: 9
    [128, 128, 128, 128], // row 1, col 1: 16, the busiest
    [384, 256, 128, 128], // row 2, col 3: 1
  ])
  // Brightness grows with time spent, fully shaded for the busiest cell.
  expect(cells.map((c) => c[1])).toEqual([
    'rgba(255, 102, 170, 0.250)', // sqrt(1/16)
    'rgba(255, 102, 170, 0.500)', // sqrt(4/16)
    'rgba(255, 102, 170, 0.750)',
    'rgba(255, 102, 170, 1.000)',
    'rgba(255, 102, 170, 0.250)',
  ])
})

test('misses, 50s and 100s are marked where the note was; 300s are not', () => {
  const { ctx, calls } = fakeContext()

  draw(ctx, chartsData())

  expect(calls.filter((c) => c[0] === 'stroke').map((c) => c[1])).toEqual([
    MARK_COLORS['100'], // note 2
    MARK_COLORS['50'], // note 3
    MARK_COLORS.miss, // note 4
  ])
  // The 100 and the 50: circles of radius 4 at their notes.
  const arcs = calls.filter((c) => c[0] === 'arc').map((c) => c.slice(1, 4))
  expect(arcs).toEqual([
    [100, 100, 4],
    [400, 50, 4],
  ])
  // The miss: an x, two strokes through its note (50, 300).
  const lines = calls.filter((c) => c[0] === 'moveTo' || c[0] === 'lineTo').map((c) => c.slice(1))
  expect(lines).toEqual([
    [45, 295],
    [55, 305],
    [55, 295],
    [45, 305],
  ])
})

test('no cursor time at all: just the empty playfield', () => {
  const { ctx, calls } = fakeContext()

  draw(ctx, chartsData({ cursor: { cols: 2, rows: 2, counts: [0, 0, 0, 0], off_playfield: 0 }, notes: [note()] }))

  expect(calls.map((c) => c[0])).toEqual(['clearRect', 'fillRect'])
})
