// Unit tests for the placeholder page: it checks the API on load and says what it found.
// fetch is replaced with a fake.
import { render, screen } from '@testing-library/react'
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

test('the page has its title', () => {
  fakeFetch(() => new Promise(() => {})) // never answers

  render(<App />)

  expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('osu! coach')
})

test('it says it is checking until the API answers', () => {
  fakeFetch(() => new Promise(() => {}))

  render(<App />)

  expect(screen.getByRole('status')).toHaveTextContent('Checking the API...')
})

test('it asks the API once, through the proxy', () => {
  const fetch = fakeFetch(() => new Promise(() => {}))

  render(<App />)

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

  render(<App />)

  const status = await screen.findByText(new RegExp(text))
  expect(status).toHaveAttribute('role', 'status')
  if (text === 'API unreachable') {
    expect(status).toHaveTextContent('is the backend running on port 8000?')
    expect(status).toHaveClass('down')
  } else {
    expect(status).toHaveClass('ok')
  }
})
