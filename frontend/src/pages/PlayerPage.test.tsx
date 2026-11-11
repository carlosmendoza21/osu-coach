// Unit tests for the profile page, at /players/:username. fetch is replaced with a fake
// that answers each request when the test says so.
import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'

import App from '../App'
import { playerResponse } from '../test/fixtures'

// Requests waiting for an answer, by URL; answer() settles them.
let waiting: { url: string; reply: (r: Response) => void; fail: (e: unknown) => void }[]

beforeEach(() => {
  waiting = []
  vi.stubGlobal(
    'fetch',
    vi.fn(
      (url: string) =>
        new Promise<Response>((reply, fail) => {
          waiting.push({ url, reply, fail })
        }),
    ),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function at(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  )
}

async function answer(url: string, response: Response | Error) {
  const i = waiting.findIndex((w) => w.url === url)
  expect(i, `no request waiting for ${url}`).toBeGreaterThanOrEqual(0)
  const [request] = waiting.splice(i, 1)
  await act(async () => {
    if (response instanceof Error) request.fail(response)
    else request.reply(response)
  })
}

const PEPPY = '/api/players/peppy'

test('it asks the API for the player in the URL', () => {
  at('/players/peppy')

  expect(waiting.map((w) => w.url)).toEqual([PEPPY])
})

test('the name is escaped in the request', () => {
  at('/players/Hello%20World')

  expect(waiting.map((w) => w.url)).toEqual(['/api/players/Hello%20World'])
})

test('it says it is loading until the answer comes', () => {
  at('/players/peppy')

  expect(screen.getByRole('status')).toHaveTextContent('Loading peppy...')
  expect(screen.getByRole('status')).toHaveTextContent('can take a few seconds')
})

test('the profile', async () => {
  at('/players/peppy')
  await answer(PEPPY, Response.json(playerResponse()))

  const profile = screen.getByRole('region', { name: 'Profile' })
  expect(profile).toHaveTextContent('peppy')
  expect(profile).toHaveTextContent('AU')
  expect(profile).toHaveTextContent('Global rank#817,157')
  expect(profile).toHaveTextContent('Performance1,184pp')
  expect(profile).toHaveTextContent('Accuracy96.85%')
  expect(profile).toHaveTextContent('Play count7,816')
  expect(profile).toHaveTextContent('Synced from osu! 2 minutes ago.')
  const name = screen.getByRole('link', { name: 'peppy' })
  expect(name).toHaveAttribute('href', 'https://osu.ppy.sh/users/2')
})

test('an inactive player is unranked', async () => {
  at('/players/peppy')
  await answer(PEPPY, Response.json(playerResponse({ global_rank: null })))

  expect(screen.getByRole('region', { name: 'Profile' })).toHaveTextContent('Global rankunranked')
})

test('the top plays and the recent plays', async () => {
  at('/players/peppy')
  await answer(PEPPY, Response.json(playerResponse()))

  const best = screen.getByRole('table', { name: 'Top plays (2)' })
  const recent = screen.getByRole('table', { name: 'Recent plays (1)' })
  expect(best.querySelectorAll('tbody tr')).toHaveLength(2)
  expect(recent.querySelectorAll('tbody tr')).toHaveLength(1)
  expect(recent.querySelector('tbody tr')).toHaveClass('failed')
})

test.each([
  [404, { detail: "osu! player 'peppy' not found" }, 'No osu! player is called "peppy".'],
  [502, { detail: 'osu! request failed' }, "osu! didn't answer. Try again in a minute."],
  [504, { detail: 'syncing from osu! timed out' }, 'Syncing from osu! took too long.'],
  [500, { detail: 'player missing after sync' }, 'Something went wrong: player missing after sync'],
])('a %i is explained', async (status, body, message) => {
  at('/players/peppy')
  await answer(PEPPY, Response.json(body, { status }))

  expect(screen.getByRole('alert')).toHaveTextContent(message)
  expect(screen.queryByRole('region', { name: 'Profile' })).toBeNull()
})

test('no answer at all is explained', async () => {
  at('/players/peppy')
  await answer(PEPPY, new TypeError('Failed to fetch'))

  expect(screen.getByRole('alert')).toHaveTextContent("Couldn't reach the API.")
})

test('searching another player shows them, not the last one', async () => {
  const user = userEvent.setup()
  at('/players/peppy')
  await answer(PEPPY, Response.json(playerResponse()))

  await user.type(screen.getByRole('textbox', { name: 'osu! username' }), 'mrekk{Enter}')

  // Loading the new player: peppy's profile is gone at once.
  expect(screen.getByRole('status')).toHaveTextContent('Loading mrekk...')
  expect(screen.queryByRole('region', { name: 'Profile' })).toBeNull()
  await answer('/api/players/mrekk', Response.json(playerResponse({ username: 'mrekk' })))
  expect(screen.getByRole('link', { name: 'mrekk' })).toBeInTheDocument()
})

test("a late answer for the previous player doesn't replace the new one", async () => {
  const user = userEvent.setup()
  at('/players/peppy')
  // peppy's answer hasn't come yet when the reader searches for mrekk.
  await user.type(screen.getByRole('textbox', { name: 'osu! username' }), 'mrekk{Enter}')
  await answer('/api/players/mrekk', Response.json(playerResponse({ username: 'mrekk' })))

  await answer(PEPPY, Response.json(playerResponse()))

  expect(screen.getByRole('link', { name: 'mrekk' })).toBeInTheDocument()
  expect(screen.queryByRole('link', { name: 'peppy' })).toBeNull()
})

test("a late error for the previous player doesn't replace the new one", async () => {
  const user = userEvent.setup()
  at('/players/nobody')
  await user.type(screen.getByRole('textbox', { name: 'osu! username' }), 'peppy{Enter}')
  await answer(PEPPY, Response.json(playerResponse()))

  await answer('/api/players/nobody', Response.json({ detail: 'not found' }, { status: 404 }))

  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.getByRole('link', { name: 'peppy' })).toBeInTheDocument()
})
