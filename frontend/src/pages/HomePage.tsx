import { useEffect, useState } from 'react'

import { getHealth } from '../api'

type ApiStatus = 'checking' | 'ok' | 'down'

/** /: what the app is, and whether the API answers. */
export default function HomePage() {
  const [status, setStatus] = useState<ApiStatus>('checking')

  useEffect(() => {
    // `ignore`: if the page goes away before the answer comes, don't set state on it.
    let ignore = false
    getHealth()
      .then((health) => {
        if (!ignore) setStatus(health.status === 'ok' ? 'ok' : 'down')
      })
      .catch(() => {
        if (!ignore) setStatus('down')
      })
    return () => {
      ignore = true
    }
  }, [])

  return (
    <>
      <p>Replay analysis, skill profiles and practice maps for osu! standard.</p>
      <p className="muted">Look a player up with the search box above.</p>
      <p className={`api-status ${status}`} role="status">
        {status === 'checking' && 'Checking the API...'}
        {status === 'ok' && 'API connected'}
        {status === 'down' && 'API unreachable: is the backend running on port 8000?'}
      </p>
    </>
  )
}
