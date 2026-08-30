import { derived, get, writable } from 'svelte/store'
import { logError, logEvent, logWarn, nextRunId, record, suspectEngineBug } from './log.js'

const IDLE = { phase: 'idle', request: null, result: null, error: null, startedAt: 0, id: '' }

/** phase: idle | running | done | error */
export const run = writable(IDLE)

export function startRun(request) {
  const id = nextRunId()
  const intent = String(request?.intent ?? '')
  logEvent('run.start', `run ${id} started`, {
    id,
    intent,
    intent_chars: intent.length,
    datasheets: Object.keys(request?.datasheets || {}).length,
    time_limit_s: request?.time_limit_s ?? null,
    review: request?.review !== false,
    // normalizeRequest omits `ground` unless it was asked for, so absent is false.
    ground: request?.ground === true,
  })
  run.set({ phase: 'running', request, result: null, error: null, startedAt: Date.now(), id })
}

export function finishRun(result) {
  logDone(get(run), result)
  run.update((state) => ({ ...state, phase: 'done', result, error: null }))
}

export function failRun(error) {
  logFailure(get(run), error)
  run.update((state) => ({ ...state, phase: 'error', result: null, error }))
}

export function resetRun() {
  const { id } = get(run)
  // log.js has no debug helper: this line is noise unless you are chasing a
  // state machine, which is exactly what the level is for.
  record({ level: 'debug', src: 'app', event: 'run.reset', msg: 'run cleared', data: { id } })
  run.update((state) => ({ ...IDLE, request: state.request }))
}

/** The only wall-clock the client owns, on the same clock as `startedAt` so
    fake timers move both. Zero when no run was started to measure from. */
function clientMs(state) {
  return state.startedAt ? Date.now() - state.startedAt : 0
}

function numberOr(value, fallback) {
  const n = Number(value)
  return Number.isFinite(n) ? n : fallback
}

function listOf(value) {
  return Array.isArray(value) ? value : []
}

/** Short part-number lists; which part came from cache is the whole signal. */
function cacheOf(result) {
  const cache = result?.cache
  if (!cache || typeof cache !== 'object') return null
  return { hit: listOf(cache.hit), read: listOf(cache.read), unusable: listOf(cache.unusable) }
}

// The one place `served_by` and `cache` surface at all: nothing in the UI
// renders them, and the pipeline logs nothing server-side.
function logDone(state, result) {
  const ms = clientMs(state)
  const serverS = numberOr(result?.duration_s, 0)
  const blockers = listOf(result?.blockers)
  const warnings = listOf(result?.warnings)
  const status = String(result?.status ?? '')
  const pcb = typeof result?.kicad_pcb === 'string' ? result.kicad_pcb : ''
  const note = status === 'fallback' || blockers.length ? logWarn : logEvent
  note('run.done', `run ${state.id} finished ${status || 'with no status'} in ${ms} ms`, {
    id: state.id,
    client_ms: ms,
    server_s: serverS,
    // duration_s starts inside the service's generate(), so the difference is
    // transport plus JSON, which is the number worth watching.
    overhead_ms: ms - Math.round(serverS * 1000),
    status,
    served_by: result?.served_by ?? null,
    cache: cacheOf(result),
    repair_rounds: numberOr(result?.repair_rounds, 0),
    parts: listOf(result?.parts).length,
    nets: listOf(result?.nets).length,
    findings: listOf(result?.findings).length,
    blockers: blockers.length,
    warnings: warnings.length,
    first_warning: warnings.length ? String(warnings[0]) : '',
    wirelength_mm: result?.wirelength_mm ?? null,
    // The board text is by far the largest thing a response carries. Its size
    // is the useful part, so only the size is kept.
    has_pcb: pcb.length > 0,
    pcb_chars: pcb.length,
  })
}

function logFailure(state, error) {
  const kind = String(error?.kind ?? '')
  const status = numberOr(error?.status, 0)
  const message = String(error?.message ?? error ?? '')
  const suspect = suspectEngineBug(status, message)
  const msg = suspect
    ? `run ${state.id} failed (${kind}). This looks like known issue 10: an internal engine error surfaced as a 400.`
    : `run ${state.id} failed (${kind})`
  logError('run.error', msg, {
    id: state.id,
    client_ms: clientMs(state),
    kind,
    status,
    errorId: String(error?.errorId ?? ''),
    message,
    suspect_engine_bug: suspect,
  })
}

/** Milliseconds since the run started; the interval exists only while running. */
export const elapsed = derived(
  run,
  ($run, set) => {
    if ($run.phase !== 'running') {
      set(0)
      return
    }
    set(Date.now() - $run.startedAt)
    const id = setInterval(() => set(Date.now() - $run.startedAt), 250)
    return () => clearInterval(id)
  },
  0,
)
