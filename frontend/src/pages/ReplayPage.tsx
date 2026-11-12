import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router'

import { getReplay } from '../api'
import ReplayAnalysis, { ReplayHeader } from '../components/ReplayAnalysis'
import { replayError } from '../errors'
import type { ReplayDetail } from '../types'

export const POLL_MS = 2000 // how often to check a pending replay
export const GIVE_UP_MS = 90_000 // a map fetch and analysis take a few seconds, not minutes

// The latest look at a replay, and which id it was for.
type Load =
  | { id: string; state: 'error'; message: string }
  | { id: string; state: 'shown'; detail: ReplayDetail; gaveUp: boolean }

/** /replays/:id: a replay and its analysis, checked again until the analysis is done. */
export default function ReplayPage() {
  const { id = '' } = useParams()
  const [load, setLoad] = useState<Load | null>(null)

  useEffect(() => {
    let ignore = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const started = Date.now()

    async function check() {
      try {
        const detail = await getReplay(Number(id))
        if (ignore) return
        const waiting = detail.status === 'pending'
        const gaveUp = waiting && Date.now() - started >= GIVE_UP_MS
        setLoad({ id, state: 'shown', detail, gaveUp })
        // Still pending: look again shortly, unless it's been too long.
        if (waiting && !gaveUp) timer = setTimeout(() => void check(), POLL_MS)
      } catch (error: unknown) {
        if (!ignore) setLoad({ id, state: 'error', message: replayError(id, error) })
      }
    }

    // An id that isn't a whole number can't be a replay: no request for it.
    if (/^\d+$/.test(id)) void check()
    return () => {
      ignore = true
      clearTimeout(timer)
    }
  }, [id])

  if (!/^\d+$/.test(id)) {
    return (
      <p role="alert" className="error">
        There's no replay {id}.
      </p>
    )
  }
  if (load === null || load.id !== id) {
    return (
      <p role="status" className="muted">
        Loading replay {id}...
      </p>
    )
  }
  if (load.state === 'error') {
    return (
      <p role="alert" className="error">
        {load.message}
      </p>
    )
  }

  const { detail, gaveUp } = load
  return (
    <>
      <ReplayHeader detail={detail} />
      {detail.status === 'analyzed' ? (
        <ReplayAnalysis detail={detail} />
      ) : gaveUp ? (
        <p role="alert" className="error">
          The analysis is taking longer than expected. osu! may be slow, or the map may have
          changed since this replay was played. Try <Link to="/replays/new">uploading it</Link>{' '}
          again later.
        </p>
      ) : (
        <p role="status" className="muted">
          {detail.beatmap ? 'Analyzing the replay...' : 'Fetching the map from osu!...'} This page
          updates by itself.
        </p>
      )}
    </>
  )
}
