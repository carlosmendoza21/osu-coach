// Unit tests for the replay page, at /replays/:id. fetch is replaced with a fake that
// answers from a list, and timers are fake, so minutes of checking take no time.
import { act, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'

import { pendingDetail, replayDetail } from '../test/fixtures'
import ReplayPage, { GIVE_UP_MS, POLL_MS } from './ReplayPage'

let urls: string[]
let answers: (() => Promise<Response>)[] // used in order; the last one repeats

beforeEach(() => {
  vi.useFakeTimers()
  urls = []
  answers = [async () => Response.json(replayDetail())]
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) => {
      // The charts ask for their own data once a replay is analyzed: never answered
      // here (src/components/ReplayChartsPanel.test.tsx covers them), and not counted.
      if (url.endsWith('/charts')) return new Promise<Response>(() => {})
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

const pending = (withMap = true) => async () => Response.json(pendingDetail(withMap))
const analyzed = async () => Response.json(replayDetail())

/** Render the page and let the first request finish. */
async function at(path: string) {
  const view = render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/replays/:id" element={<ReplayPage />} />
      </Routes>
    </MemoryRouter>,
  )
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
  return view
}

async function wait(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

test('it asks for the replay in the URL', async () => {
  await at('/replays/2')

  expect(urls).toEqual(['/api/replays/2'])
})

test('it says it is loading until the first answer', () => {
  answers = [() => new Promise(() => {})]
  render(
    <MemoryRouter initialEntries={['/replays/2']}>
      <Routes>
        <Route path="/replays/:id" element={<ReplayPage />} />
      </Routes>
    </MemoryRouter>,
  )

  expect(screen.getByRole('status')).toHaveTextContent('Loading replay 2...')
})

test('an analyzed replay is shown at once, and not asked for again', async () => {
  await at('/replays/2')
  await wait(10 * POLL_MS)

  expect(screen.getByRole('region', { name: 'Play' })).toHaveTextContent('Played by mrekk')
  expect(screen.getByRole('region', { name: 'Summary' })).toHaveTextContent('UR66.9')
  expect(screen.getByRole('table', { name: 'By pattern' })).toBeInTheDocument()
  expect(urls).toHaveLength(1)
})

test.each([
  [false, 'Fetching the map from osu!...'],
  [true, 'Analyzing the replay...'],
])('pending (map stored: %s) says what is happening', async (withMap, text) => {
  answers = [pending(withMap)]
  await at('/replays/2')

  expect(screen.getByRole('status')).toHaveTextContent(text)
  expect(screen.getByRole('status')).toHaveTextContent('This page updates by itself.')
  expect(screen.queryByRole('region', { name: 'Summary' })).toBeNull()
})

test('pending: asked again every 2 seconds until analyzed, then no more', async () => {
  answers = [pending(false), pending(true), pending(true), analyzed]
  await at('/replays/2')
  expect(urls).toHaveLength(1)

  await wait(POLL_MS - 1)
  expect(urls).toHaveLength(1) // not before 2 s
  await wait(1)
  expect(urls).toHaveLength(2)
  expect(screen.getByRole('status')).toHaveTextContent('Analyzing the replay...') // the map came

  await wait(2 * POLL_MS)
  expect(urls).toHaveLength(4)
  expect(screen.getByRole('region', { name: 'Summary' })).toHaveTextContent('UR66.9')

  await wait(10 * POLL_MS)
  expect(urls).toHaveLength(4) // analyzed: no more requests
})

test('after 90 seconds still pending, it stops and explains', async () => {
  answers = [pending(true)]
  await at('/replays/2')

  await wait(GIVE_UP_MS)
  const asked = urls.length
  expect(screen.getByRole('alert')).toHaveTextContent('taking longer than expected')
  expect(screen.getByRole('link', { name: 'uploading it' })).toHaveAttribute('href', '/replays/new')

  await wait(10 * POLL_MS)
  expect(urls).toHaveLength(asked) // stopped asking
  expect(asked).toBe(GIVE_UP_MS / POLL_MS + 1) // every 2 s for 90 s, plus the first
})

test('leaving the page stops the checking', async () => {
  answers = [pending(true)]
  const { unmount } = await at('/replays/2')

  unmount()
  await wait(10 * POLL_MS)

  expect(urls).toHaveLength(1)
})

test.each([
  [404, { detail: 'replay 2 not found' }, "There's no replay 2."],
  [500, { detail: 'boom' }, 'Something went wrong: boom'],
])('a %i is explained', async (status, body, message) => {
  answers = [async () => Response.json(body, { status })]
  await at('/replays/2')

  expect(screen.getByRole('alert')).toHaveTextContent(message)
})

test('an error while checking stops the checking and is explained', async () => {
  answers = [pending(true), () => Promise.reject(new TypeError('Failed to fetch'))]
  await at('/replays/2')

  await wait(POLL_MS)
  expect(screen.getByRole('alert')).toHaveTextContent("Couldn't reach the API.")
  await wait(10 * POLL_MS)
  expect(urls).toHaveLength(2)
})

test.each(['abc', '1.5', '-1', '2x'])('an id that is not a number (%s) is not asked for', async (id) => {
  await at(`/replays/${id}`)

  expect(screen.getByRole('alert')).toHaveTextContent(`There's no replay ${id}.`)
  expect(urls).toEqual([])
})

function GoTo({ path }: { path: string }) {
  const navigate = useNavigate()
  return (
    <button type="button" onClick={() => void navigate(path)}>
      go
    </button>
  )
}

test("moving to another replay drops the last one, even if its answer comes late", async () => {
  // Replay 2 is still pending when the reader moves to replay 3.
  let replyFor2: (r: Response) => void = () => {}
  answers = [
    () => new Promise((reply) => (replyFor2 = reply)),
    async () => Response.json(replayDetail({ replay: { ...replayDetail().replay, id: 3, player_name: 'gwb' } })),
  ]
  render(
    <MemoryRouter initialEntries={['/replays/2']}>
      <GoTo path="/replays/3" />
      <Routes>
        <Route path="/replays/:id" element={<ReplayPage />} />
      </Routes>
    </MemoryRouter>,
  )
  await wait(0)

  fireEvent.click(screen.getByRole('button', { name: 'go' }))
  await wait(0)
  expect(urls).toEqual(['/api/replays/2', '/api/replays/3'])
  expect(screen.getByRole('region', { name: 'Play' })).toHaveTextContent('Played by gwb')

  replyFor2(Response.json(replayDetail())) // replay 2's answer, after all
  await wait(0)
  expect(screen.getByRole('region', { name: 'Play' })).toHaveTextContent('Played by gwb')
})

test("while the next replay loads, the last one isn't shown", async () => {
  answers = [async () => Response.json(replayDetail()), () => new Promise(() => {})]
  render(
    <MemoryRouter initialEntries={['/replays/2']}>
      <GoTo path="/replays/3" />
      <Routes>
        <Route path="/replays/:id" element={<ReplayPage />} />
      </Routes>
    </MemoryRouter>,
  )
  await wait(0)
  expect(screen.getByRole('region', { name: 'Play' })).toHaveTextContent('Played by mrekk')

  fireEvent.click(screen.getByRole('button', { name: 'go' }))
  await wait(0)

  // Replay 3 hasn't answered: replay 2's play is gone, and loading is said.
  expect(screen.getByRole('status')).toHaveTextContent('Loading replay 3...')
  expect(screen.queryByRole('region', { name: 'Play' })).toBeNull()
})
