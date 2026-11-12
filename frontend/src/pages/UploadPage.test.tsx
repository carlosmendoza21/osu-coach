// Unit tests for the upload page, at /replays/new. fetch is replaced with a fake.
import { act, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'

import UploadPage from './UploadPage'

type Call = { url: string; init?: RequestInit }
let calls: Call[]
let answer: () => Promise<Response>

beforeEach(() => {
  calls = []
  answer = async () => Response.json({ replay: { id: 42 }, created: true })
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      calls.push({ url, init })
      return answer()
    }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function Where() {
  return <p data-testid="where">{useLocation().pathname}</p>
}

function renderPage() {
  render(
    <MemoryRouter initialEntries={['/replays/new']}>
      <Routes>
        <Route path="/replays/new" element={<UploadPage />} />
        <Route path="*" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  )
  return screen.getByLabelText('Replay file') as HTMLInputElement
}

/** The name of the file the n-th request sent, after checking it sent a form. */
function sentFileName(n: number): string {
  const body = calls[n].init?.body
  expect(body).toBeInstanceOf(FormData)
  return ((body as FormData).get('file') as File).name
}

const osr = (name = 'play.osr') => new File([new Uint8Array([0, 1, 2])], name)

test('the page says what it takes', () => {
  renderPage()

  expect(screen.getByRole('heading', { name: 'Upload a replay' })).toBeInTheDocument()
  expect(screen.getByLabelText('Replay file')).toHaveAttribute('accept', '.osr')
  expect(screen.getByText('Choose an .osr file, or drop it here')).toBeInTheDocument()
})

test('a picked file is uploaded, then its replay page opens', async () => {
  const user = userEvent.setup()
  const input = renderPage()

  await user.upload(input, osr())

  expect(calls).toHaveLength(1)
  expect(calls[0].url).toBe('/api/replays/upload')
  expect(sentFileName(0)).toBe('play.osr')
  expect(await screen.findByTestId('where')).toHaveTextContent('/replays/42')
})

test('a dropped file is uploaded too', async () => {
  renderPage()
  const zone = screen.getByText('Choose an .osr file, or drop it here').closest('label')!

  await act(async () => {
    fireEvent.drop(zone, { dataTransfer: { files: [osr('dropped.osr')] } })
  })

  expect(sentFileName(0)).toBe('dropped.osr')
  expect(await screen.findByTestId('where')).toHaveTextContent('/replays/42')
})

test('dragging over the zone highlights it, and leaving undoes that', () => {
  renderPage()
  const zone = screen.getByText('Choose an .osr file, or drop it here').closest('label')!

  fireEvent.dragOver(zone)
  expect(zone).toHaveClass('dragging')
  fireEvent.dragLeave(zone)
  expect(zone).not.toHaveClass('dragging')
})

test('while uploading: the file is named and the picker is off', async () => {
  const user = userEvent.setup()
  answer = () => new Promise(() => {}) // never answers
  const input = renderPage()

  await user.upload(input, osr('long.osr'))

  expect(screen.getByText('Uploading long.osr...')).toBeInTheDocument()
  expect(input).toBeDisabled()
})

test.each([
  [413, { detail: 'a replay is at most 4 MB' }, 'That file is too large to be a replay (4 MB at most).'],
  [422, { detail: 'only osu! standard replays are supported, not mania' },
   "That isn't a replay we can read: only osu! standard replays are supported, not mania."],
])('a %i is explained, and another file can be tried', async (status, body, message) => {
  const user = userEvent.setup()
  answer = async () => Response.json(body, { status })
  const input = renderPage()

  await user.upload(input, osr())

  expect(await screen.findByRole('alert')).toHaveTextContent(message)
  expect(input).not.toBeDisabled()
  expect(screen.queryByTestId('where')).toBeNull() // stayed on the upload page
})

test('picking the same file again uploads it again', async () => {
  // The input is cleared after each pick: otherwise the browser sees no change.
  const user = userEvent.setup()
  answer = async () => Response.json({ detail: 'bad' }, { status: 422 })
  const input = renderPage()
  const file = osr()

  await user.upload(input, file)
  await screen.findByRole('alert')
  expect(input.value).toBe('')
  await user.upload(input, file)

  expect(calls).toHaveLength(2)
})

test('no answer at all is explained', async () => {
  const user = userEvent.setup()
  answer = () => Promise.reject(new TypeError('Failed to fetch'))
  const input = renderPage()

  await user.upload(input, osr())

  expect(await screen.findByRole('alert')).toHaveTextContent("Couldn't reach the API.")
})
