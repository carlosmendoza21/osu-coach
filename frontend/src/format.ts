// How numbers and dates read on the page, in one place so every page shows them alike.

/** 1234.5 -> "1,235pp" */
export function formatPp(pp: number | null): string {
  return pp === null ? '-' : `${Math.round(pp).toLocaleString('en-US')}pp`
}

/** A 0-1 fraction (a score's accuracy) -> "98.76%" */
export function formatAccuracy(fraction: number): string {
  return `${(fraction * 100).toFixed(2)}%`
}

/** An already-percent value (a player's accuracy, 98.7612) -> "98.76%" */
export function formatPercent(percent: number): string {
  return `${percent.toFixed(2)}%`
}

/** 12345 -> "#12,345"; null (inactive players) -> "unranked" */
export function formatRank(rank: number | null): string {
  return rank === null ? 'unranked' : `#${rank.toLocaleString('en-US')}`
}

/** 1234567 -> "1,234,567" */
export function formatCount(n: number): string {
  return n.toLocaleString('en-US')
}

/** ["HD", "DT"] -> "+HDDT"; [] -> "NM" (no mod), as osu! shows them */
export function formatMods(mods: string[]): string {
  return mods.length === 0 ? 'NM' : `+${mods.join('')}`
}

/** 5.4321 -> "5.43★" */
export function formatStars(stars: number): string {
  return `${stars.toFixed(2)}★`
}

/** An ISO date -> "Apr 28, 2026", in the reader's time zone */
export function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  })
}

/** How long ago an ISO date was, roughly: "just now", "5 minutes ago", "3 days ago" */
export function timeAgo(iso: string, now: Date = new Date()): string {
  const seconds = Math.max(0, (now.getTime() - new Date(iso).getTime()) / 1000)
  const units: [number, string][] = [
    [365 * 24 * 3600, 'year'],
    [30 * 24 * 3600, 'month'],
    [24 * 3600, 'day'],
    [3600, 'hour'],
    [60, 'minute'],
  ]
  for (const [size, name] of units) {
    const n = Math.floor(seconds / size)
    if (n >= 1) return `${n} ${name}${n === 1 ? '' : 's'} ago`
  }
  return 'just now'
}

/** osu!'s accuracy for some hit counts: 300s count fully, 100s a third, 50s a sixth. */
export function accuracyOf(counts: { '300': number; '100': number; '50': number; miss: number }): number {
  const total = counts['300'] + counts['100'] + counts['50'] + counts.miss
  if (total === 0) return 0
  return (300 * counts['300'] + 100 * counts['100'] + 50 * counts['50']) / (300 * total)
}

/** A mean timing error -> "1.8 ms early", "2.2 ms late", or "on time" (within 0.05 ms). */
export function formatTiming(meanMs: number | null): string {
  if (meanMs === null) return '-'
  if (Math.abs(meanMs) < 0.05) return 'on time'
  return `${Math.abs(meanMs).toFixed(1)} ms ${meanMs < 0 ? 'early' : 'late'}`
}

/** An unstable rate -> "95.5"; lower is steadier timing. */
export function formatUr(ur: number | null): string {
  return ur === null ? '-' : ur.toFixed(1)
}

/** An aim error in osu! pixels -> "14.2 px" */
export function formatPx(px: number | null): string {
  return px === null ? '-' : `${px.toFixed(1)} px`
}
