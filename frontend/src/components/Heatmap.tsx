import { useEffect, useRef } from 'react'

import { MARK_COLORS, PLAYFIELD_HEIGHT, PLAYFIELD_WIDTH, draw } from '../heatmap'
import type { ReplayCharts } from '../types'

/** Where the cursor spent its time over the playfield, with misses, 50s and 100s marked. */
export default function Heatmap({ charts }: { charts: ReplayCharts }) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const counted = charts.notes.reduce<Record<string, number>>((acc, n) => {
    acc[n.result] = (acc[n.result] ?? 0) + 1
    return acc
  }, {})

  useEffect(() => {
    const ctx = canvas.current?.getContext('2d')
    if (ctx) draw(ctx, charts) // no 2D context (a test page without canvas): nothing to draw
  }, [charts])

  return (
    <figure className="chart heatmap" aria-label="Cursor heatmap">
      <figcaption>
        Where the cursor was <span className="muted">(brighter: longer)</span>
      </figcaption>
      <canvas
        ref={canvas}
        width={PLAYFIELD_WIDTH}
        height={PLAYFIELD_HEIGHT}
        role="img"
        aria-label={`Cursor heatmap with ${counted.miss ?? 0} misses, ${counted['50'] ?? 0} 50s and ${counted['100'] ?? 0} 100s marked`}
      />
      <p className="legend">
        <span style={{ color: MARK_COLORS.miss }}>✕ miss</span>
        <span style={{ color: MARK_COLORS['50'] }}>○ 50</span>
        <span style={{ color: MARK_COLORS['100'] }}>○ 100</span>
        {charts.cursor.off_playfield > 0 && (
          <span className="muted">{charts.cursor.off_playfield} frames off the playfield</span>
        )}
      </p>
    </figure>
  )
}
