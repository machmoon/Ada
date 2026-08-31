// The Case tab's read of the service's additive `enclosure` response key
// (docs/ai-cad-plan.md, workstream D's frozen shape): the .scad text, the
// emitted parameters, the fit receipt's signed margins, and the warnings.
// Pure and DOM-free; the tab renders what this accepts and nothing else.

export const SCAD_FILENAME = 'enclosure.scad'
// Octet-stream for the same reason download.js's PCB_MIME is: the browser must
// save the file, not open a text tab over the top of the app.
export const SCAD_MIME = 'application/octet-stream'

/** The receipt's axis order — x and y around the board, z above it. */
export const MARGIN_AXES = ['x', 'y', 'z']

function objectOf(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : null
}

function finite(value) {
  return typeof value === 'number' && Number.isFinite(value)
}

/** The enclosure on a response, or null when it carries none. `null` is the
    contract's honest degradation (stage skipped or failed, board still
    delivered), so callers must render an explicit empty state, never a blank
    tab. A malformed object is treated the same as an absent one — the tab
    must not draw a receipt it cannot vouch for. */
export function readEnclosure(result) {
  const enclosure = objectOf(result?.enclosure)
  if (!enclosure) return null
  const scad = typeof enclosure.scad === 'string' ? enclosure.scad : ''
  if (!scad.trim()) return null
  return {
    scad,
    params: paramsOf(enclosure.params),
    margins: marginsOf(enclosure.fit),
    warnings: warningsOf(enclosure.warnings),
    repairRounds: roundsOf(enclosure.repair_rounds),
  }
}

/** The emitted parameters as rows, in the server's own order — emit.py writes
    them board-first on purpose, and alphabetising would shuffle the story.
    Only finite numbers survive: a parameter is a millimetre value or it is
    not a parameter. */
function paramsOf(value) {
  const params = objectOf(value)
  if (!params) return []
  return Object.entries(params)
    .filter(([, mm]) => finite(mm))
    .map(([name, mm]) => ({ name, mm }))
}

/** The signed per-axis margins, or null when the receipt is absent or
    malformed. Whole-or-nothing: a receipt missing an axis is not a receipt,
    and inventing a zero for it is exactly the quiet-zero bug class the
    verifier exists to prevent. */
function marginsOf(fit) {
  const margins = objectOf(objectOf(fit)?.margins_mm)
  if (!margins || !MARGIN_AXES.every((axis) => finite(margins[axis]))) return null
  return { x: margins.x, y: margins.y, z: margins.z }
}

function warningsOf(value) {
  if (!Array.isArray(value)) return []
  return value.filter((warning) => typeof warning === 'string' && warning.trim())
}

function roundsOf(value) {
  const n = Number(value)
  return Number.isFinite(n) && n >= 0 ? Math.round(n) : 0
}

/** Any negative margin means the cavity collides with the board — the one
    number on the receipt that must never be softened. */
export function hasCollision(margins) {
  if (!margins) return false
  return MARGIN_AXES.some((axis) => margins[axis] < 0)
}

/** A millimetre value for display: up to three decimals, trailing zeros
    trimmed, but never bare — "2.0", not "2", so the column reads as mm. */
export function formatMm(value) {
  const trimmed = value.toFixed(3).replace(/0+$/, '')
  return trimmed.endsWith('.') ? `${trimmed}0` : trimmed
}

/** A signed margin: the receipt's whole point is the sign, so a positive
    clearance carries an explicit plus and a collision keeps its minus. */
export function formatMargin(value) {
  return `${value >= 0 ? '+' : ''}${formatMm(value)}`
}
