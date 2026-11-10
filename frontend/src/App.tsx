import { useEffect, useState } from 'react'

import { getHealth } from './api'

type ApiStatus = 'checking' | 'ok' | 'down'

export default function App() {
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
    <main>
      <h1>osu! coach</h1>
      <p>Replay analysis, skill profiles and practice maps for osu! standard.</p>
      <p className={`api-status ${status}`} role="status">
        {status === 'checking' && 'Checking the API...'}
        {status === 'ok' && 'API connected'}
        {status === 'down' && 'API unreachable: is the backend running on port 8000?'}
      </p>
    </main>
  )
}
