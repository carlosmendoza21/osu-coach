// Unit tests for usePollUntilReady, the waiting the skills and practice maps panels
// share. The request is a fake that answers from a list, and timers are fake, so minutes
// of asking again take no time.
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'

import { ApiError } from '../api'
import { usePollUntilReady } from './usePollUntilReady'

const POLL = 1000
const GIVE_UP = 10_000

let asked: number[]
let answers: (() => Promise<string>)[] // used in order; the last one repeats

// Stable, like a function from api.ts: the hook's effect depends on it.
function request(id: number): Promise<string> {
  asked.push(id)
  const next = answers.length > 1 ? answers.shift()! : answers[0]
  return next()
}

const ok = (value: string) => async () => value
const notReady = async () => Promise.reject(new ApiError(409, 'not ready'))
const failing = (status: number) => async () => Promise.reject(new ApiError(status, 'boom'))

beforeEach(() => {
  vi.useFakeTimers()
  asked = []
  answers = [ok('data')]
})

afterEach(() => {
  vi.useRealTimers()
})

async function wait(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

async function poll(id = 2) {
  const view = renderHook(({ id }) => usePollUntilReady(id, request, POLL, GIVE_UP), {
    initialProps: { id },
  })
  await wait(0)
  return view
}

describe('ready at once', () => {
  test('loading, then the data', async () => {
    answers = [() => new Promise(() => {})]
    const { result } = renderHook(() => usePollUntilReady(2, request, POLL, GIVE_UP))

    expect(result.current).toEqual({ state: 'loading' })
    expect(asked).toEqual([2])
  })

  test('the data, asked for once', async () => {
    const { result } = await poll()
    await wait(10 * POLL)

    expect(result.current).toEqual({ state: 'done', data: 'data' })
    expect(asked).toEqual([2])
  })
})

describe('not ready yet (409)', () => {
  test('waiting, asked again every poll, not sooner', async () => {
    answers = [notReady]
    const { result } = await poll()

    expect(result.current).toEqual({ state: 'waiting', gaveUp: false })
    await wait(POLL - 1)
    expect(asked).toHaveLength(1)
    await wait(1)
    expect(asked).toHaveLength(2)
  })

  test('the data once it is ready, and no more asking', async () => {
    answers = [notReady, notReady, ok('late data')]
    const { result } = await poll()

    await wait(2 * POLL)
    expect(result.current).toEqual({ state: 'done', data: 'late data' })
    await wait(10 * POLL)
    expect(asked).toHaveLength(3)
  })

  test('gives up after giveUpMs, and says so', async () => {
    answers = [notReady]
    const { result } = await poll()

    await wait(GIVE_UP)
    expect(result.current).toEqual({ state: 'waiting', gaveUp: true })
    const count = asked.length
    expect(count).toBe(GIVE_UP / POLL + 1) // one per poll, and the first
    await wait(10 * POLL)
    expect(asked).toHaveLength(count)
  })
})

describe('errors', () => {
  test.each([
    ['404', failing(404)],
    ['500', failing(500)],
    ['no answer', () => Promise.reject(new TypeError('Failed to fetch'))],
  ])('%s: an error, not asked again', async (_name, answer) => {
    answers = [answer]
    const { result } = await poll()

    expect(result.current.state).toBe('error')
    await wait(10 * POLL)
    expect(asked).toHaveLength(1)
  })

  test('the error itself is passed on', async () => {
    answers = [failing(404)]
    const { result } = await poll()

    const current = result.current
    expect(current.state === 'error' && current.error instanceof ApiError && current.error.status).toBe(404)
  })
})

describe('another id, or leaving', () => {
  test('a new id is asked for, loading until its answer', async () => {
    answers = [ok('two'), () => new Promise(() => {})]
    const { result, rerender } = await poll(2)
    expect(result.current).toEqual({ state: 'done', data: 'two' })

    rerender({ id: 3 })

    expect(result.current).toEqual({ state: 'loading' }) // not 2's data
    expect(asked).toEqual([2, 3])
  })

  test("the last id's late answer is never shown", async () => {
    let late: (value: string) => void = () => {}
    answers = [() => new Promise((reply) => (late = reply)), ok('three')]
    const { result, rerender } = await poll(2)

    rerender({ id: 3 })
    await wait(0)
    await act(async () => late('two'))

    expect(result.current).toEqual({ state: 'done', data: 'three' })
  })

  test("the last id's asking stops", async () => {
    answers = [notReady, () => new Promise(() => {})]
    const { rerender } = await poll(2)

    rerender({ id: 3 })
    await wait(10 * POLL)

    expect(asked).toEqual([2, 3])
  })

  test('leaving stops the asking', async () => {
    answers = [notReady]
    const { unmount } = await poll()

    unmount()
    await wait(10 * POLL)

    expect(asked).toHaveLength(1)
  })
})
