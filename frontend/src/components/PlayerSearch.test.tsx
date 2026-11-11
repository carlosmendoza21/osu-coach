// Unit tests for the search box: what it sends the reader to.
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router'
import { expect, test } from 'vitest'

import PlayerSearch from './PlayerSearch'

function Where() {
  const location = useLocation()
  return <p data-testid="where">{location.pathname}</p>
}

function renderSearch() {
  render(
    <MemoryRouter initialEntries={['/']}>
      <PlayerSearch />
      <Routes>
        <Route path="*" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  )
  return {
    box: screen.getByRole('textbox', { name: 'osu! username' }),
    button: screen.getByRole('button', { name: 'Search' }),
    where: () => screen.getByTestId('where').textContent,
  }
}

test.each([
  ['peppy', '/players/peppy'],
  ['  peppy  ', '/players/peppy'], // spaces around a name are dropped
  ['Hello World', '/players/Hello%20World'], // a space inside stays, escaped
  ['a/b', '/players/a%2Fb'], // a slash can't make another path
  ['who?', '/players/who%3F'], // nor a ? a query
])('searching %j goes to %s', async (typed, path) => {
  const user = userEvent.setup()
  const { box, where } = renderSearch()

  await user.type(box, `${typed}{Enter}`)

  // The router stores the path as the browser would show it.
  expect(where()).toBe(path)
})

test('the button searches too', async () => {
  const user = userEvent.setup()
  const { box, button, where } = renderSearch()

  await user.type(box, 'peppy')
  await user.click(button)

  expect(where()).toBe('/players/peppy')
})

test.each(['', '   '])('nothing to search for (%j): the button is off and Enter does nothing', async (typed) => {
  const user = userEvent.setup()
  const { box, button, where } = renderSearch()

  if (typed) await user.type(box, typed)
  await user.type(box, '{Enter}')

  expect(button).toBeDisabled()
  expect(where()).toBe('/')
})

test('names are capped at 32 characters, like the API', () => {
  const { box } = renderSearch()

  expect(box).toHaveAttribute('maxLength', '32')
})
