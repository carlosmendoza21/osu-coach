import { formatAccuracy, formatDate, formatMods, formatPp, formatStars } from '../format'
import type { Score } from '../types'

/** A table of plays: the map, its grade, pp, accuracy, mods, hit counts and date. */
export default function ScoreTable({ scores, caption }: { scores: Score[]; caption: string }) {
  if (scores.length === 0) {
    return <p className="muted">No plays to show.</p>
  }
  return (
    <table className="scores">
      <caption>{caption}</caption>
      <thead>
        <tr>
          <th scope="col">Map</th>
          <th scope="col">Grade</th>
          <th scope="col">pp</th>
          <th scope="col">Accuracy</th>
          <th scope="col">Mods</th>
          <th scope="col" title="300 / 100 / 50 / miss">
            Hits
          </th>
          <th scope="col">Combo</th>
          <th scope="col">Date</th>
        </tr>
      </thead>
      <tbody>
        {scores.map((s) => (
          <tr key={s.id} className={s.passed ? undefined : 'failed'}>
            <td className="map">
              {s.beatmap ? (
                <a href={`https://osu.ppy.sh/b/${s.beatmap.id}`} target="_blank" rel="noreferrer">
                  {s.beatmap.artist} - {s.beatmap.title}{' '}
                  <span className="version">[{s.beatmap.version}]</span>{' '}
                  <span className="stars">{formatStars(s.beatmap.star_rating)}</span>
                </a>
              ) : (
                `map ${s.beatmap_id}`
              )}
            </td>
            <td className={`grade grade-${s.rank}`}>{s.passed ? s.rank : 'F'}</td>
            <td>{formatPp(s.pp)}</td>
            <td>{formatAccuracy(s.accuracy)}</td>
            <td>{formatMods(s.mods)}</td>
            <td className="hits">
              {s.count_300} / {s.count_100} / {s.count_50} / <span className="miss">{s.count_miss}</span>
            </td>
            <td>{s.max_combo}x</td>
            <td>{formatDate(s.date)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
