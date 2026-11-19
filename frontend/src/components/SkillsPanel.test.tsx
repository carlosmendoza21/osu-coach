// Unit tests for the skills panel and its radar. fetch is replaced with a fake that
// answers from a list, and timers are fake, so minutes of waiting for a profile take no
// time. jsdom gives elements no size, so Recharts' ResponsiveContainer is swapped for a
// fixed 400 x 300 box.
import { act, render, screen, within } from '@testing-library/react'
import { type ReactElement, cloneElement } from 'react'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'

import { skills } from '../test/fixtures'
import type { Skills } from '../types'
import SkillsPanel, { SKILLS_GIVE_UP_MS, SKILLS_POLL_MS } from './SkillsPanel'
import SkillsRadar from './SkillsRadar'

vi.mock('recharts', async (original) => {
  const recharts = await original<typeof import('recharts')>()
  return {
    ...recharts,
    ResponsiveContainer: ({ children }: { children: ReactElement<{ width?: number; height?: number }> }) => (
      <div>{cloneElement(children, { width: 400, height: 300 })}</div>
    ),
  }
})

let urls: string[]
let answers: (() => Promise<Response>)[] // used in order; the last one repeats

beforeEach(() => {
  vi.useFakeTimers()
  urls = []
  answers = [async () => Response.json(skills())]
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

const scored = (changes: Partial<Skills> = {}) => async () => Response.json(skills(changes))
const status = (code: number, detail: string) => async () => Response.json({ detail }, { status: code })
const notReady = status(409, "skill profile isn't ready yet")

async function wait(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

/** Render the panel and let the first answer (and the lazy radar) arrive. */
async function panel(osuId = 2) {
  const view = render(<SkillsPanel osuId={osuId} />)
  await wait(0)
  await wait(0)
  return view
}

const section = () => screen.getByRole('region', { name: 'Skills' })

// ---------- The profile ----------

describe('a scored player', () => {
  test("it asks for the player's skills by osu! id", async () => {
    await panel(7562902)

    expect(urls).toEqual(['/api/players/7562902/skills'])
  })

  test('it says it is loading until the answer', () => {
    answers = [() => new Promise(() => {})]
    render(<SkillsPanel osuId={2} />)

    expect(within(section()).getByRole('status')).toHaveTextContent('Loading the skills...')
  })

  test('the strongest and weakest, and how many plays', async () => {
    await panel()

    expect(section()).toHaveTextContent('Strongest: Consistency · Weakest: Reading · from 50 plays')
  })

  test('plays not scored yet are mentioned', async () => {
    answers = [scored({ plays_used: 58, plays_skipped: 3 })]
    await panel()

    expect(section()).toHaveTextContent('from 58 plays (3 not scored yet)')
  })

  test('the six scores, in the radar order, to one decimal', async () => {
    const { container } = await panel()

    const rows = [...container.querySelectorAll('.skills-scores div')].map((d) => d.textContent)
    expect(rows).toEqual(['Aim41.1', 'Speed32.9', 'Stamina38.3', 'Accuracy69.4', 'Consistency80.9', 'Reading8.0'])
  })

  test('the radar is drawn', async () => {
    const { container } = await panel()

    expect(container.querySelector('.recharts-radar-polygon')).not.toBeNull()
  })

  test('a scored profile is not asked for again', async () => {
    await panel()
    await wait(10 * SKILLS_POLL_MS)

    expect(urls).toHaveLength(1)
  })
})

// ---------- Not scored yet ----------

describe('a player not scored yet', () => {
  test('it says the skills are being scored', async () => {
    answers = [notReady]
    await panel()

    expect(within(section()).getByRole('status')).toHaveTextContent("Scoring skills from this player's plays...")
  })

  test('it asks again every few seconds, not sooner', async () => {
    answers = [notReady]
    await panel()

    await wait(SKILLS_POLL_MS - 1)
    expect(urls).toHaveLength(1)
    await wait(1)
    expect(urls).toHaveLength(2)
    await wait(SKILLS_POLL_MS)
    expect(urls).toHaveLength(3)
  })

  test('the profile shows once it is scored, and the asking stops', async () => {
    answers = [notReady, notReady, scored()]
    await panel()

    await wait(2 * SKILLS_POLL_MS)
    await wait(0)
    expect(section()).toHaveTextContent('Strongest: Consistency')
    await wait(10 * SKILLS_POLL_MS)
    expect(urls).toHaveLength(3)
  })

  test('it gives up after a few minutes, and says so', async () => {
    answers = [notReady]
    await panel()

    await wait(SKILLS_GIVE_UP_MS)
    const asked = urls.length
    expect(section()).toHaveTextContent("The skills aren't scored yet. Reload the page in a few minutes.")
    expect(within(section()).queryByRole('status')).toBeNull()
    await wait(10 * SKILLS_POLL_MS)
    expect(urls).toHaveLength(asked)
    // One request per poll until then: no more, no fewer.
    expect(asked).toBe(SKILLS_GIVE_UP_MS / SKILLS_POLL_MS + 1)
  })

  test('leaving the page stops the asking', async () => {
    answers = [notReady]
    const { unmount } = await panel()

    unmount()
    await wait(10 * SKILLS_POLL_MS)
    expect(urls).toHaveLength(1)
  })
})

// ---------- Errors ----------

describe('errors', () => {
  test.each([
    ['an unknown player (404)', status(404, 'player 2 not found'), 'No skill profile for this player.'],
    ['a server error (500)', status(500, 'Internal Server Error'), "The skills couldn't be loaded."],
    ['no answer at all', () => Promise.reject(new TypeError('Failed to fetch')), "The skills couldn't be loaded."],
  ])('%s', async (_name, answer, message) => {
    answers = [answer]
    await panel()

    expect(section()).toHaveTextContent(message)
    // An error isn't "not scored yet": nothing is asked again.
    await wait(10 * SKILLS_POLL_MS)
    expect(urls).toHaveLength(1)
  })
})

// ---------- Another player ----------

describe('another player', () => {
  test('a new osu! id is asked for, and its profile shown', async () => {
    answers = [scored(), scored({ player_id: 3, strongest: 'aim' })]
    const { rerender } = await panel(2)

    rerender(<SkillsPanel osuId={3} />)
    await wait(0)
    await wait(0)

    expect(urls).toEqual(['/api/players/2/skills', '/api/players/3/skills'])
    expect(section()).toHaveTextContent('Strongest: Aim')
  })

  test("the last player's late answer doesn't show for the new one", async () => {
    let late: (r: Response) => void = () => {}
    answers = [() => new Promise((reply) => (late = reply)), () => new Promise(() => {})]
    const { rerender } = await panel(2)

    rerender(<SkillsPanel osuId={3} />)
    await act(async () => late(Response.json(skills())))
    await wait(0)

    expect(within(section()).getByRole('status')).toHaveTextContent('Loading the skills...')
  })

  test("the last player's answer arriving after the new one's doesn't replace it", async () => {
    let late: (r: Response) => void = () => {}
    answers = [() => new Promise((reply) => (late = reply)), scored({ player_id: 3, strongest: 'aim' })]
    const { rerender } = await panel(2)

    rerender(<SkillsPanel osuId={3} />)
    await wait(0)
    await wait(0)
    await act(async () => late(Response.json(skills())))
    await wait(0)

    expect(section()).toHaveTextContent('Strongest: Aim')
  })

  test("the last player's profile isn't shown while the new one loads", async () => {
    answers = [scored(), () => new Promise(() => {})]
    const { rerender } = await panel(2)
    expect(section()).toHaveTextContent('Strongest: Consistency')

    rerender(<SkillsPanel osuId={3} />)

    expect(within(section()).getByRole('status')).toHaveTextContent('Loading the skills...')
    expect(section()).not.toHaveTextContent('Strongest')
  })

  test("the last player's polling stops", async () => {
    answers = [notReady, () => new Promise(() => {})]
    const { rerender } = await panel(2)
    rerender(<SkillsPanel osuId={3} />)
    await wait(0)

    await wait(10 * SKILLS_POLL_MS)
    // 2's first look, then 3's (which never answers): 2 isn't asked about again.
    expect(urls).toEqual(['/api/players/2/skills', '/api/players/3/skills'])
  })
})

// ---------- The radar itself ----------

/** Each radar point's distance from the centre, as a share of the outer ring's. */
function shares(container: HTMLElement): number[] {
  const points = (d: string) =>
    [...d.matchAll(/(-?[\d.]+),(-?[\d.]+)/g)].map((m) => [Number(m[1]), Number(m[2])] as const)
  const rings = container.querySelectorAll('.recharts-polar-grid-concentric-polygon')
  const outer = points(rings[rings.length - 1].getAttribute('d') ?? '')
  const shape = points(container.querySelector('.recharts-radar-polygon path')?.getAttribute('d') ?? '')
  const cx = outer.reduce((s, p) => s + p[0], 0) / outer.length
  const cy = outer.reduce((s, p) => s + p[1], 0) / outer.length
  const r = Math.hypot(outer[0][0] - cx, outer[0][1] - cy)
  return shape.slice(0, 6).map(([x, y]) => Math.round((Math.hypot(x - cx, y - cy) / r) * 1000) / 1000)
}

const labels = (container: HTMLElement) => [...container.querySelectorAll('.recharts-polar-angle-axis-tick text')]

describe('the radar', () => {
  test('its six labels, round from the top', () => {
    const { container } = render(<SkillsRadar skills={skills()} />)

    expect(labels(container).map((t) => t.textContent)).toEqual([
      'Aim',
      'Speed',
      'Stamina',
      'Accuracy',
      'Consistency',
      'Reading',
    ])
  })

  test('each point is as far out as its score, on a fixed 0-100 scale', () => {
    // Edge: the best is only 50, so a scale fitted to the data would push it to the edge
    // (a fitted scale rounds up to a "nice" number: 80 would still be drawn on 0-100).
    const { container } = render(
      <SkillsRadar skills={skills({ aim: 50, speed: 40, stamina: 20, accuracy: 30, consistency: 10, reading: 25 })} />,
    )

    expect(shares(container)).toEqual([0.5, 0.4, 0.2, 0.3, 0.1, 0.25])
  })

  test('aim points straight up', () => {
    const { container } = render(<SkillsRadar skills={skills()} />)

    const [aim, ...others] = labels(container)
    const y = (e: Element) => Number(e.getAttribute('y'))
    expect(aim.textContent).toBe('Aim')
    for (const other of others) expect(y(aim)).toBeLessThan(y(other))
  })
})
