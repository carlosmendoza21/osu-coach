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
