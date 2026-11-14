// Drawing the cursor heatmap on a canvas (used by components/Heatmap.tsx).
import { shade } from './chartdata'
import type { ReplayCharts } from './types'

export const PLAYFIELD_WIDTH = 512 // the osu! playfield, in its own pixels
export const PLAYFIELD_HEIGHT = 384
// Notes worth a look: a miss red, a 50 orange, a 100 yellow. 300s aren't drawn.
export const MARK_COLORS: Record<string, string> = { miss: '#ff4d4d', '50': '#f0a43a', '100': '#f6d743' }

/** Draw the heatmap: cursor time per cell, then the notes that weren't 300s. */
export function draw(ctx: CanvasRenderingContext2D, charts: ReplayCharts) {
  const { cols, rows, counts } = charts.cursor
  const cellW = PLAYFIELD_WIDTH / cols
  const cellH = PLAYFIELD_HEIGHT / rows
  const max = Math.max(0, ...counts)

  ctx.clearRect(0, 0, PLAYFIELD_WIDTH, PLAYFIELD_HEIGHT)
  ctx.fillStyle = '#1f1d24'
  ctx.fillRect(0, 0, PLAYFIELD_WIDTH, PLAYFIELD_HEIGHT)
  for (let row = 0; row < rows; row++) {
    for (let col = 0; col < cols; col++) {
      const a = shade(counts[row * cols + col], max)
      if (a > 0) {
        ctx.fillStyle = `rgba(255, 102, 170, ${a.toFixed(3)})` // osu! pink
        ctx.fillRect(col * cellW, row * cellH, cellW, cellH)
      }
    }
  }
  for (const note of charts.notes) {
    const color = MARK_COLORS[note.result]
    if (!color) continue
    ctx.strokeStyle = color
    ctx.lineWidth = 2
    ctx.beginPath()
    if (note.result === 'miss') {
      // An x for a miss.
      ctx.moveTo(note.x - 5, note.y - 5)
      ctx.lineTo(note.x + 5, note.y + 5)
      ctx.moveTo(note.x + 5, note.y - 5)
      ctx.lineTo(note.x - 5, note.y + 5)
    } else {
      ctx.arc(note.x, note.y, 4, 0, 2 * Math.PI)
    }
    ctx.stroke()
  }
}
