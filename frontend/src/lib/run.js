import { derived, get, writable } from 'svelte/store'
import { countOf } from './format.js'
import { logError, logEvent, logWarn, nextRunId, record, suspectEngineBug } from './log.js'
import { describeStageEvent } from './stream.js'

/** Longest feed a run keeps. A pipeline emits tens of events, not hundreds, so
    this is a bound against a pathological server rather than a display limit. */
export const MAX_FEED = 500

const IDLE = {
  phase: 'idle',
  request: null,
  result: null,
  error: null,
  startedAt: 0,
  id: '',
  // Keyed by backend stage name, plus the synthetic `validate` — see stageEvent.
  stages: {},
  feed: [],
}

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
  run.set({
    phase: 'running',
    request,
    result: null,
    error: null,
    startedAt: Date.now(),
    id,
    stages: {},
    feed: [],
  })
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
  // Fresh containers rather than IDLE's own: nothing may end up sharing the
  // holder every future run starts from.
  run.update((state) => ({ ...IDLE, stages: {}, feed: [], request: state.request }))
}

// ------------------------------------------------------- the live pipeline

/** What each finished stage keeps: enough to label the row, never the payload
    itself. The feed already carries the sentence, and the debug log the frame. */
const DONE_SUMMARY = {
  read: (e) => ({
    parts: countOf(e.parts),
    pins: countOf(e.pins),
    requirements: countOf(e.requirements),
  }),
  propose: (e) => ({
    parts: countOf(e.parts),
    nets: countOf(e.nets),
    repair_rounds: countOf(e.repair_rounds),
  }),
  place: (e) => ({
    solver_status: String(e.solver_status ?? ''),
    board_mm: listOf(e.board_mm),
    wirelength_mm: finiteOrNull(e.wirelength_mm),
    warnings: countOf(e.warnings),
  }),
  review: (e) => ({ findings: countOf(e.findings), blockers: countOf(e.blockers) }),
}

/** The solver budget is the one thing a starting stage reports. */
const START_SUMMARY = {
  place: (e) => ({ time_limit_s: finiteOrNull(e.time_limit_s) }),
}

/** Events that are not, in themselves, bad news default to info. */
const LEVEL_OF = {
  'run.error': 'error',
  'model.retry': 'warn',
  'client.badframe': 'warn',
}

let feedSeq = 0

/** One event from the pipeline stream: it moves the stage list, appends the
    sentence to the feed, and leaves the raw frame in the debug log. The whole
    transition is a merge, so an event that names nothing it knows changes
    nothing but the feed. */
export function stageEvent(evt) {
  const event = evt && typeof evt === 'object' ? evt : {}
  const name = String(event.event ?? '')
  const text = describeStageEvent(event)

  // record() rather than logEvent(): the level helpers all stamp src 'app', and
  // this line came from the server, which is the distinction worth keeping.
  record({
    level: own(LEVEL_OF, name) || 'info',
    src: 'server',
    event: `pipeline.${name || 'unknown'}`,
    msg: text,
    data: event,
  })

  run.update((state) => ({
    ...state,
    stages: nextStages(state.stages || {}, name, event),
    feed: appendFeed(state.feed || [], {
      id: (feedSeq += 1),
      t_s: finiteOrNull(event.t_s),
      text,
      event: name,
      // The feed is the one place a raw answer lives whole: the debug log
      // clips every string it records, so the row keeps its own copy. Set
      // only where there is one, so no other row carries a dead field.
      ...(name === 'model.response' ? { detail: String(event.text ?? '') } : {}),
    }),
  }))
}

function nextStages(stages, name, evt) {
  const at = finiteOrNull(evt.t_s)
  const stage = String(evt.stage ?? '')

  if (name === 'stage.start') {
    if (!stage) return stages
    return {
      ...stages,
      [stage]: { state: 'running', t_s: at, ...summaryOf(START_SUMMARY, stage, evt) },
    }
  }

  if (name === 'stage.done') {
    if (!stage) return stages
    const next = {
      ...stages,
      [stage]: { state: 'done', t_s: at, ...summaryOf(DONE_SUMMARY, stage, evt) },
    }
    // Validation and repair have no stage of their own on the wire: they run
    // inside propose, so propose finishing is the only report that they
    // finished — and a proposal accepted on the first try was still validated.
    if (stage === 'propose') {
      next.validate = { state: 'done', t_s: at, rounds: roundsIn(stages) }
    }
    return next
  }

  // A rejected proposal is the one thing that proves the repair loop is turning.
  if (name === 'propose.round') {
    return { ...stages, validate: { state: 'running', t_s: at, rounds: roundsIn(stages) + 1 } }
  }

  return stages
}

function roundsIn(stages) {
  const rounds = Number(stages?.validate?.rounds)
  return Number.isFinite(rounds) ? rounds : 0
}

function summaryOf(table, stage, evt) {
  return own(table, stage) ? table[stage](evt) : {}
}

/** Own keys only: a stage named `constructor` must not reach Object.prototype. */
function own(table, key) {
  return Object.hasOwn(table, key) ? table[key] : null
}

function appendFeed(feed, entry) {
  const next = [...feed, entry]
  return next.length > MAX_FEED ? next.slice(next.length - MAX_FEED) : next
}

/** null rather than 0 for an absent number: a stage that reported no time is
    not a stage that reported zero. */
function finiteOrNull(value) {
  if (value == null || value === '') return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
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
