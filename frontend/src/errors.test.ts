// Unit tests for src/errors.ts: what the reader is told when a player can't be loaded.
import { expect, test } from 'vitest'

import { ApiError } from './api'
import { playerError, replayError, uploadError } from './errors'

test.each([
  [new ApiError(404, "osu! player 'x' not found"), 'No osu! player is called "nobody".'],
  [new ApiError(502, 'osu! request failed'), "osu! didn't answer. Try again in a minute."],
  [new ApiError(504, 'syncing from osu! timed out'), 'Syncing from osu! took too long. Try again in a minute.'],
  // Anything else: the API's own reason.
  [new ApiError(500, 'player missing after sync'), 'Something went wrong: player missing after sync'],
  [new ApiError(422, 'Field required'), 'Something went wrong: Field required'],
  // Not an answer from the API at all (fetch failed: the dev server is down).
  [new TypeError('Failed to fetch'), "Couldn't reach the API. Is the backend running?"],
  ['a thrown string', "Couldn't reach the API. Is the backend running?"],
])('%s -> %s', (error, message) => {
  expect(playerError('nobody', error)).toBe(message)
})

test.each([
  [new ApiError(413, 'a replay is at most 4 MB'), 'That file is too large to be a replay (4 MB at most).'],
  [new ApiError(422, 'only osu! standard replays are supported, not taiko'),
   "That isn't a replay we can read: only osu! standard replays are supported, not taiko."],
  [new ApiError(500, 'replay missing after storing it'), 'Something went wrong: replay missing after storing it'],
  [new TypeError('Failed to fetch'), "Couldn't reach the API. Is the backend running?"],
])('uploadError %s -> %s', (error, message) => {
  expect(uploadError(error)).toBe(message)
})

test.each([
  [new ApiError(404, 'replay 9 not found'), "There's no replay 9."],
  [new ApiError(500, 'boom'), 'Something went wrong: boom'],
  [new TypeError('Failed to fetch'), "Couldn't reach the API. Is the backend running?"],
])('replayError %s -> %s', (error, message) => {
  expect(replayError('9', error)).toBe(message)
})
