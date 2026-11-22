import { Suspense, lazy } from 'react'

import { ApiError, getSkills } from '../api'
import { usePollUntilReady } from '../hooks/usePollUntilReady'
import { SKILL_LABELS, SKILL_ORDER, playsNote } from '../skills'

// The radar (and Recharts with it) loads only once there's a profile to show.
const SkillsRadar = lazy(() => import('./SkillsRadar'))

export const SKILLS_POLL_MS = 5000 // how often to check for a profile still being scored
// A new player's first scoring waits on every map's download and attributes, one request
// a second with osu!'s rate limit: about 3 minutes for 50 new maps.
export const SKILLS_GIVE_UP_MS = 5 * 60_000

function skillsError(error: unknown): string {
  if (error instanceof ApiError && error.status === 404) return 'No skill profile for this player.'
  return "The skills couldn't be loaded."
}

/** A player's skill profile: a radar of the six skills, their strongest and weakest.
 * Checked again while it's still being scored (a player's first sync). */
export default function SkillsPanel({ osuId }: { osuId: number }) {
  const load = usePollUntilReady(osuId, getSkills, SKILLS_POLL_MS, SKILLS_GIVE_UP_MS)

  let body
  if (load.state === 'loading') {
    body = (
      <p role="status" className="muted">
        Loading the skills...
      </p>
    )
  } else if (load.state === 'waiting') {
    body = load.gaveUp ? (
      <p className="muted">The skills aren't scored yet. Reload the page in a few minutes.</p>
    ) : (
      <p role="status" className="muted">
        Scoring skills from this player's plays... (a new player takes a few minutes)
      </p>
    )
  } else if (load.state === 'error') {
    body = <p className="error">{skillsError(load.error)}</p>
  } else {
    const skills = load.data
    body = (
      <>
        <p className="skills-summary">
          Strongest: <strong>{SKILL_LABELS[skills.strongest]}</strong> · Weakest:{' '}
          <strong>{SKILL_LABELS[skills.weakest]}</strong>{' '}
          <span className="muted">· {playsNote(skills)}</span>
        </p>
        <div className="skills-body">
          <Suspense fallback={<p className="muted">Loading the chart...</p>}>
            <SkillsRadar skills={skills} />
          </Suspense>
          <dl className="skills-scores">
            {SKILL_ORDER.map((name) => (
              <div key={name}>
                <dt>{SKILL_LABELS[name]}</dt>
                <dd>{skills[name].toFixed(1)}</dd>
              </div>
            ))}
          </dl>
        </div>
      </>
    )
  }
  return (
    <section className="skills" aria-label="Skills">
      <h3>
        Skills <span className="muted">(0-100, from top plays)</span>
      </h3>
      {body}
    </section>
  )
}
