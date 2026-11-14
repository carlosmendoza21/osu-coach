import { Bar, BarChart, CartesianGrid, ReferenceArea, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

import { histogram } from '../chartdata'
import type { ReplayCharts } from '../types'

// The windows shaded behind the bars: 300s green, 100s blue-ish, 50s amber.
const WINDOW_COLORS = { great: '#4ac26b', ok: '#5aa9e6', meh: '#f0a43a' }

/** How many hits landed at each timing, early (left) to late (right), over the windows. */
export default function HitErrorChart({ charts }: { charts: ReplayCharts }) {
  const { windows } = charts
  const bins = histogram(charts.notes, windows.meh)
  const edge = bins.length ? -bins[0].from : windows.meh

  return (
    <figure className="chart" aria-label="Hit error histogram">
      <figcaption>
        Hit timing <span className="muted">(ms; negative is early)</span>
      </figcaption>
      <ResponsiveContainer width="100%" height={220}>
        <BarChart data={bins} margin={{ top: 8, right: 16, bottom: 8, left: 0 }} barCategoryGap={0}>
          {(['meh', 'ok', 'great'] as const).map((w) => (
            <ReferenceArea
              key={w}
              x1={-windows[w]}
              x2={windows[w]}
              fill={WINDOW_COLORS[w]}
              fillOpacity={0.12}
              ifOverflow="hidden"
            />
          ))}
          <CartesianGrid vertical={false} strokeOpacity={0.2} />
          <XAxis
            dataKey="mid"
            type="number"
            domain={[-edge, edge]}
            tickFormatter={(v: number) => `${v}`}
          />
          <YAxis allowDecimals={false} width={40} />
          <ReferenceLine x={0} strokeOpacity={0.6} />
          <Tooltip
            formatter={(value) => [value, 'hits']}
            labelFormatter={(mid) => `${Number(mid) - 1} to ${Number(mid) + 1} ms`}
          />
          <Bar dataKey="count" fill="#ff66aa" isAnimationActive={false} />
        </BarChart>
      </ResponsiveContainer>
    </figure>
  )
}
