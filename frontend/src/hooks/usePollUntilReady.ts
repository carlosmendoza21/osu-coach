import { useEffect, useState } from 'react'

import { ApiError } from '../api'

/** Where a polled request stands. */
export type Polled<T> =
  | { state: 'loading' } // no answer yet for this id
  | { state: 'waiting'; gaveUp: boolean } // the API said 409: not ready yet
  | { state: 'error'; error: unknown } // any other failure: not asked again
  | { state: 'done'; data: T }

// The latest answer, and which id it was for (kept apart: callers don't see the id).
type Load<T> = { id: number; polled: Polled<T> }

/** Ask `request(id)` until the API stops saying "not ready yet" (409): again every
 * `pollMs`, giving up after `giveUpMs`. Any other error ends it. Changing `id` starts
 * over, and an answer for an earlier id is never shown. `request` must not change
 * between renders (a function from api.ts, not one made inline). */
export function usePollUntilReady<T>(
  id: number,
  request: (id: number) => Promise<T>,
  pollMs: number,
  giveUpMs: number,
): Polled<T> {
  const [load, setLoad] = useState<Load<T> | null>(null)

  useEffect(() => {
    // `ignore` drops answers that arrive after the id changed or the page was left.
    let ignore = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const started = Date.now()

    async function check() {
      try {
        const data = await request(id)
        if (!ignore) setLoad({ id, polled: { state: 'done', data } })
      } catch (error: unknown) {
        if (ignore) return
        if (error instanceof ApiError && error.status === 409) {
          // Not ready: look again shortly, unless it's been too long.
          const gaveUp = Date.now() - started >= giveUpMs
          setLoad({ id, polled: { state: 'waiting', gaveUp } })
          if (!gaveUp) timer = setTimeout(() => void check(), pollMs)
        } else {
          setLoad({ id, polled: { state: 'error', error } })
        }
      }
    }

    void check()
    return () => {
      ignore = true
      clearTimeout(timer)
    }
  }, [id, request, pollMs, giveUpMs])

  // A result for another id doesn't count: still loading this one.
  if (load === null || load.id !== id) return { state: 'loading' }
  return load.polled
}
