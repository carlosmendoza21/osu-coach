// Calls to the backend. In development Vite forwards /api/... to it (vite.config.ts).
import type { PlayerResponse, ReplayCharts, ReplayDetail, UploadResponse } from './types'

export const API_BASE = '/api'

/** A request the API answered with an error status, with its reason. */
export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/** The reason in a FastAPI error body: {"detail": "..."} or a list of field errors. */
async function reason(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json()
    if (body && typeof body === 'object' && 'detail' in body) {
      const detail = body.detail
      if (typeof detail === 'string') return detail
      if (Array.isArray(detail) && detail.length > 0) {
        const first: unknown = detail[0]
        if (first && typeof first === 'object' && 'msg' in first) return String(first.msg)
      }
    }
  } catch {
    // Not JSON (a proxy's error page, say): fall through to the status text.
  }
  return response.statusText || `request failed (${response.status})`
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init)
  if (!response.ok) {
    throw new ApiError(response.status, await reason(response))
  }
  return (await response.json()) as T
}

export function getHealth(): Promise<{ status: string }> {
  return request('/health')
}

export function getPlayer(username: string): Promise<PlayerResponse> {
  return request(`/players/${encodeURIComponent(username)}`)
}

export function getReplay(id: number): Promise<ReplayDetail> {
  return request(`/replays/${id}`)
}

export function getReplayCharts(id: number): Promise<ReplayCharts> {
  return request(`/replays/${id}/charts`)
}

export function uploadReplay(file: File): Promise<UploadResponse> {
  const form = new FormData()
  form.append('file', file) // the field name the API expects
  return request('/replays/upload', { method: 'POST', body: form })
}
