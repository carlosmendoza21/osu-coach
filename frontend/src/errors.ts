import { ApiError } from './api'

/** What to tell the reader when loading a player fails. */
export function playerError(username: string, error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 404) return `No osu! player is called "${username}".`
    if (error.status === 502) return "osu! didn't answer. Try again in a minute."
    if (error.status === 504) return 'Syncing from osu! took too long. Try again in a minute.'
    return `Something went wrong: ${error.message}`
  }
  return "Couldn't reach the API. Is the backend running?"
}

/** What to tell the reader when an upload is refused. */
export function uploadError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 413) return 'That file is too large to be a replay (4 MB at most).'
    // 422: the parser's own reason, e.g. "only osu! standard replays are supported".
    if (error.status === 422) return `That isn't a replay we can read: ${error.message}.`
    return `Something went wrong: ${error.message}`
  }
  return "Couldn't reach the API. Is the backend running?"
}

/** What to tell the reader when loading a replay fails. */
export function replayError(id: string, error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 404) return `There's no replay ${id}.`
    return `Something went wrong: ${error.message}`
  }
  return "Couldn't reach the API. Is the backend running?"
}
