// Unit tests for the Coach panel. fetch is replaced with a fake whose answer is a stream
// the test feeds event by event, so each stage of a report (starting, steps, the report,
// errors) can be checked; timers are fake, so minutes of waiting take no time.
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'

import { coachReport } from '../test/fixtures'
import type { CoachEvent, CoachReport } from '../types'
import CoachPanel from './CoachPanel'

type Feed = { send: (e: CoachEvent) => void; end: () => void }

let urls: string[]
let signals: (AbortSignal | undefined)[]
let feed: Feed
let answer: () => Response

/** A streamed answer the test controls through `feed`. */
function stream(): Response {
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      feed = {
        send: (e) => controller.enqueue(encoder.encode(`data: ${JSON.stringify(e)}\n\n`)),
        end: () => controller.close(),
      }
    },
  })
  return new Response(body, { headers: { 'Content-Type': 'text/event-stream' } })
}

beforeEach(() => {
  vi.useFakeTimers()
  urls = []
  signals = []
  answer = stream
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      urls.push(url)
      signals.push(init?.signal ?? undefined)
      return answer()
    }),
  )
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

async function settle(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

const section = () => screen.getByRole('region', { name: 'Coach' })
const button = () => within(section()).getByRole('button')

async function coach(osuId = 2) {
  const view = render(<CoachPanel osuId={osuId} />)
  fireEvent.click(button())
  await settle()
  return view
}

async function send(...events: CoachEvent[]) {
  for (const e of events) feed.send(e)
  await settle()
}

async function finish(report: CoachReport = coachReport()) {
  await send({ type: 'report', report })
  feed.end()
  await settle()
}

// ---------- Before and while ----------

describe('asking', () => {
  test('before: a button and what it does, nothing asked yet', () => {
    render(<CoachPanel osuId={2} />)

    expect(button()).toHaveTextContent('Coach me')
    expect(section()).toHaveTextContent("A report on this player's strengths and weaknesses")
    expect(urls).toEqual([])
  })

  test('the report is asked for, for this player', async () => {
    await coach(7562902)

    expect(urls).toEqual(['/api/players/7562902/report'])
  })

  test('while it runs: starting, the button off, and a word on the wait', async () => {
    await coach()

    expect(within(section()).getByRole('status')).toHaveTextContent('Starting... 0:00')
    expect(button()).toHaveTextContent('Coaching...')
    expect(button()).toBeDisabled()
    expect(section()).toHaveTextContent('A local model can take a few minutes.')
  })

  test('each step as it comes, the latest first and the rest below', async () => {
    await coach()
    await send({ type: 'step', text: 'Reading your profile' }, { type: 'step', text: 'Looking up your skills' })

    expect(within(section()).getByRole('status')).toHaveTextContent('Looking up your skills...')
    const done = within(section()).getByRole('list')
    expect(within(done).getAllByRole('listitem').map((li) => li.textContent)).toEqual(['Reading your profile'])
  })

  test('the time so far, every second', async () => {
    await coach()
    await send({ type: 'step', text: 'Writing your report' })

    await settle(65_000)

    expect(within(section()).getByRole('status')).toHaveTextContent('Writing your report... 1:05')
  })
})

// ---------- The report ----------

describe('the report', () => {
  test("a model's: who wrote it, the summary, each weakness with its explanation and evidence", async () => {
    await coach()
    await finish()

    expect(section()).toHaveTextContent("Written by ollama:qwen3:8b, from this player's numbers.")
    expect(section()).toHaveTextContent("You're strong in consistency; reading is where to work.")
    const [weaknesses] = within(section()).getAllByRole('list')
    const first = within(weaknesses).getAllByRole('listitem')[0]
    expect(first).toHaveTextContent('Reading 8.0/100')
    expect(first).toHaveTextContent('You rarely see high approach rates.')
    expect(first).toHaveTextContent("The highest AR you've passed is 9.0; your plays average AR 6.7.")
    expect(first).toHaveTextContent('0 of your 50 plays use Hidden.')
  })

  test('the weaknesses in order, then the strengths, then the plan', async () => {
    await coach()
    await finish()

    const headings = within(section()).getAllByRole('heading', { level: 4 }).map((h) => h.textContent)
    expect(headings).toEqual(['Weaknesses', 'Strengths', 'Practice plan'])
    const text = section().textContent ?? ''
    expect(text.indexOf('Reading 8.0')).toBeLessThan(text.indexOf('Speed 32.9'))
    expect(text.indexOf('Speed 32.9')).toBeLessThan(text.indexOf('Stamina 38.3'))
    expect(section()).toHaveTextContent('Then stamina: hold your tapping to the end of long streams.')
  })

  test('a pointer to the practice maps, for the weakest skill', async () => {
    await coach()
    await finish()

    const link = within(section()).getByRole('link', { name: 'The practice maps below' })
    expect(link).toHaveAttribute('href', '#practice-maps')
    expect(link.parentElement).toHaveTextContent("are picked for peppy's weakest skill, reading.")
  })

  test('no pointer without practice maps', async () => {
    await coach()
    await finish(coachReport({ maps: [] }))

    expect(within(section()).queryByRole('link')).toBeNull()
  })

  test('a template report says so', async () => {
    await coach()
    await finish(coachReport({ source: 'template', model: null, fallback_reason: null }))

    expect(section()).toHaveTextContent("From templates, with this player's numbers.")
    expect(section()).not.toHaveTextContent('Written by')
  })

  test("a model's words refused: the template, and why", async () => {
    await coach()
    await finish(coachReport({ source: 'template', fallback_reason: 'numbers not in the facts: 8' }))

    expect(section()).toHaveTextContent(
      "From templates, with this player's numbers (ollama:qwen3:8b was tried, but its words weren't used: numbers not in the facts: 8).",
    )
  })

  test('no explanation in a template report, only the evidence', async () => {
    const report = coachReport({ source: 'template', model: null })
    report.weaknesses = report.weaknesses.map((w) => ({ ...w, explanation: null }))
    await coach()
    await finish(report)

    expect(section()).not.toHaveTextContent('You rarely see high approach rates.')
    expect(section()).toHaveTextContent('0 of your 50 plays use Hidden.')
  })

  test('done: the button asks again, and a new report replaces the old', async () => {
    await coach()
    await finish()
    expect(button()).toHaveTextContent('Coach me again')
    expect(button()).toBeEnabled()

    fireEvent.click(button())
    await settle()
    expect(within(section()).getByRole('status')).toHaveTextContent('Starting...')
    await finish(coachReport({ summary: 'A second look.' }))

    expect(section()).toHaveTextContent('A second look.')
    expect(urls).toHaveLength(2)
  })
})

// ---------- Errors ----------

describe('errors', () => {
  test.each([
    [409, "The coach needs this player's skills first: they're still being scored."],
    [404, 'There is no such player to coach.'],
    [500, "The coach couldn't write a report. Try again."],
  ])('a %i is explained', async (status, message) => {
    answer = () => Response.json({ detail: 'x' }, { status })
    await coach()

    expect(within(section()).getByRole('alert')).toHaveTextContent(message)
    expect(button()).toHaveTextContent('Coach me again')
  })

  test('no answer at all is explained', async () => {
    answer = () => {
      throw new TypeError('Failed to fetch')
    }
    await coach()

    expect(within(section()).getByRole('alert')).toHaveTextContent("The coach couldn't write a report. Try again.")
  })

  test('an error event is explained', async () => {
    await coach()
    await send({ type: 'error', detail: "the report couldn't be written (RuntimeError('bug'))" })

    expect(within(section()).getByRole('alert')).toHaveTextContent("The coach couldn't write a report. Try again.")
  })

  test('a stream that ends without a report is explained', async () => {
    await coach()
    await send({ type: 'step', text: 'Reading your profile' })
    feed.end()
    await settle()

    expect(within(section()).getByRole('alert')).toHaveTextContent('The report stopped before it was done.')
  })
})

// ---------- Leaving ----------

describe('leaving', () => {
  test('leaving the page cancels the report', async () => {
    const { unmount } = await coach()

    unmount()

    expect(signals[0]?.aborted).toBe(true)
  })

  test('asking again cancels the first', async () => {
    await coach()
    await finish()
    fireEvent.click(button())
    await settle()

    expect(signals[0]?.aborted).toBe(true) // the finished one, harmlessly
    expect(signals[1]?.aborted).toBe(false)
  })

  test('the clock stops once the report is in', async () => {
    await coach()
    await finish()
    const timers = vi.getTimerCount()

    await settle(10_000)

    expect(timers).toBe(0) // no ticking left behind
  })
})
