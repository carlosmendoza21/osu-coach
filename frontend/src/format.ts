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
