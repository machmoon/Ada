const DOT = '·'

export function formatDuration(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0))
  if (s < 60) return `${s} s`
  return `${Math.floor(s / 60)} m ${String(s % 60).padStart(2, '0')} s`
}

export function formatCount(n, singular, plural = `${singular}s`) {
  return `${n} ${n === 1 ? singular : plural}`
}

export function formatParts(parts) {
  return (parts || []).filter(Boolean).join(` ${DOT} `)
}

export function formatBoard(boardMm) {
  const [w, h] = boardMm || []
  if (typeof w !== 'number' || typeof h !== 'number') return ''
  return `${w.toFixed(1)} × ${h.toFixed(1)} mm`
}

/** requirements/pins arrive as either a count or the list itself. */
export function countOf(value) {
  if (Array.isArray(value)) return value.length
  const n = Number(value)
  return Number.isFinite(n) ? n : 0
}

export function joinDot(parts) {
  return parts.filter(Boolean).join(` ${DOT} `)
}
