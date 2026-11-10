// Unit tests for src/api.ts. fetch is replaced with a fake that records each request and
// answers with a chosen response; src/api.live.test.ts calls the real API instead.
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'

import { API_BASE, ApiError, getHealth, getPlayer, getReplay, uploadReplay } from './api'

type Call = { url: string; init?: RequestInit }
let calls: Call[]
let answer: () => Response | Promise<Response>

beforeEach(() => {
  calls = []
  answer = () => Response.json({ ok: true })
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init })
      return answer()
    }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

// ---------- Requests ----------

describe('requests', () => {
  test.each([
    ['getHealth', () => getHealth(), '/api/health'],
    ['getPlayer', () => getPlayer('peppy'), '/api/players/peppy'],
    // Names can have spaces and other characters a URL path can't hold as they are.
    ['getPlayer with a space', () => getPlayer('Hello World'), '/api/players/Hello%20World'],
    ['getPlayer with a slash', () => getPlayer('a/b'), '/api/players/a%2Fb'],
    ['getPlayer with a ?', () => getPlayer('who?'), '/api/players/who%3F'],
    ['getReplay', () => getReplay(42), '/api/replays/42'],
  ])('%s asks for %s', async (_name, call, url) => {
    await call()

    expect(calls).toHaveLength(1)
    expect(calls[0].url).toBe(url)
    expect(calls[0].init?.method ?? 'GET').toBe('GET')
  })

  test('every path starts with the proxied /api', () => {
    expect(API_BASE).toBe('/api')
  })

  test('uploadReplay posts the file as the "file" form field', async () => {
    const file = new File([new Uint8Array([1, 2, 3])], 'play.osr')

    await uploadReplay(file)

    expect(calls[0].url).toBe('/api/replays/upload')
    expect(calls[0].init?.method).toBe('POST')
    const body = calls[0].init?.body
    expect(body).toBeInstanceOf(FormData)
    const sent = (body as FormData).get('file')
    expect(sent).toBeInstanceOf(File)
    expect((sent as File).name).toBe('play.osr')
    // No Content-Type set by hand: the browser adds multipart's boundary itself.
    expect(calls[0].init?.headers).toBeUndefined()
  })

  test('the JSON body is returned as it is', async () => {
    answer = () => Response.json({ replay: { id: 7 }, status: 'pending' })

    expect(await getReplay(7)).toEqual({ replay: { id: 7 }, status: 'pending' })
  })
})

// ---------- Errors ----------

describe('errors', () => {
  test.each([
    // FastAPI's own errors: {"detail": "..."}.
    [404, () => Response.json({ detail: 'replay 9 not found' }, { status: 404 }), 'replay 9 not found'],
    [422, () => Response.json({ detail: 'not a valid .osr file (cut short or damaged)' }, { status: 422 }),
     'not a valid .osr file (cut short or damaged)'],
    // Field validation errors: a list; the first one's message.
    [422, () => Response.json({ detail: [{ loc: ['body', 'file'], msg: 'Field required' }] }, { status: 422 }),
     'Field required'],
    // Not FastAPI's shape, or not JSON at all (a proxy's error page): the status text.
    [502, () => new Response('<html>Bad Gateway</html>', { status: 502, statusText: 'Bad Gateway' }), 'Bad Gateway'],
    [500, () => Response.json({ error: 'x' }, { status: 500, statusText: 'Internal Server Error' }),
     'Internal Server Error'],
    [503, () => Response.json({ detail: [] }, { status: 503, statusText: 'Service Unavailable' }),
     'Service Unavailable'],
    [504, () => Response.json({ detail: 5 }, { status: 504, statusText: 'Gateway Timeout' }), 'Gateway Timeout'],
    // No status text either (HTTP/2 has none): still a readable message.
    [500, () => new Response('', { status: 500 }), 'request failed (500)'],
  ])('%i gives an ApiError: %#', async (status, response, message) => {
    answer = response

    const error = await getReplay(9).catch((e: unknown) => e)

    expect(error).toBeInstanceOf(ApiError)
    expect(error).toBeInstanceOf(Error)
    expect((error as ApiError).status).toBe(status)
    expect((error as ApiError).message).toBe(message)
    expect((error as ApiError).name).toBe('ApiError')
  })

  test('a network failure passes through as it is', async () => {
    answer = () => Promise.reject(new TypeError('Failed to fetch'))

    await expect(getHealth()).rejects.toThrow(new TypeError('Failed to fetch'))
  })

  test.each([200, 201, 299])('%i is a success', async (status) => {
    answer = () => Response.json({ status: 'ok' }, { status })

    await expect(getHealth()).resolves.toEqual({ status: 'ok' })
  })
})
