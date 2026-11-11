import { useEffect, useState } from 'react'
import { useParams } from 'react-router'

import { getPlayer } from '../api'
import ScoreTable from '../components/ScoreTable'
import { playerError } from '../errors'
import { formatCount, formatPercent, formatPp, formatRank, timeAgo } from '../format'
import type { PlayerResponse } from '../types'

// A finished load, and which username it was for.
type Load =
  | { username: string; state: 'error'; message: string }
  | { username: string; state: 'done'; data: PlayerResponse }

/** /players/:username: a player's profile, top plays and latest plays. */
export default function PlayerPage() {
  const { username = '' } = useParams()
  const [load, setLoad] = useState<Load | null>(null)

  useEffect(() => {
    // `ignore` drops an answer for an earlier username that arrives after the reader
    // has moved on to another player.
    let ignore = false
    getPlayer(username)
      .then((data) => {
        if (!ignore) setLoad({ username, state: 'done', data })
      })
      .catch((error: unknown) => {
        if (!ignore) setLoad({ username, state: 'error', message: playerError(username, error) })
      })
    return () => {
      ignore = true
    }
  }, [username])

  // Still loading this username until a result for it arrives: a result for the
  // previous one doesn't count.
  if (load === null || load.username !== username) {
    return (
      <p role="status" className="muted">
        Loading {username}... (a first look-up syncs from osu! and can take a few seconds)
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

  const { player, best, recent } = load.data
  return (
    <>
      <section className="profile" aria-label="Profile">
        <h2>
          <a href={`https://osu.ppy.sh/users/${player.osu_id}`} target="_blank" rel="noreferrer">
            {player.username}
          </a>{' '}
          <span className="country">{player.country}</span>
        </h2>
        <dl className="stats">
          <div>
            <dt>Global rank</dt>
            <dd>{formatRank(player.global_rank)}</dd>
          </div>
          <div>
            <dt>Performance</dt>
            <dd>{formatPp(player.pp)}</dd>
          </div>
          <div>
            <dt>Accuracy</dt>
            <dd>{formatPercent(player.accuracy)}</dd>
          </div>
          <div>
            <dt>Play count</dt>
            <dd>{formatCount(player.playcount)}</dd>
          </div>
        </dl>
        <p className="muted">Synced from osu! {timeAgo(player.updated_at)}.</p>
      </section>
      <ScoreTable scores={best} caption={`Top plays (${best.length})`} />
      <ScoreTable scores={recent} caption={`Recent plays (${recent.length})`} />
    </>
  )
}
