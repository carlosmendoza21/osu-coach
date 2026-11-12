import { Link, Route, Routes } from 'react-router'

import PlayerSearch from './components/PlayerSearch'
import HomePage from './pages/HomePage'
import PlayerPage from './pages/PlayerPage'
import ReplayPage from './pages/ReplayPage'
import UploadPage from './pages/UploadPage'

/** The layout around every page, and which page each URL shows. */
export default function App() {
  return (
    <>
      <header className="site-header">
        <Link to="/" className="brand">
          osu! coach
        </Link>
        <PlayerSearch />
        <Link to="/replays/new" className="nav-link">
          Upload a replay
        </Link>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/players/:username" element={<PlayerPage />} />
          <Route path="/replays/new" element={<UploadPage />} />
          <Route path="/replays/:id" element={<ReplayPage />} />
          <Route path="*" element={<p role="alert">This page doesn't exist.</p>} />
        </Routes>
      </main>
    </>
  )
}
