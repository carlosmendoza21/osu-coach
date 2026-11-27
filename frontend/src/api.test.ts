// Unit tests for src/api.ts. fetch is replaced with a fake that records each request and
// answers with a chosen response; src/api.live.test.ts calls the real API instead.
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'

import {
  API_BASE,
  ApiError,
  getHealth,
  getPlayer,
  getRecommendations,
  getReplay,
  getSkills,
  streamReport,
  uploadReplay,
} from './api'
import type { CoachEvent } from './types'

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
    ['getSkills', () => getSkills(7562902), '/api/players/7562902/skills'],
    ['getRecommendations', () => getRecommendations(2), '/api/players/2/recommendations'],
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

// ---------- The coaching report as a stream ----------

/** A response whose body arrives in these chunks, as a stream does. */
function streamed(chunks: string[], init: ResponseInit = {}): Response {
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  })
  return new Response(body, { headers: { 'Content-Type': 'text/event-stream' }, ...init })
}

const STEP = 'data: {"type": "step", "text": "Looking up your skills"}\n\n'
const REPORT = 'data: {"type": "report", "report": {"username": "peppy"}}\n\n'

async function collect(osuId = 2, signal?: AbortSignal): Promise<CoachEvent[]> {
  const events: CoachEvent[] = []
  await streamReport(osuId, (e) => events.push(e), signal)
  return events
}

describe('streamReport', () => {
  test('a POST that asks for a stream', async () => {
    answer = () => streamed([REPORT])

    await collect(7562902)

    expect(calls[0].url).toBe('/api/players/7562902/report')
    expect(calls[0].init?.method).toBe('POST')
    expect(calls[0].init?.headers).toEqual({ Accept: 'text/event-stream' })
  })

  test('each event, in order', async () => {
    answer = () => streamed([STEP, REPORT])

    expect(await collect()).toEqual([
      { type: 'step', text: 'Looking up your skills' },
      { type: 'report', report: { username: 'peppy' } },
    ])
  })

  test('events split across chunks come through whole', async () => {
    // Edge: the network splits the stream anywhere, even mid-event and mid-character.
    const all = STEP + REPORT
    answer = () => streamed([all.slice(0, 7), all.slice(7, 60), all.slice(60, 61), all.slice(61)])

    expect((await collect()).map((e) => e.type)).toEqual(['step', 'report'])
  })

  test('a character split between chunks is decoded whole', async () => {
    // "★" is three bytes: a chunk boundary inside it must not garble it.
    const event = 'data: {"type": "step", "text": "5.1★"}\n\n'
    const bytes = new TextEncoder().encode(event)
    const at = event.indexOf('★') + 1 // in the middle of its bytes
    answer = () => {
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(bytes.slice(0, at))
          controller.enqueue(bytes.slice(at))
          controller.close()
        },
      })
      return new Response(body)
    }

    expect(await collect()).toEqual([{ type: 'step', text: '5.1★' }])
  })

  test('a last event without its blank line still counts', async () => {
    answer = () => streamed([STEP, 'data: {"type": "report", "report": {}}'])

    expect((await collect()).map((e) => e.type)).toEqual(['step', 'report'])
  })

  test('an empty stream has no events', async () => {
    answer = () => streamed([])

    expect(await collect()).toEqual([])
  })

  test.each([
    [409, "this player's skills aren't scored yet"],
    [404, 'player 2 not found'],
  ])('a %i before the stream is an ApiError with its reason', async (status, detail) => {
    answer = () => Response.json({ detail }, { status })

    const error = await collect().catch((e: unknown) => e)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(status)
    expect((error as ApiError).message).toBe(detail)
  })

  test('the signal is passed on, so the request can be cancelled', async () => {
    answer = () => streamed([REPORT])
    const controller = new AbortController()

    await collect(2, controller.signal)

    expect(calls[0].init?.signal).toBe(controller.signal)
  })
})
