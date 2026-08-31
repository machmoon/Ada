// Reading the response's `order` block. Pure functions only, so the pane can
// be a renderer and every honesty rule here can be tested without a DOM.
//
// The rule this file exists to enforce: nothing invented. Every number the
// order pane shows comes out of the response, and where the response has no
// number — a house that only quotes through an authenticated API — the pane
// says so rather than showing a zero. A zero in a money field reads as "free".

/** The fab houses the form may ask for. Ids must match silkscreen.fabhouse. */
export const FAB_SERVICES = [
  { id: 'oshpark-2layer', label: 'OSH Park — 2 Layer Prototype', priced: true },
  { id: 'oshpark-2layer-swift', label: 'OSH Park — 2 Layer Super Swift', priced: true },
  { id: 'jlcpcb-2layer', label: 'JLCPCB — 2 Layer FR-4', priced: false },
  { id: 'pcbway-2layer', label: 'PCBWay — 2 Layer FR-4', priced: false },
]

/** Checks that stop an order, in the order the gate reported them. */
const BLOCKING = new Set(['fail', 'skipped'])

/** The order block of a result, or null. Never throws on a shape it dislikes. */
export function orderOf(result) {
  const order = result && typeof result === 'object' ? result.order : null
  return order && typeof order === 'object' ? order : null
}

/** Whether there is enough in the response to draw the order pane at all. */
export function hasOrder(result) {
  const order = orderOf(result)
  return Boolean(order && order.gate && Array.isArray(order.gate.checks))
}

/** Every check, normalised, with the blocking ones first.

    Blocking first rather than in gate order: the pane is read top-down and the
    reason an order is refused should not be below the six checks that passed. */
export function checks(result) {
  const order = orderOf(result)
  const raw = order && order.gate && Array.isArray(order.gate.checks) ? order.gate.checks : []
  const normalised = raw.map((check, index) => ({
    index,
    id: String(check.id ?? ''),
    title: String(check.title ?? ''),
    status: String(check.status ?? 'skipped'),
    summary: String(check.summary ?? ''),
    source: String(check.source ?? ''),
    evidence: Array.isArray(check.evidence) ? check.evidence.map(String) : [],
    blocking: BLOCKING.has(String(check.status ?? 'skipped')),
  }))
  return [
    ...normalised.filter((c) => c.blocking),
    ...normalised.filter((c) => !c.blocking),
  ]
}

/** Pass/fail/warn/skipped tallies, read off the checks rather than the header.

    Derived here for the same reason the gate derives its own verdict: a count
    carried alongside the checks can disagree with them. */
export function tally(result) {
  const counts = { pass: 0, fail: 0, warn: 0, skipped: 0 }
  for (const check of checks(result)) {
    if (counts[check.status] === undefined) counts.skipped += 1
    else counts[check.status] += 1
  }
  return counts
}

/** True only when no check blocks. Never read from a stored flag. */
export function isGo(result) {
  if (!hasOrder(result)) return false
  return !checks(result).some((check) => check.blocking)
}

/** The quote, with `priced` telling the caller whether the money fields mean
    anything. An unpriced quote keeps its reason and its own quote-page link. */
export function quote(result) {
  const order = orderOf(result)
  const raw = order && order.quote && typeof order.quote === 'object' ? order.quote : null
  if (!raw) return null
  const priced = raw.basis !== 'unavailable' && raw.total_cents !== null &&
    raw.total_cents !== undefined
  return {
    priced,
    house: String(raw.house ?? ''),
    service: String(raw.service ?? ''),
    basis: String(raw.basis ?? 'unavailable'),
    quantity: Number(raw.quantity ?? 0),
    boardsOrdered: Number(raw.boards_ordered ?? 0),
    areaSqIn: Number(raw.area_sq_in ?? 0),
    subtotal: priced ? money(raw.subtotal_cents, raw.currency) : '',
    shipping: priced ? money(raw.shipping_cents, raw.currency) : '',
    total: priced ? money(raw.total_cents, raw.currency) : 'no price',
    leadTime: Array.isArray(raw.lead_time_days) ? raw.lead_time_days.map(Number) : [],
    reason: String(raw.unavailable_reason ?? ''),
    notes: Array.isArray(raw.notes) ? raw.notes.map(String) : [],
    quoteUrl: String(raw.quote_url ?? ''),
    sourceUrl: String(raw.source_url ?? ''),
  }
}

/** Cents to a money string. Returns '' for a missing value rather than $0.00. */
export function money(cents, currency = 'USD') {
  if (cents === null || cents === undefined || !Number.isFinite(Number(cents))) return ''
  return `$${(Number(cents) / 100).toFixed(2)} ${currency}`
}

/** The fab package as rows the pane can list and download. */
export function files(result) {
  const order = orderOf(result)
  const raw = order && Array.isArray(order.files) ? order.files : []
  return raw
    .filter((f) => f && typeof f.filename === 'string' && typeof f.content === 'string')
    .map((f) => ({
      filename: f.filename,
      content: f.content,
      bytes: new TextEncoder().encode(f.content).length,
    }))
}
