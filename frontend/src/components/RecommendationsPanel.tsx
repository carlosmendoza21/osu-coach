import { ApiError, getRecommendations } from '../api'
import { formatLength, formatStars } from '../format'
import { usePollUntilReady } from '../hooks/usePollUntilReady'
import { SKILL_LABELS } from '../skills'
import type { Recommendation, Recommendations } from '../types'

// Made with the skill profile, so they're waited for the same way and as long.
export const RECS_POLL_MS = 5000
export const RECS_GIVE_UP_MS = 5 * 60_000

/** The recommendations, once made. A player scored before recommendations existed
 * answers with none made yet (no target skill) until their next sync makes them, and
 * opening their page starts that sync: so that counts as not ready yet, like a 409. */
async function getMadeRecommendations(osuId: number): Promise<Recommendations> {
  const recs = await getRecommendations(osuId)
  if (recs.target_skill === null) throw new ApiError(409, "practice maps aren't picked yet")
  return recs
}

function recsError(error: unknown): string {
  if (error instanceof ApiError && error.status === 404) return 'No practice maps for this player.'
  return "The practice maps couldn't be loaded."
}

function PracticeMap({ rec }: { rec: Recommendation }) {
  const b = rec.beatmap
  return (
    <li>
      <a href={`https://osu.ppy.sh/b/${b.id}`} target="_blank" rel="noreferrer">
        {b.artist} - {b.title} <span className="version">[{b.version}]</span>
      </a>{' '}
      <span className="muted">by {b.creator}</span>
      <div className="practice-stats">
        <span className="stars">{formatStars(b.star_rating)}</span> · {Math.round(b.bpm)} BPM · AR{' '}
        {b.ar} · OD {b.od} · {formatLength(b.hit_length_s)}
      </div>
      <div className="practice-reason">{rec.reason}</div>
    </li>
  )
}

/** Practice maps for the player's weakest skill, each with why it was picked. Checked
 * again while they're still being picked (a player's first sync). */
export default function RecommendationsPanel({ osuId }: { osuId: number }) {
  const load = usePollUntilReady(osuId, getMadeRecommendations, RECS_POLL_MS, RECS_GIVE_UP_MS)

  let heading = <>Practice maps</>
  let body
  if (load.state === 'loading') {
    body = (
      <p role="status" className="muted">
        Loading the practice maps...
      </p>
    )
  } else if (load.state === 'waiting') {
    body = load.gaveUp ? (
      <p className="muted">The practice maps aren't picked yet. Reload the page in a few minutes.</p>
    ) : (
      <p role="status" className="muted">
        Picking practice maps... (they follow the skills, a few minutes for a new player)
      </p>
    )
  } else if (load.state === 'error') {
    body = <p className="error">{recsError(load.error)}</p>
  } else if (load.data.maps.length === 0) {
    // Made, but no map we have fit (the pool is thin at the extremes).
    body = <p className="muted">No practice maps fit this player yet.</p>
  } else {
    const { target_skill, comfort_stars, maps } = load.data
    heading = (
      <>
        Practice maps for <strong>{target_skill && SKILL_LABELS[target_skill]}</strong>
        {comfort_stars !== null && (
          <span className="muted"> (set from a comfort level of {formatStars(comfort_stars)})</span>
        )}
      </>
    )
    body = (
      <ol className="practice">
        {maps.map((rec) => (
          <PracticeMap key={rec.beatmap.id} rec={rec} />
        ))}
      </ol>
    )
  }
  return (
    <section className="recommendations" aria-label="Practice maps">
      <h3>{heading}</h3>
      {body}
    </section>
  )
}
