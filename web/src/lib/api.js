// The only fetch in the app. Paths are relative because the bundle is served
// from the same origin as the API — in dev by the Vite proxy, in production by
// service/app.py. No absolute URL, therefore no CORS, ever.

const ENDPOINT = '/generate'

export const REQUEST_TIMEOUT_MS = 300000
export const MAX_REQUEST_BYTES = 1024 * 1024
export const MIN_TIME_LIMIT_S = 5
export const MAX_TIME_LIMIT_S = 60

/** kind is what the UI switches on; status is kept for the error panel's footer. */
export class ApiError extends Error {
  constructor(kind, message, { status = 0, errorId = '' } = {}) {
    super(message)
    this.name = 'ApiError'
    this.kind = kind
    this.status = status
    this.errorId = errorId
  }
}

function clampTimeLimit(value) {
  const n = Number(value)
  if (!Number.isFinite(n)) return MIN_TIME_LIMIT_S
  return Math.min(MAX_TIME_LIMIT_S, Math.max(MIN_TIME_LIMIT_S, Math.round(n)))
}

/** Drop half-filled datasheet rows and clamp the solver budget to what the service accepts. */
export function normalizeRequest(request) {
  const datasheets = {}
  for (const [part, url] of Object.entries(request.datasheets || {})) {
    const p = String(part).trim()
    const u = String(url).trim()
    if (p && u) datasheets[p] = u
  }
  return {
    intent: String(request.intent ?? '').trim(),
    datasheets,
    time_limit_s: clampTimeLimit(request.time_limit_s),
    review: request.review !== false,
  }
}

export function requestBytes(request) {
  return new TextEncoder().encode(JSON.stringify(request)).length
}

function asArray(value) {
  return Array.isArray(value) ? value : []
}

/** Synthesize finding cards from the flattened blocker strings older responses return. */
function findingsFrom(data) {
  if (Array.isArray(data.findings)) return data.findings
  return asArray(data.blockers).map((text) => ({
    severity: 'blocker',
    title: String(text),
    detail: '',
    parts: [],
    citation: '',
    suggested_fix: '',
  }))
}

function normalizeResponse(data) {
  return {
    ...data,
    board_mm: asArray(data.board_mm),
    parts: asArray(data.parts),
    blockers: asArray(data.blockers),
    warnings: asArray(data.warnings),
    nets: asArray(data.nets),
    datasheets: asArray(data.datasheets),
    findings: findingsFrom(data),
    repair_rounds: Number(data.repair_rounds ?? 0),
    duration_s: Number(data.duration_s ?? 0),
  }
}

function errorFor(status, body) {
  const message = String(body.error || `request failed with status ${status}`)
  const errorId = String(body.error_id || '')
  if (status === 400) return new ApiError('validation', message, { status })
  if (status === 404) return new ApiError('not-found', message, { status })
  if (status === 413) return new ApiError('too-large', message, { status })
  if (status === 502) {
    // An unkeyed clone is the most common first run: it is configuration, not an outage.
    const kind = message.includes('GOOGLE_API_KEY') ? 'no-api-key' : 'upstream'
    return new ApiError(kind, message, { status })
  }
  if (status >= 500) return new ApiError('internal', message, { status, errorId })
  return new ApiError('upstream', message, { status })
}

export async function generate(request) {
  const body = JSON.stringify(request)
  const bytes = new TextEncoder().encode(body).length
  if (bytes > MAX_REQUEST_BYTES) {
    throw new ApiError(
      'too-large',
      `The request is ${bytes} bytes; the service accepts at most ${MAX_REQUEST_BYTES}.`,
    )
  }

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)

  let response
  try {
    response = await fetch(ENDPOINT, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body,
      signal: controller.signal,
    })
  } catch (err) {
    if (controller.signal.aborted) {
      throw new ApiError('timeout', 'The run passed the 300 second budget and was cancelled.')
    }
    throw new ApiError('network', String(err && err.message ? err.message : err))
  } finally {
    clearTimeout(timer)
  }

  let data = {}
  try {
    data = await response.json()
  } catch {
    if (response.ok) {
      throw new ApiError('internal', 'The service returned a body that is not JSON.', {
        status: response.status,
      })
    }
  }

  if (!response.ok) throw errorFor(response.status, data || {})
  return normalizeResponse(data || {})
}
