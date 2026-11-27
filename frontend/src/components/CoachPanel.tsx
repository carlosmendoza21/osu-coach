import { useEffect, useRef, useState } from 'react'

import { ApiError, streamReport } from '../api'
import { formatLength } from '../format'
import { SKILL_LABELS } from '../skills'
import type { CoachReport, SkillNote } from '../types'

// Where the coach is: not asked yet, working (with what it has done so far), done, or failed.
type Run =
  | { state: 'idle' }
  | { state: 'running'; steps: string[]; started: number }
  | { state: 'done'; report: CoachReport }
  | { state: 'error'; message: string }

export const TICK_MS = 1000 // how often the running time updates

function coachError(error: unknown): string {
  if (error instanceof ApiError && error.status === 409) {
    return "The coach needs this player's skills first: they're still being scored. Try again in a few minutes."
  }
  if (error instanceof ApiError && error.status === 404) return 'There is no such player to coach.'
  return "The coach couldn't write a report. Try again."
}

/** Who wrote the report's words, and why the template's were used if a model was tried. */
function Byline({ report }: { report: CoachReport }) {
  if (report.source === 'llm') {
    return <p className="muted coach-byline">Written by {report.model}, from this player's numbers.</p>
  }
  return (
    <p className="muted coach-byline">
      From templates, with this player's numbers
      {report.fallback_reason && (
        <>
          {' '}
          ({report.model} was tried, but its words weren't used: {report.fallback_reason})
        </>
      )}
      .
    </p>
  )
}

function Note({ note }: { note: SkillNote }) {
  return (
    <li>
      <strong>{SKILL_LABELS[note.skill]}</strong> <span className="muted">{note.score.toFixed(1)}/100</span>
      {note.explanation && <p className="coach-explanation">{note.explanation}</p>}
      <ul className="coach-evidence">
        {note.evidence.map((e) => (
          <li key={e}>{e}</li>
        ))}
      </ul>
    </li>
  )
}

function Report({ report }: { report: CoachReport }) {
  return (
    <>
      <Byline report={report} />
      <p className="coach-summary">{report.summary}</p>
      <h4>Weaknesses</h4>
      <ol className="coach-notes">
        {report.weaknesses.map((n) => (
          <Note key={n.skill} note={n} />
        ))}
      </ol>
      <h4>Strengths</h4>
      <ul className="coach-notes">
        {report.strengths.map((n) => (
          <Note key={n.skill} note={n} />
        ))}
      </ul>
      <h4>Practice plan</h4>
      <ol className="coach-plan">
        {report.plan.map((p) => (
          <li key={p.skill}>{p.text}</li>
        ))}
      </ol>
      {report.maps.length > 0 && (
        <p>
          <a href="#practice-maps">The practice maps below</a> are picked for {report.username}'s weakest
          skill, {SKILL_LABELS[report.weaknesses[0].skill].toLowerCase()}.
        </p>
      )}
    </>
  )
}

/** "Coach me": asks the coach for a report on the player and shows it working, then the
 * report. A report from a local model takes minutes, so each step shows as it happens,
 * with the time so far. Leaving the page stops it. */
export default function CoachPanel({ osuId }: { osuId: number }) {
  const [run, setRun] = useState<Run>({ state: 'idle' })
  const [now, setNow] = useState(() => Date.now())
  const abort = useRef<AbortController | null>(null)

  // Leaving the page (or another player) stops the report being written.
  useEffect(() => () => abort.current?.abort(), [])

  // The running time, while it runs.
  const running = run.state === 'running'
  useEffect(() => {
    if (!running) return
    const timer = setInterval(() => setNow(Date.now()), TICK_MS)
    return () => clearInterval(timer)
  }, [running])

  async function coach() {
    abort.current?.abort()
    const controller = new AbortController()
    abort.current = controller
    const started = Date.now()
    setNow(started)
    setRun({ state: 'running', steps: [], started })
    try {
      let finished = false
      await streamReport(
        osuId,
        (event) => {
          if (event.type === 'step') {
            setRun((r) => (r.state === 'running' ? { ...r, steps: [...r.steps, event.text] } : r))
          } else if (event.type === 'report') {
            finished = true
            setRun({ state: 'done', report: event.report })
          } else {
            finished = true
            setRun({ state: 'error', message: "The coach couldn't write a report. Try again." })
          }
        },
        controller.signal,
      )
      if (!finished) setRun({ state: 'error', message: 'The report stopped before it was done. Try again.' })
    } catch (error: unknown) {
      if (controller.signal.aborted) return // the page moved on: nothing to show
      setRun({ state: 'error', message: coachError(error) })
    }
  }

  let body
  if (run.state === 'idle') {
    body = (
      <p className="muted">
        A report on this player's strengths and weaknesses, with evidence and a practice plan.
      </p>
    )
  } else if (run.state === 'running') {
    const last = run.steps.length > 0 ? run.steps[run.steps.length - 1] : 'Starting'
    body = (
      <>
        <p role="status">
          {last}... <span className="muted">{formatLength((now - run.started) / 1000)}</span>
        </p>
        {run.steps.length > 1 && (
          <ul className="coach-steps muted">
            {run.steps.slice(0, -1).map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ul>
        )}
        <p className="muted">A local model can take a few minutes.</p>
      </>
    )
  } else if (run.state === 'error') {
    body = (
      <p role="alert" className="error">
        {run.message}
      </p>
    )
  } else {
    body = <Report report={run.report} />
  }

  return (
    <section className="coach" aria-label="Coach">
      <h3>Coach</h3>
      <button type="button" onClick={() => void coach()} disabled={running}>
        {run.state === 'idle' ? 'Coach me' : running ? 'Coaching...' : 'Coach me again'}
      </button>
      {body}
    </section>
  )
}
