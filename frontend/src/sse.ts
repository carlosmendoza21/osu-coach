// Reading server-sent events out of a stream of text: pure functions, no fetch.
import type { CoachEvent } from './types'

/** The complete events in `buffer`, and what's left over (an event still arriving).
 * Events end with a blank line; each one's `data:` lines hold JSON. */
export function takeEvents(buffer: string): { events: CoachEvent[]; rest: string } {
  const normalized = buffer.replace(/\r\n/g, '\n')
  const parts = normalized.split('\n\n')
  const rest = parts.pop() ?? '' // the last part has no blank line after it yet
  const events: CoachEvent[] = []
  for (const part of parts) {
    const data = part
      .split('\n')
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).trimStart())
      .join('\n')
    if (data) events.push(JSON.parse(data) as CoachEvent)
  }
  return { events, rest }
}
