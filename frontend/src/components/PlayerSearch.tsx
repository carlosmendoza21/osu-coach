import { type FormEvent, useState } from 'react'
import { useNavigate } from 'react-router'

/** A box to type an osu! username in; going to that player's page on submit. */
export default function PlayerSearch({ initial = '' }: { initial?: string }) {
  const [name, setName] = useState(initial)
  const navigate = useNavigate()

  function submit(event: FormEvent) {
    event.preventDefault() // stay on this page; the router changes the URL instead
    const username = name.trim()
    if (username) void navigate(`/players/${encodeURIComponent(username)}`)
  }

  return (
    <form className="player-search" role="search" onSubmit={submit}>
      <input
        aria-label="osu! username"
        placeholder="osu! username"
        value={name}
        onChange={(e) => setName(e.target.value)}
        maxLength={32}
      />
      <button type="submit" disabled={!name.trim()}>
        Search
      </button>
    </form>
  )
}
