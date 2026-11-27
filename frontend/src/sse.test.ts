// Unit tests for src/sse.ts: reading server-sent events out of a stream of text.
import { describe, expect, test } from 'vitest'

import { takeEvents } from './sse'

const step = (text: string) => `data: ${JSON.stringify({ type: 'step', text })}\n\n`

describe('takeEvents', () => {
  test.each([
    ['nothing', '', [], ''],
    ['one event', step('a'), [{ type: 'step', text: 'a' }], ''],
    ['two events', step('a') + step('b'), [{ type: 'step', text: 'a' }, { type: 'step', text: 'b' }], ''],
    // Edge: an event still arriving stays in the buffer for the next chunk.
    ['half an event', 'data: {"type": "st', [], 'data: {"type": "st'],
    ['an event and a half', step('a') + 'data: {"ty', [{ type: 'step', text: 'a' }], 'data: {"ty'],
    // Edge: the data line is all there, but not the blank line that ends the event.
    ['no blank line yet', 'data: {"type": "step", "text": "a"}\n', [], 'data: {"type": "step", "text": "a"}\n'],
  ])('%s', (_name, buffer, events, rest) => {
    expect(takeEvents(buffer)).toEqual({ events, rest })
  })

  test('Windows line endings are read the same', () => {
    expect(takeEvents('data: {"type": "step", "text": "a"}\r\n\r\n').events).toEqual([{ type: 'step', text: 'a' }])
  })

  test('data: with or without a space', () => {
    expect(takeEvents('data:{"type": "step", "text": "a"}\n\n').events).toEqual([{ type: 'step', text: 'a' }])
  })

  test("an event's data lines are joined", () => {
    // The format allows one event's data over several lines.
    const buffer = 'data: {"type": "step",\ndata: "text": "a"}\n\n'

    expect(takeEvents(buffer).events).toEqual([{ type: 'step', text: 'a' }])
  })

  test('other lines (comments, ids) are ignored', () => {
    const buffer = ': keep-alive\nid: 7\nevent: step\ndata: {"type": "step", "text": "a"}\n\n'

    expect(takeEvents(buffer).events).toEqual([{ type: 'step', text: 'a' }])
  })

  test('an event with no data is skipped', () => {
    expect(takeEvents(': only a comment\n\n' + step('a')).events).toEqual([{ type: 'step', text: 'a' }])
  })

  test('a whole report comes through as sent', () => {
    const report = { type: 'report', report: { username: 'peppy', summary: 'a "quoted" word\nand a line' } }

    expect(takeEvents(`data: ${JSON.stringify(report)}\n\n`).events).toEqual([report])
  })
})
