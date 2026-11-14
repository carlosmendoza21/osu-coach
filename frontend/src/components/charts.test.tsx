// Unit tests for the three charts and the panel that loads them. jsdom gives elements no
// size, so Recharts' ResponsiveContainer is swapped for a fixed 800 x 220 box, and canvas
// drawing is checked through a spy (src/heatmap.test.ts checks the drawing itself).
import { act, render, screen, within } from '@testing-library/react'
import { type ReactElement, cloneElement } from 'react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'

import * as heatmap from '../heatmap'
import { chartsData, note } from '../test/fixtures'
import Heatmap from './Heatmap'
import HitErrorChart from './HitErrorChart'
import ReplayChartsPanel from './ReplayChartsPanel'
import UrChart, { UR_WINDOW } from './UrChart'

vi.mock('recharts', async (original) => {
  const recharts = await original<typeof import('recharts')>()
  return {
    ...recharts,
    ResponsiveContainer: ({ children }: { children: ReactElement<{ width?: number; height?: number }> }) => (
      <div>{cloneElement(children, { width: 800, height: 220 })}</div>
    ),
  }
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

/** The x axis's tick labels (Recharts 3 keeps them apart from the axis line). */
function xTicks(container: HTMLElement): string[] {
  const labels = container.querySelectorAll('.recharts-xAxis-tick-labels .recharts-cartesian-axis-tick-value')
  return [...labels].map((t) => t.textContent ?? '')
}

// ---------- Hit timing ----------

test('the histogram has its caption, the three windows and a line at 0', () => {
  const { container } = render(<HitErrorChart charts={chartsData()} />)

  const figure = screen.getByRole('figure', { name: 'Hit error histogram' })
  expect(within(figure).getByText('Hit timing')).toBeInTheDocument()
  expect(figure).toHaveTextContent('negative is early')
  expect(container.querySelectorAll('.recharts-reference-area')).toHaveLength(3) // 300, 100, 50
  expect(container.querySelectorAll('.recharts-reference-line')).toHaveLength(1)
})

test("the histogram's axis spans the 50 window, rounded to whole bins", () => {
  const { container } = render(<HitErrorChart charts={chartsData()} />)

  const values = xTicks(container).map(Number)
  expect(values.length).toBeGreaterThan(2) // not passing on an empty list
  expect(Math.min(...values)).toBeGreaterThanOrEqual(-106) // 104.5 rounds up to 106
  expect(Math.max(...values)).toBeLessThanOrEqual(106)
})

test('a bar for each bin with hits', () => {
  // Hits in three different 2 ms bins.
  const notes = [note({ error_ms: -20 }), note({ idx: 1, error_ms: 0 }), note({ idx: 2, error_ms: 0.5 }), note({ idx: 3, error_ms: 30 })]
  const { container } = render(<HitErrorChart charts={chartsData({ notes })} />)

  expect(container.querySelectorAll('.recharts-bar-rectangle path')).toHaveLength(3)
})

// ---------- UR over time ----------

test('UR over time has its caption, its line, and a red line per miss', () => {
  const notes = [
    ...Array.from({ length: 60 }, (_, i) => note({ idx: i, time_ms: i * 100, error_ms: i % 2 ? 5 : -5 })),
    note({ idx: 60, time_ms: 2000, result: 'miss', error_ms: null }),
    note({ idx: 61, time_ms: 4000, result: 'miss', error_ms: null }),
  ]
  const { container } = render(<UrChart charts={chartsData({ notes })} />)

  const figure = screen.getByRole('figure', { name: 'UR over time' })
  expect(figure).toHaveTextContent(`over each ${UR_WINDOW} hits; lower is steadier`)
  expect(container.querySelectorAll('.recharts-line-curve')).toHaveLength(1)
  expect(container.querySelectorAll('.recharts-reference-line')).toHaveLength(2)
})

test('the time axis reads as minutes and seconds', () => {
  const notes = Array.from({ length: 60 }, (_, i) => note({ idx: i, time_ms: i * 3000, error_ms: i % 2 ? 5 : -5 }))
  const { container } = render(<UrChart charts={chartsData({ notes })} />)

  const ticks = xTicks(container)
  expect(ticks.length).toBeGreaterThan(2)
  expect(ticks.every((t) => /^\d+:\d\d$/.test(t))).toBe(true)
  expect(ticks[0]).toBe('0:00') // the whole map, not from the first UR point (2:27)
})

test('too few hits for a UR: said so instead of an empty chart', () => {
  render(<UrChart charts={chartsData({ notes: [note()] })} />)

  expect(screen.getByText('Too few hits to show UR over time.')).toBeInTheDocument()
  expect(screen.queryByRole('figure')).toBeNull()
})

// ---------- Heatmap ----------

test('the heatmap names what it marks, for screen readers too', () => {
  render(<Heatmap charts={chartsData()} />)

  const canvas = screen.getByRole('img', { name: /Cursor heatmap/ })
  expect(canvas).toHaveAttribute('aria-label', 'Cursor heatmap with 1 misses, 1 50s and 1 100s marked')
  expect(canvas).toHaveAttribute('width', '512') // the playfield's own size, scaled by CSS
  expect(canvas).toHaveAttribute('height', '384')
  const legend = screen.getByText('✕ miss').parentElement!
  expect(legend).toHaveTextContent('✕ miss○ 50○ 100')
  expect(legend).toHaveTextContent('3 frames off the playfield')
})

test('no frames off the playfield: not mentioned', () => {
  const data = chartsData()
  render(<Heatmap charts={{ ...data, cursor: { ...data.cursor, off_playfield: 0 } }} />)

  expect(screen.queryByText(/off the playfield/)).toBeNull()
})

test('the canvas is drawn with the chart data', () => {
  const ctx = {} as CanvasRenderingContext2D
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(ctx as never)
  const draw = vi.spyOn(heatmap, 'draw').mockImplementation(() => {})
  const data = chartsData()

  render(<Heatmap charts={data} />)

  expect(draw).toHaveBeenCalledWith(ctx, data)
})

test('without a 2D context (no canvas support), nothing breaks', () => {
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
  const draw = vi.spyOn(heatmap, 'draw')

  render(<Heatmap charts={chartsData()} />)

  expect(draw).not.toHaveBeenCalled()
  expect(screen.getByRole('img', { name: /Cursor heatmap/ })).toBeInTheDocument()
})

// ---------- The panel ----------

let urls: string[]
let answer: () => Promise<Response>

beforeEach(() => {
  urls = []
  answer = async () => Response.json(chartsData())
})

function fakeFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) => {
      urls.push(url)
      return answer()
    }),
  )
}

test('the panel loads this replay\'s charts, then shows all three', async () => {
  fakeFetch()
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)

  render(<ReplayChartsPanel id={7} />)
  expect(screen.getByRole('status')).toHaveTextContent('Loading the charts...')
  await act(async () => {})

  expect(urls).toEqual(['/api/replays/7/charts'])
  const panel = screen.getByRole('region', { name: 'Charts' })
  expect(within(panel).getAllByRole('figure').map((f) => f.getAttribute('aria-label'))).toEqual([
    'Hit error histogram',
    'UR over time',
    'Cursor heatmap',
  ])
})

test("if the charts can't be loaded, it says so (and only the charts are missing)", async () => {
  fakeFetch()
  answer = async () => Response.json({ detail: 'boom' }, { status: 500 })

  render(<ReplayChartsPanel id={7} />)
  await act(async () => {})

  expect(screen.getByText("The charts couldn't be loaded.")).toBeInTheDocument()
  expect(screen.queryByRole('region', { name: 'Charts' })).toBeNull()
})

test('another replay: its own charts, never the last one\'s', async () => {
  fakeFetch()
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
  const { rerender } = render(<ReplayChartsPanel id={7} />)
  await act(async () => {})
  answer = () => new Promise(() => {}) // replay 8's answer hasn't come yet

  rerender(<ReplayChartsPanel id={8} />)

  expect(urls).toEqual(['/api/replays/7/charts', '/api/replays/8/charts'])
  expect(screen.getByRole('status')).toHaveTextContent('Loading the charts...')
  expect(screen.queryByRole('region', { name: 'Charts' })).toBeNull()
})

test("a late answer for the last replay doesn't replace this one's charts", async () => {
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
  const replies: ((r: Response) => void)[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) => {
      urls.push(url)
      return new Promise<Response>((reply) => replies.push(reply))
    }),
  )
  const { rerender } = render(<ReplayChartsPanel id={7} />)
  rerender(<ReplayChartsPanel id={8} />)

  // Replay 8 answers first (no misses), then replay 7's late answer (1 miss).
  await act(async () => replies[1](Response.json(chartsData({ notes: [note()] }))))
  await act(async () => replies[0](Response.json(chartsData())))

  expect(screen.getByRole('img', { name: /Cursor heatmap/ })).toHaveAttribute(
    'aria-label',
    'Cursor heatmap with 0 misses, 0 50s and 0 100s marked',
  )
})
