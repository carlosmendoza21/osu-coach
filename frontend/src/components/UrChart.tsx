import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

import { formatClock, rollingUr } from '../chartdata'
import type { ReplayCharts } from '../types'

export const UR_WINDOW = 50 // hits per point: enough to be steady, small enough to show a section

/** UR along the map: where the timing slipped. Misses are marked on the time axis. */
export default function UrChart({ charts }: { charts: ReplayCharts }) {
  const points = rollingUr(charts.notes, UR_WINDOW)
  const misses = charts.notes.filter((n) => n.result === 'miss')
  // The whole map along the axis: the first UR point only comes after UR_WINDOW hits,
  // and a miss before it must still show.
  const times = charts.notes.map((n) => n.time_ms)
  const span: [number, number] = [Math.min(...times), Math.max(...times)]

  if (points.length === 0) {
    return <p className="muted">Too few hits to show UR over time.</p>
  }
  return (
    <figure className="chart" aria-label="UR over time">
      <figcaption>
        UR over time <span className="muted">(over each {UR_WINDOW} hits; lower is steadier)</span>
      </figcaption>
      <ResponsiveContainer width="100%" height={220}>
        <LineChart data={points} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
          <CartesianGrid strokeOpacity={0.2} />
          <XAxis dataKey="time_ms" type="number" domain={span} tickFormatter={formatClock} />
          <YAxis width={40} domain={[0, 'auto']} />
          {misses.map((m) => (
            <ReferenceLine key={m.idx} x={m.time_ms} stroke="#ff6b6b" strokeOpacity={0.7} />
          ))}
          <Tooltip
            labelFormatter={(t) => formatClock(Number(t))}
            formatter={(value) => [Number(value).toFixed(1), 'UR']}
          />
          <Line dataKey="ur" stroke="#ff66aa" dot={false} strokeWidth={2} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </figure>
  )
}
