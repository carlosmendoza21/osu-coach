// Unit tests for the practice maps panel. fetch is replaced with a fake that answers from
// a list, and timers are fake. The waiting itself is tested in
// src/hooks/usePollUntilReady.test.tsx; this checks what the panel shows.
import { act, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'

import { recommendation, recommendations } from '../test/fixtures'
import type { Recommendations } from '../types'
import RecommendationsPanel, { RECS_GIVE_UP_MS, RECS_POLL_MS } from './RecommendationsPanel'

let urls: string[]
let answers: (() => Promise<Response>)[] // used in order; the last one repeats

beforeEach(() => {
  vi.useFakeTimers()
  urls = []
  answers = [async () => Response.json(recommendations())]
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) => {
      urls.push(url)
      const next = answers.length > 1 ? answers.shift()! : answers[0]
      return next()
    }),
  )
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

const made = (changes: Partial<Recommendations> = {}) => async () => Response.json(recommendations(changes))
const status = (code: number, detail: string) => async () => Response.json({ detail }, { status: code })
const notReady = status(409, "recommendations aren't ready yet")
// A player scored before recommendations existed: none made yet.
const noneMadeYet = made({ target_skill: null, comfort_stars: null, created_at: null, maps: [] })

async function wait(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

async function panel(osuId = 2) {
  const view = render(<RecommendationsPanel osuId={osuId} />)
  await wait(0)
  return view
}

const section = () => screen.getByRole('region', { name: 'Practice maps' })
const items = () => within(section()).queryAllByRole('listitem')

// ---------- The maps ----------

describe('practice maps', () => {
  test("they're asked for by osu! id", async () => {
    await panel(7562902)

    expect(urls).toEqual(['/api/players/7562902/recommendations'])
  })

  test('loading until the answer', () => {
    answers = [() => new Promise(() => {})]
    render(<RecommendationsPanel osuId={2} />)

    expect(within(section()).getByRole('status')).toHaveTextContent('Loading the practice maps...')
  })

  test('the heading names the skill and the comfort level', async () => {
    await panel()

    expect(within(section()).getByRole('heading')).toHaveTextContent(
      'Practice maps for Reading (set from a comfort level of 3.68★)',
    )
  })

  test('the maps, best first, in a numbered list', async () => {
    answers = [made({ maps: [3, 1, 2].map((r) => recommendation(r)) })]
    await panel()

    expect(within(section()).getByRole('list').tagName).toBe('OL')
    // In the order the API sends (it sorts by rank): the panel doesn't reorder.
    expect(items().map((li) => within(li).getByRole('link').textContent)).toEqual([
      'Artist 3 - Title 3 [Insane]',
      'Artist 1 - Title 1 [Insane]',
      'Artist 2 - Title 2 [Insane]',
    ])
  })

  test('a map: linked to osu!, its mapper, stats and reason', async () => {
    answers = [made({ maps: [recommendation(1, { star_rating: 4.27, bpm: 170.4, ar: 8.6, od: 8.3, hit_length_s: 30 })] })]
    await panel()

    const [li] = items()
    const link = within(li).getByRole('link')
    expect(link).toHaveAttribute('href', 'https://osu.ppy.sh/b/1001')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noreferrer') // the new tab can't reach back
    expect(li).toHaveTextContent('by Mapper')
    expect(li).toHaveTextContent('4.27★ · 170 BPM · AR 8.6 · OD 8.3 · 0:30')
    expect(li).toHaveTextContent('AR 8.5; 4.1★, 0.1★ above your comfort level (3.7★)')
  })

  test('every map is shown', async () => {
    answers = [made({ maps: Array.from({ length: 10 }, (_, i) => recommendation(i + 1)) })]
    await panel()

    expect(items()).toHaveLength(10)
  })

  test('made, but nothing fit', async () => {
    answers = [made({ maps: [] })]
    await panel()

    expect(section()).toHaveTextContent('No practice maps fit this player yet.')
    expect(within(section()).queryByRole('list')).toBeNull()
    await wait(10 * RECS_POLL_MS)
    expect(urls).toHaveLength(1) // made: not asked again
  })
})

// ---------- Not picked yet ----------

describe('not picked yet', () => {
  test.each([
    ['not scored yet (409)', notReady],
    // Edge: scored before recommendations existed. Opening the page syncs the player,
    // which picks them, so this is waited out like a 409.
    ['none made yet', noneMadeYet],
  ])('%s: picking, then the maps once made', async (_name, first) => {
    answers = [first, first, made()]
    await panel()

    expect(within(section()).getByRole('status')).toHaveTextContent('Picking practice maps...')
    expect(section()).not.toHaveTextContent('No practice maps')
    await wait(2 * RECS_POLL_MS)
    expect(items()).toHaveLength(3)
    expect(urls).toHaveLength(3)
  })

  test('given up on after a few minutes, with a message', async () => {
    answers = [noneMadeYet]
    await panel()

    await wait(RECS_GIVE_UP_MS)

    expect(section()).toHaveTextContent("The practice maps aren't picked yet. Reload the page in a few minutes.")
    expect(within(section()).queryByRole('status')).toBeNull()
  })
})

// ---------- Errors ----------

describe('errors', () => {
  test.each([
    ['an unknown player (404)', status(404, 'player 2 not found'), 'No practice maps for this player.'],
    ['a server error (500)', status(500, 'Internal Server Error'), "The practice maps couldn't be loaded."],
    ['no answer at all', () => Promise.reject(new TypeError('Failed to fetch')), "The practice maps couldn't be loaded."],
  ])('%s', async (_name, answer, message) => {
    answers = [answer]
    await panel()

    expect(section()).toHaveTextContent(message)
    expect(within(section()).getByRole('heading')).toHaveTextContent(/^Practice maps$/)
    await wait(10 * RECS_POLL_MS)
    expect(urls).toHaveLength(1)
  })
})
