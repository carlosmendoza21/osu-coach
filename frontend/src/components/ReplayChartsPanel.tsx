import { useEffect, useState } from 'react'

import { getReplayCharts } from '../api'
import type { ReplayCharts } from '../types'
import Heatmap from './Heatmap'
import HitErrorChart from './HitErrorChart'
import UrChart from './UrChart'

type Load = { id: number; state: 'done'; charts: ReplayCharts } | { id: number; state: 'error' }

/** An analyzed replay's three charts, loaded on their own after the analysis shows. */
export default function ReplayChartsPanel({ id }: { id: number }) {
  const [load, setLoad] = useState<Load | null>(null)

  useEffect(() => {
    let ignore = false
    getReplayCharts(id)
      .then((charts) => {
        if (!ignore) setLoad({ id, state: 'done', charts })
      })
      .catch(() => {
        if (!ignore) setLoad({ id, state: 'error' })
      })
    return () => {
      ignore = true
    }
  }, [id])

  if (load === null || load.id !== id) {
    return (
      <p role="status" className="muted">
        Loading the charts...
      </p>
    )
  }
  if (load.state === 'error') {
    // The analysis above still stands; only the charts are missing.
    return <p className="error">The charts couldn't be loaded.</p>
  }
  return (
    <section className="charts" aria-label="Charts">
      <HitErrorChart charts={load.charts} />
      <UrChart charts={load.charts} />
      <Heatmap charts={load.charts} />
    </section>
  )
}
