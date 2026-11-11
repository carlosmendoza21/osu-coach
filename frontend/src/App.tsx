import { Link, Route, Routes } from 'react-router'

import PlayerSearch from './components/PlayerSearch'
import HomePage from './pages/HomePage'
import PlayerPage from './pages/PlayerPage'

/** The layout around every page, and which page each URL shows. */
export default function App() {
  return (
    <>
      <header className="site-header">
        <Link to="/" className="brand">
          osu! coach
        </Link>
        <PlayerSearch />
      </header>
      <main>
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/players/:username" element={<PlayerPage />} />
          <Route path="*" element={<p role="alert">This page doesn't exist.</p>} />
        </Routes>
      </main>
    </>
  )
}
