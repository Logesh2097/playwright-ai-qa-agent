/** How statuses, rates, times and durations are shown on every reporting page. */

export type Tone = 'success' | 'warning' | 'danger' | 'neutral'

const TONES: Record<string, Tone> = {
  passed: 'success',
  fixed: 'success',
  failed: 'danger',
  error: 'danger',
  regressed: 'danger',
  still_failing: 'danger',
  flaky: 'warning',
  needs_review: 'warning',
  inconclusive: 'warning',
  changed: 'warning',
}

/** Only `passed` is green. Flaky and needs-review are never shown as a pass. */
export function toneOf(status: string | null | undefined): Tone {
  return (status && TONES[status]) || 'neutral'
}

export function label(value: string | null | undefined): string {
  return value ? value.replaceAll('_', ' ') : '—'
}

/** A share between 0 and 1 as a percentage; nothing to divide by is shown as a dash, not 0%. */
export function percent(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `${(value * 100).toFixed(value === 1 || value === 0 ? 0 : 1)}%`
}

export function duration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—'
  if (ms < 1000) return `${ms} ms`
  const seconds = ms / 1000
  if (seconds < 60) return `${seconds.toFixed(1)} s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} min ${Math.round(seconds % 60)} s`
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`
}

export function dateTime(value: string | null | undefined): string {
  if (!value) return '—'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime())
    ? '—'
    : parsed.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

export function shortId(id: string): string {
  return id.slice(0, 8)
}

export function fileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/** Recorded values (expected, found) as text. Never rendered as markup. */
export function showValue(value: unknown): string {
  if (value === null || value === undefined) return '—'
  return typeof value === 'string' ? value : JSON.stringify(value)
}
