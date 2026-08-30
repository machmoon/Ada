// The only fetch in the app. Paths are relative because the bundle is served
// from the same origin as the API — in dev by the Vite proxy, in production by
// service/app.py. No absolute URL, therefore no CORS, ever.

import { logError, logEvent, logWarn } from './log.js'
import { parseNdjson } from './stream.js'

const ENDPOINT = '/generate'
const STREAM_ENDPOINT = '/generate/stream'
const CHAT_STREAM_ENDPOINT = '/chat/stream'
const MODELS_ENDPOINT = '/models'
const PLACEMENT_ENDPOINT = '/placement/repair'
const NDJSON_TYPE = 'application/x-ndjson'
const TIMEOUT_MESSAGE = 'The run passed the 300 second budget and was cancelled.'

export const REQUEST_TIMEOUT_MS = 300000
export const MAX_REQUEST_BYTES = 1024 * 1024
export const MIN_TIME_LIMIT_S = 5
export const MAX_TIME_LIMIT_S = 120

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
    // Unlimited is the UI default. Keep an explicit false in the normalized
    // request so edit/retry/session restore cannot silently turn it back on.
    no_solver_budget:
      request.no_solver_budget === undefined ? true : request.no_solver_budget === true,
    // Grounding is opt-in and only sent when it was asked for: an absent flag
    // is the service's default, so a stray `ground: false` would say nothing.
    ...(request.ground === true ? { ground: true } : {}),
  }
}

export function normalizePlacementRequest(request = {}) {
  const policies = new Set(['deterministic', 'gemini', 'ollama', 'tinker', 'hybrid'])
  const requestedPolicy = String(request.policy || 'deterministic')
  const profile =
    request.profile && typeof request.profile === 'object'
      ? request.profile
      : String(request.profile || 'compact-control')
  const normalized = {
    profile,
    policy: policies.has(requestedPolicy) ? requestedPolicy : 'deterministic',
    profile_id: String(request.profile_id || '').trim(),
  }
  if (request.board && typeof request.board === 'object') normalized.board = request.board
  if (request.feedback && typeof request.feedback === 'object') {
    normalized.feedback = request.feedback
  }
  return normalized
}

export async function repairPlacement(request) {
  const body = JSON.stringify(normalizePlacementRequest(request))
  guardSize(body)
  let response
  try {
    response = await fetch(PLACEMENT_ENDPOINT, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body,
    })
  } catch (err) {
    throw new ApiError('network', String(err && err.message ? err.message : err))
  }
  let data = {}
  try {
    data = await response.json()
  } catch {
    throw new ApiError('internal', 'The placement service returned invalid JSON.', {
      status: response.status,
    })
  }
  if (!response.ok) throw errorFor(response.status, data)
  return data
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

/** The one line a response leaves behind, whichever way a request exits. A
    body that would not parse is a failed run even under a 200, so it warns.
    `response` is duck-typed: the streaming path passes the status its terminal
    error frame carried, which is the status the one-shot would have answered. */
function recordOutcome(path, response, ms, parsed, extra = {}) {
  const outcome = { status: response.status, ok: response.ok, ms, parsed, ...extra }
  const line = `POST ${path} returned ${response.status} in ${ms} ms`
  if (response.ok && parsed) logEvent('api.response', line, outcome)
  else logWarn('api.response', line, outcome)
}

/** The pre-flight the network never sees. Shared, so both entry points refuse
    the same oversized request with the same error. */
function guardSize(body) {
  const bytes = new TextEncoder().encode(body).length
  if (bytes <= MAX_REQUEST_BYTES) return
  logWarn('api.too-large', `Request of ${bytes} bytes refused before the network`, {
    bytes,
    limit: MAX_REQUEST_BYTES,
  })
  throw new ApiError(
    'too-large',
    `The request is ${bytes} bytes; the service accepts at most ${MAX_REQUEST_BYTES}.`,
  )
}

function requestTimer(controller, request) {
  if (request?.no_solver_budget === true) return null
  return setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)
}

function clearRequestTimer(timer) {
  if (timer !== null) clearTimeout(timer)
}

export async function generate(request) {
  const body = JSON.stringify(request)
  guardSize(body)

  const controller = new AbortController()
  const timer = requestTimer(controller, request)

  const startedAt = Date.now()
  let response
  try {
    response = await fetch(ENDPOINT, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body,
      signal: controller.signal,
    })
  } catch (err) {
    logError('api.failed', 'POST /generate never reached a response', {
      ms: Date.now() - startedAt,
      aborted: controller.signal.aborted,
    })
    if (controller.signal.aborted) {
      throw new ApiError('timeout', TIMEOUT_MESSAGE)
    }
    throw new ApiError('network', String(err && err.message ? err.message : err))
  } finally {
    clearRequestTimer(timer)
  }

  let data = {}
  let parsed = true
  try {
    data = await response.json()
  } catch {
    parsed = false
    if (response.ok) {
      // Written before the throw, which is the one exit that would otherwise
      // skip the line below -- and a 200 whose body will not parse is exactly
      // the response worth having a record of.
      recordOutcome(ENDPOINT, response, Date.now() - startedAt, false)
      throw new ApiError('internal', 'The service returned a body that is not JSON.', {
        status: response.status,
      })
    }
  }

  recordOutcome(ENDPOINT, response, Date.now() - startedAt, parsed)

  if (!response.ok) throw errorFor(response.status, data || {})
  return normalizeResponse(data || {})
}

// ------------------------------------------------------------------ stream

/** The declared type, read defensively: a proxy, a stub, or an error page need
    not carry Headers at all, and a missing one is not a reason to throw. */
function contentTypeOf(response) {
  try {
    return String(response.headers?.get?.('content-type') ?? '')
  } catch {
    return ''
  }
}

function isNdjson(type) {
  return type.toLowerCase().includes(NDJSON_TYPE)
}

/** One listener call that cannot take the read down with it: a bug in the feed
    must not cost the run that is still arriving. */
function emit(onEvent, evt) {
  if (typeof onEvent !== 'function') return
  try {
    onEvent(evt)
  } catch (err) {
    logWarn('api.stream-listener', 'A stream listener threw; the read continues', {
      event: String(evt?.event ?? ''),
      message: String(err && err.message ? err.message : err),
    })
  }
}

/** The one-shot endpoint, taken over from a stream that never got going. Only
    ever reached before a single frame arrived — see generateStream. */
function fallbackToOneShot(request, why, data) {
  logEvent('api.stream-fallback', `Falling back to POST ${ENDPOINT}: ${why}`, data)
  return generate(request)
}

function objectOf(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {}
}

/** The two frames that end a run, mapped onto exactly what the one-shot path
    would have produced for the same outcome. Anything else is progress. */
function terminalOf(evt) {
  const name = String(evt?.event ?? '')
  if (name === 'run.done') return { result: normalizeResponse(objectOf(evt.result)) }
  if (name === 'run.error') {
    // A frame with no status is malformed rather than classifiable; 500 is the
    // honest reading, and it is the status errorFor treats as internal.
    return { error: errorFor(Number(evt.status) || 500, evt) }
  }
  return null
}

/** A stream that opened and then stopped without saying how the run ended. The
    pipeline may well have finished server-side, so this is reported, never
    retried: a second run is a second set of model calls. */
function streamFailure(controller, ms, events, why) {
  logError('api.failed', `POST ${STREAM_ENDPOINT} stopped after ${events} events`, {
    ms,
    events,
    aborted: controller.signal.aborted,
    why,
  })
  if (controller.signal.aborted) return new ApiError('timeout', TIMEOUT_MESSAGE)
  return new ApiError('network', why)
}

/** Best-effort release of a body we are done with; a reader that rejects on
    cancel has nothing left to tell us. */
function releaseReader(reader) {
  try {
    const cancelled = reader.cancel?.()
    if (cancelled && typeof cancelled.catch === 'function') cancelled.catch(() => {})
  } catch {
    // Deliberately swallowed: the run's outcome is already decided.
  }
}

/** The streaming twin of generate(): same request shaping, same budget, same
    result, same error taxonomy — the only addition is that `onEvent` sees each
    pipeline event as it lands.

    It falls back to generate() in exactly three cases, all of which mean the
    stream never started: a 404 (a service built before this endpoint existed),
    a body that is not NDJSON, and a response with no readable body. Once the
    read has begun, every ending is this run's ending. A connection that dies
    mid-stream is a network failure rather than a second attempt, because the
    pipeline it was reporting on costs real model calls. */
export async function generateStream(request, onEvent) {
  // Streaming is the debugging surface — the feed is where a raw answer can
  // actually be read — so this route, and only this route, asks for the
  // model's own text. The one-shot body stays exactly what the caller shaped.
  const body = JSON.stringify({ ...request, debug: true })
  guardSize(body)

  const controller = new AbortController()
  const timer = requestTimer(controller, request)
  const startedAt = Date.now()

  try {
    let response
    try {
      response = await fetch(STREAM_ENDPOINT, {
        method: 'POST',
        headers: { 'content-type': 'application/json', accept: NDJSON_TYPE },
        body,
        signal: controller.signal,
      })
    } catch (err) {
      logError('api.failed', `POST ${STREAM_ENDPOINT} never reached a response`, {
        ms: Date.now() - startedAt,
        aborted: controller.signal.aborted,
      })
      if (controller.signal.aborted) throw new ApiError('timeout', TIMEOUT_MESSAGE)
      throw new ApiError('network', String(err && err.message ? err.message : err))
    }

    if (!response.ok) {
      if (response.status === 404) {
        return await fallbackToOneShot(request, 'the service has no streaming endpoint', {
          status: 404,
        })
      }
      // Every other status is an answer about this request, so it is classified
      // exactly as the one-shot path classifies it.
      let data = {}
      let parsed = true
      try {
        data = await response.json()
      } catch {
        parsed = false
      }
      recordOutcome(STREAM_ENDPOINT, response, Date.now() - startedAt, parsed, { stream: true })
      throw errorFor(response.status, data || {})
    }

    const type = contentTypeOf(response)
    if (!isNdjson(type)) {
      return await fallbackToOneShot(request, `the response is ${type || 'untyped'}`, {
        status: response.status,
        content_type: type,
      })
    }
    if (!response.body || typeof response.body.getReader !== 'function') {
      return await fallbackToOneShot(request, 'the response body cannot be read as a stream', {
        status: response.status,
        content_type: type,
      })
    }

    logEvent('api.stream', `POST ${STREAM_ENDPOINT} opened ${response.status}`, {
      status: response.status,
      ms: Date.now() - startedAt,
      content_type: type,
    })

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let carry = ''
    let events = 0
    let outcome = null
    let closed = false

    try {
      while (!outcome && !closed) {
        let chunk
        try {
          chunk = await reader.read()
        } catch (err) {
          throw streamFailure(
            controller,
            Date.now() - startedAt,
            events,
            String(err && err.message ? err.message : err),
          )
        }
        closed = Boolean(chunk.done)
        // On close the decoder is flushed and a newline appended, so a server
        // that ends without one still yields its last frame.
        const text = closed
          ? `${decoder.decode()}\n`
          : decoder.decode(chunk.value, { stream: true })
        const step = parseNdjson(carry, text)
        carry = step.carry
        for (const evt of step.events) {
          events += 1
          emit(onEvent, evt)
          if (!outcome) outcome = terminalOf(evt)
        }
      }
    } finally {
      releaseReader(reader)
    }

    const ms = Date.now() - startedAt
    if (outcome?.error) {
      const error = outcome.error
      recordOutcome(STREAM_ENDPOINT, { status: error.status, ok: false }, ms, true, {
        stream: true,
        events,
      })
      throw error
    }
    if (outcome) {
      recordOutcome(STREAM_ENDPOINT, response, ms, true, { stream: true, events })
      return outcome.result
    }
    throw streamFailure(controller, ms, events, 'The stream closed before the run finished.')
  } finally {
    clearRequestTimer(timer)
  }
}

// --------------------------------------------------------------------- chat

function chatTerminalOf(evt) {
  const name = String(evt?.event ?? '')
  if (name === 'chat.done') {
    return {
      outcome: {
        assistant: String(evt.assistant ?? ''),
        needsClarification: evt.needs_clarification === true,
        model: String(evt.model ?? ''),
        thinkingLevel: String(evt.thinking_level ?? 'auto'),
        quotaRpm: String(evt.quota_rpm ?? 'auto'),
        result: evt.result ? normalizeResponse(objectOf(evt.result)) : null,
      },
    }
  }
  if (name === 'chat.error') {
    return { error: errorFor(Number(evt.status) || 500, evt) }
  }
  return null
}

function chatFailure(controller, ms, events, why) {
  logError('api.failed', `POST ${CHAT_STREAM_ENDPOINT} stopped after ${events} events`, {
    ms,
    events,
    aborted: controller.signal.aborted,
    why,
  })
  if (controller.signal.aborted) return new ApiError('timeout', TIMEOUT_MESSAGE)
  return new ApiError('network', why)
}

/** One ADK orchestrator turn. Unlike generateStream this has no one-shot
    fallback: replaying an agent turn can duplicate a paid tool invocation. */
export async function chatStream(request, onEvent) {
  const body = JSON.stringify({ ...request, debug: true })
  guardSize(body)

  const controller = new AbortController()
  const timer = requestTimer(controller, request)
  const startedAt = Date.now()
  try {
    let response
    try {
      response = await fetch(CHAT_STREAM_ENDPOINT, {
        method: 'POST',
        headers: { 'content-type': 'application/json', accept: NDJSON_TYPE },
        body,
        signal: controller.signal,
      })
    } catch (err) {
      logError('api.failed', `POST ${CHAT_STREAM_ENDPOINT} never reached a response`, {
        ms: Date.now() - startedAt,
        aborted: controller.signal.aborted,
      })
      if (controller.signal.aborted) throw new ApiError('timeout', TIMEOUT_MESSAGE)
      throw new ApiError('network', String(err && err.message ? err.message : err))
    }

    if (!response.ok) {
      let data = {}
      let parsed = true
      try {
        data = await response.json()
      } catch {
        parsed = false
      }
      recordOutcome(CHAT_STREAM_ENDPOINT, response, Date.now() - startedAt, parsed, {
        stream: true,
      })
      throw errorFor(response.status, data || {})
    }

    const type = contentTypeOf(response)
    if (!isNdjson(type) || !response.body || typeof response.body.getReader !== 'function') {
      throw new ApiError('internal', 'The chat service did not return a readable event stream.', {
        status: response.status,
      })
    }

    logEvent('api.stream', `POST ${CHAT_STREAM_ENDPOINT} opened ${response.status}`, {
      status: response.status,
      ms: Date.now() - startedAt,
      content_type: type,
    })

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let carry = ''
    let events = 0
    let outcome = null
    let closed = false
    try {
      while (!outcome && !closed) {
        let chunk
        try {
          chunk = await reader.read()
        } catch (err) {
          throw chatFailure(
            controller,
            Date.now() - startedAt,
            events,
            String(err && err.message ? err.message : err),
          )
        }
        closed = Boolean(chunk.done)
        const text = closed ? `${decoder.decode()}\n` : decoder.decode(chunk.value, { stream: true })
        const step = parseNdjson(carry, text)
        carry = step.carry
        for (const evt of step.events) {
          events += 1
          emit(onEvent, evt)
          if (!outcome) outcome = chatTerminalOf(evt)
        }
      }
    } finally {
      releaseReader(reader)
    }

    const ms = Date.now() - startedAt
    if (outcome?.error) {
      const error = outcome.error
      recordOutcome(CHAT_STREAM_ENDPOINT, { status: error.status, ok: false }, ms, true, {
        stream: true,
        events,
      })
      throw error
    }
    if (outcome) {
      recordOutcome(CHAT_STREAM_ENDPOINT, response, ms, true, { stream: true, events })
      return outcome.outcome
    }
    throw chatFailure(controller, ms, events, 'The stream closed before the turn finished.')
  } finally {
    clearRequestTimer(timer)
  }
}

/** Server-filtered Gemini models. Auto remains the normal UI choice. */
export async function listModels() {
  let response
  try {
    response = await fetch(MODELS_ENDPOINT, { headers: { accept: 'application/json' } })
  } catch (err) {
    throw new ApiError('network', String(err && err.message ? err.message : err))
  }
  let data = {}
  try {
    data = await response.json()
  } catch {
    throw new ApiError('internal', 'The model catalog is not JSON.', { status: response.status })
  }
  if (!response.ok) throw errorFor(response.status, data || {})
  return {
    default: String(data.default ?? 'auto'),
    auto_model: String(data.auto_model ?? ''),
    source: String(data.source ?? ''),
    warning: String(data.warning ?? ''),
    models: asArray(data.models).map((model) => ({
      id: String(model?.id ?? ''),
      name: String(model?.name ?? model?.id ?? ''),
      description: String(model?.description ?? ''),
      input_token_limit: model?.input_token_limit ?? null,
      output_token_limit: model?.output_token_limit ?? null,
      thinking: model?.thinking ?? null,
    })).filter((model) => model.id),
  }
}
