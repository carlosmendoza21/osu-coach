import { type ChangeEvent, type DragEvent, useState } from 'react'
import { useNavigate } from 'react-router'

import { uploadReplay } from '../api'
import { uploadError } from '../errors'

type Upload = { state: 'idle' } | { state: 'uploading'; name: string } | { state: 'error'; message: string }

/** /replays/new: pick or drop an .osr file; once stored, go to its page. */
export default function UploadPage() {
  const [upload, setUpload] = useState<Upload>({ state: 'idle' })
  const [dragging, setDragging] = useState(false)
  const navigate = useNavigate()

  async function send(file: File) {
    setUpload({ state: 'uploading', name: file.name })
    try {
      const stored = await uploadReplay(file)
      void navigate(`/replays/${stored.replay.id}`)
    } catch (error: unknown) {
      setUpload({ state: 'error', message: uploadError(error) })
    }
  }

  function picked(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    event.target.value = '' // so picking the same file again still counts as a change
    if (file) void send(file)
  }

  function dropped(event: DragEvent) {
    event.preventDefault() // otherwise the browser opens the file itself
    setDragging(false)
    const file = event.dataTransfer.files[0]
    if (file) void send(file)
  }

  const busy = upload.state === 'uploading'
  return (
    <>
      <h1>Upload a replay</h1>
      <p className="muted">
        An osu! standard replay (<code>.osr</code>) from osu!stable or osu!lazer. Its map is fetched
        from osu!, then every circle and slider is judged.
      </p>
      <label
        className={`dropzone${dragging ? ' dragging' : ''}${busy ? ' busy' : ''}`}
        onDragOver={(e) => {
          e.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={dropped}
      >
        <input type="file" accept=".osr" onChange={picked} disabled={busy} aria-label="Replay file" />
        <span>{busy ? `Uploading ${upload.name}...` : 'Choose an .osr file, or drop it here'}</span>
      </label>
      {upload.state === 'error' && (
        <p role="alert" className="error">
          {upload.message}
        </p>
      )}
    </>
  )
}
