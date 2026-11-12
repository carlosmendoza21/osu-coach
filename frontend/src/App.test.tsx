// Unit tests for the placeholder page: it checks the API on load and says what it found.
// fetch is replaced with a fake.
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, expect, test, vi } from 'vitest'

import App from './App'

afterEach(() => {
  vi.unstubAllGlobals()
})

function fakeFetch(answer: () => Promise<Response>) {
  const fn = vi.fn((_url: string) => answer())
  vi.stubGlobal('fetch', fn)
  return fn
}

test('the header links home', () => {
  fakeFetch(() => new Promise(() => {})) // never answers

  render(
    <MemoryRouter>
      <App />
    </MemoryRouter>,
  )

  expect(screen.getByRole('link', { name: 'osu! coach' })).toHaveAttribute('href', '/')
})

test('it says it is checking until the API answers', () => {
  fakeFetch(() => new Promise(() => {}))

  render(
    <MemoryRouter>
      <App />
    </MemoryRouter>,
  )

  expect(screen.getByRole('status')).toHaveTextContent('Checking the API...')
})

test('it asks the API once, through the proxy', () => {
  const fetch = fakeFetch(() => new Promise(() => {}))

  render(
    <MemoryRouter>
      <App />
    </MemoryRouter>,
  )

  expect(fetch).toHaveBeenCalledTimes(1)
  expect(fetch.mock.calls[0][0]).toBe('/api/health')
})

test.each([
  ['ok', async () => Response.json({ status: 'ok' }), 'API connected'],
  // The API answered, but not with "ok".
  ['not ok', async () => Response.json({ status: 'degraded' }), 'API unreachable'],
  // The proxy can't reach the backend: Vite answers 500 or 502 by itself.
  ['proxy error', async () => new Response('', { status: 502 }), 'API unreachable'],
  // No answer at all (the dev server is down).
  ['network error', async () => Promise.reject(new TypeError('Failed to fetch')), 'API unreachable'],
])('when the API is %s, it says so', async (_name, answer, text) => {
  fakeFetch(answer)

  render(
    <MemoryRouter>
      <App />
    </MemoryRouter>,
  )

  const status = await screen.findByText(new RegExp(text))
  expect(status).toHaveAttribute('role', 'status')
  if (text === 'API unreachable') {
    expect(status).toHaveTextContent('is the backend running on port 8000?')
    expect(status).toHaveClass('down')
  } else {
    expect(status).toHaveClass('ok')
  }
})

// ---------- Routes ----------

test.each([
  ['/', 'API'],
  ['/players/peppy', 'Loading peppy...'],
  ['/nowhere', "This page doesn't exist."],
  ['/players', "This page doesn't exist."], // a player page needs a name
])('%s shows its page', (path, text) => {
  fakeFetch(() => new Promise(() => {}))

  render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  )

  expect(screen.getByText(text, { exact: false })).toBeInTheDocument()
})

test.each(['/', '/players/peppy', '/nowhere'])('every page has the header search (%s)', (path) => {
  fakeFetch(() => new Promise(() => {}))

  render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  )

  expect(screen.getByRole('search')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'osu! coach' })).toHaveAttribute('href', '/')
})

test.each([
  ['/replays/new', 'Upload a replay'],
  ['/replays/2', 'Loading replay 2...'],
])('%s shows its page', (path, text) => {
  fakeFetch(() => new Promise(() => {}))

  render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  )

  expect(screen.getByRole('main')).toHaveTextContent(text)
})

test.each(['/', '/players/peppy', '/replays/2'])('every page links to the upload page (%s)', (path) => {
  fakeFetch(() => new Promise(() => {}))

  render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  )

  expect(screen.getByRole('link', { name: 'Upload a replay' })).toHaveAttribute('href', '/replays/new')
})
