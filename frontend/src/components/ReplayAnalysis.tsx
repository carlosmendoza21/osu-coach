import {
  accuracyOf,
  formatAccuracy,
  formatDate,
  formatMods,
  formatPx,
  formatStars,
  formatTiming,
  formatUr,
} from '../format'
import type { HitCounts, HitResult, ReplayDetail } from '../types'

const RESULTS: HitResult[] = ['300', '100', '50', 'miss']
// Patterns in a fixed order, so pages for different replays read alike.
const PATTERN_ORDER = ['jump', 'stream', 'slider', 'spinner', 'other']

function patternRank(name: string): number {
  const i = PATTERN_ORDER.indexOf(name)
  return i === -1 ? PATTERN_ORDER.length : i
}

/** The play: map, player, mods, and when it was played. */
export function ReplayHeader({ detail }: { detail: ReplayDetail }) {
  const { replay, beatmap } = detail
  return (
    <section className="replay-header" aria-label="Play">
      <h1>
        {beatmap ? (
          <a href={`https://osu.ppy.sh/b/${beatmap.id}`} target="_blank" rel="noreferrer">
            {beatmap.artist} - {beatmap.title} <span className="version">[{beatmap.version}]</span>
          </a>
        ) : (
          'Map not fetched yet'
        )}
      </h1>
      <p className="muted">
        Played by {replay.player_name}
        {' · '}
        {formatMods(replay.mods)}
        {beatmap && ` · ${formatStars(beatmap.star_rating)}`}
        {' · '}
        {formatDate(replay.played_at)}
      </p>
    </section>
  )
}

/** The analysis: counts against osu!'s, timing and aim, and how each pattern went. */
export default function ReplayAnalysis({ detail }: { detail: ReplayDetail }) {
  const { replay, judged, matches_official, by_pattern } = detail
  const official: HitCounts = {
    '300': replay.count_300,
    '100': replay.count_100,
    '50': replay.count_50,
    miss: replay.count_miss,
  }
  const patterns = Object.entries(by_pattern).sort(([a], [b]) => patternRank(a) - patternRank(b))

  return (
    <>
      <section aria-label="Summary">
        <dl className="stats">
          <div>
            <dt>Accuracy</dt>
            <dd>{formatAccuracy(accuracyOf(official))}</dd>
          </div>
          <div>
            <dt title="Unstable rate: 10x the spread of your hit timing. Lower is steadier.">UR</dt>
            <dd>{formatUr(replay.unstable_rate)}</dd>
          </div>
          <div>
            <dt>Timing</dt>
            <dd>{formatTiming(replay.mean_error_ms)}</dd>
          </div>
          <div>
            <dt title="Average distance from the circle's center when you hit it">Aim error</dt>
            <dd>{formatPx(replay.mean_aim_error_px)}</dd>
          </div>
          <div>
            <dt>Max combo</dt>
            <dd>{replay.max_combo}x</dd>
          </div>
        </dl>
      </section>

      <table className="counts">
        <caption>
          Hit counts{' '}
          {matches_official === true && <span className="match ok">✓ matches osu!</span>}
          {matches_official === false && (
            <span className="match off">differs from osu! (spinners aren't judged yet)</span>
          )}
        </caption>
        <thead>
          <tr>
            <th scope="col" />
            {RESULTS.map((r) => (
              <th scope="col" key={r}>
                {r}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          <tr>
            <th scope="row">osu!</th>
            {RESULTS.map((r) => (
              <td key={r}>{official[r]}</td>
            ))}
          </tr>
          {judged && (
            <tr>
              <th scope="row">Our analysis</th>
              {RESULTS.map((r) => (
                <td key={r}>{judged[r]}</td>
              ))}
            </tr>
          )}
        </tbody>
      </table>

      <table className="patterns">
        <caption>By pattern</caption>
        <thead>
          <tr>
            <th scope="col">Pattern</th>
            {RESULTS.map((r) => (
              <th scope="col" key={r}>
                {r}
              </th>
            ))}
            <th scope="col">Accuracy</th>
          </tr>
        </thead>
        <tbody>
          {patterns.map(([name, counts]) => (
            <tr key={name}>
              <th scope="row">{name}</th>
              {RESULTS.map((r) => (
                <td key={r} className={r === 'miss' && counts.miss > 0 ? 'miss' : undefined}>
                  {counts[r]}
                </td>
              ))}
              <td>{formatAccuracy(accuracyOf(counts))}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}
