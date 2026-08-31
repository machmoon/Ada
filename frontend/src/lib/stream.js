// The pipeline's NDJSON progress stream: bytes to events, events to sentences.
// Pure and DOM-free — the transport belongs to api.js and the rendering to the
// feed, so both halves stay testable without a server or a browser.

import { countOf, formatBoard, formatCount } from './format.js'

const MAX_RAW_CHARS = 200
const MAX_ERROR_CHARS = 160

/** One decoded chunk in; the events it completed plus the unfinished tail out.
    Feed the tail back as `carry` on the next call, starting from ''. Never
    throws: a line that is not a JSON object becomes a `client.badframe` event,
    so a malformed frame is something the feed can show rather than a dead run. */
export function parseNdjson(carry, chunk) {
  const segments = `${text(carry)}${text(chunk)}`.split('\n')
  const tail = segments.pop()
  const events = []
  for (const segment of segments) {
    const line = segment.trim()
    if (line) events.push(frameOf(line))
  }
  return { events, carry: tail }
}

function frameOf(line) {
  let frame
  try {
    frame = JSON.parse(line)
  } catch {
    return badFrame(line)
  }
  // A bare number, string, or array is well-formed JSON and still not an event.
  if (!frame || typeof frame !== 'object' || Array.isArray(frame)) return badFrame(line)
  return frame
}

function badFrame(line) {
  return { event: 'client.badframe', raw: clip(line, MAX_RAW_CHARS) }
}

const STAGE_START = {
  read: () => 'reading datasheets…',
  propose: () => 'proposing a circuit…',
  place: (e) => `placing with CP-SAT${budgetOf(e.time_limit_s)}…`,
  route: () => 'routing the copper…',
  review: () => 'adversarial review…',
}

const STAGE_DONE = {
  read: (e) =>
    `datasheets read: ${formatCount(countOf(e.parts), 'part')}, ` +
    `${formatCount(countOf(e.pins), 'pin')}, ` +
    `${formatCount(countOf(e.requirements), 'requirement')}`,

  propose: (e) => {
    const rounds = countOf(e.repair_rounds)
    const repaired = rounds > 0 ? `, after ${formatCount(rounds, 'repair round')}` : ''
    return `circuit proposed: ${formatCount(countOf(e.parts), 'part')}, ${formatCount(countOf(e.nets), 'net')}${repaired}`
  },

  place: (e) => {
    const warnings = countOf(e.warnings)
    const clauses = [
      text(e.solver_status),
      formatBoard(e.board_mm),
      wireOf(e.wirelength_mm),
      warnings > 0 ? formatCount(warnings, 'warning') : '',
    ].filter(Boolean)
    return clauses.length ? `placed: ${clauses.join(', ')}` : 'placed'
  },

  route: (e) => {
    const routed = countOf(e.routed_nets)
    const unrouted = countOf(e.unrouted_nets)
    // Say the unfinished count out loud. A net left as ratsnest is invisible
    // until fabrication, and "routed" with nothing after it reads as done.
    const clauses = [`${routed}/${routed + unrouted} nets`]
    if (countOf(e.tracks) > 0) clauses.push(formatCount(countOf(e.tracks), 'track'))
    if (countOf(e.vias) > 0) clauses.push(formatCount(countOf(e.vias), 'via'))
    if (unrouted > 0) clauses.push(`${unrouted} left unrouted`)
    return `routed: ${clauses.join(', ')}`
  },

  review: (e) => {
    const findings = countOf(e.findings)
    const blockers = countOf(e.blockers)
    const clauses = [findings > 0 ? formatCount(findings, 'finding') : 'no findings']
    if (blockers > 0) clauses.push(formatCount(blockers, 'blocker'))
    return `review: ${clauses.join(', ')}`
  },
}

const DESCRIBERS = {
  'chat.accepted': (e) =>
    `orchestrator started${text(e.model) ? ` with ${text(e.model)}` : ''}${text(e.thinking_level) ? ` · ${text(e.thinking_level)} thinking` : ''}${text(e.quota_rpm) && text(e.quota_rpm) !== 'auto' ? ` · ${text(e.quota_rpm)} RPM pace` : ''}`,

  'quota.wait': (e) => {
    const delay = seconds(e.delay_s)
    const layer = text(e.layer) || 'Gemini'
    const rpm = text(e.quota_rpm)
    return `${layer} waiting for quota pace${delay ? `: ${delay} s` : ''}${rpm ? ` at ${rpm} RPM` : ''}`
  },

  'run.accepted': () => 'request accepted, pipeline starting',

  'stage.start': (e) => stageSentence(STAGE_START, e, (stage) => `starting ${stage}…`),

  'stage.done': (e) => stageSentence(STAGE_DONE, e, (stage) => `${stage} finished`),

  'read.part': (e) => {
    const part = text(e.part)
    const counter = counterOf(e.index, e.total)
    if (e.cached) return `${part ? `${part}: ` : ''}facts already cached${counter}`
    return `reading ${part || 'a datasheet'}${counter}…`
  },

  'propose.round': (e) => {
    const round = num(e.round)
    const which = round === null ? '' : ` round ${round}`
    const first = clip(text(e.first_error), MAX_ERROR_CHARS)
    return `proposal${which} rejected: ${formatCount(countOf(e.errors), 'validation error')}${first ? ` (first: ${first})` : ''}`
  },

  'model.call': (e) => {
    const stage = whereOf(e.stage)
    const took = seconds(e.elapsed_s)
    if (!e.ok) return `model call${stage} failed${took ? ` after ${took} s` : ''}`
    // Either name identifies the call well enough; with neither, the sentence
    // still has to read as a sentence.
    const name = text(e.model) || text(e.provider)
    const chars = num(e.chars)
    const answered = `${name ? `${name} answered` : 'answered'}${took ? ` in ${took} s` : ''}`
    return `model call${stage}: ${answered}${chars === null ? '' : `, ${group(chars)} chars`}`
  },

  'model.request': (e) => {
    const layer = text(e.layer) || 'model'
    const model = text(e.model)
    return `${layer} prompt prepared${whereOf(e.stage)}${model ? ` for ${model}` : ''}`
  },

  // Only ever present on a debug run; the text itself belongs to the feed, so
  // the sentence reports its size and whether there was more of it.
  'model.response': (e) => {
    const counted = num(e.chars)
    // The event carries the answer, so its own length stands in when the count
    // is missing -- reporting nothing at all would read as a broken sentence.
    const chars = counted === null ? text(e.text).length : counted
    const clipped = e.truncated ? ' (truncated)' : ''
    return `response${whereOf(e.stage)}: ${group(chars)} chars${clipped}`
  },

  'model.retry': (e) => {
    const name = text(e.provider)
    const why = clip(text(e.error), MAX_ERROR_CHARS)
    const took = seconds(e.elapsed_s)
    return `provider${name ? ` ${name}` : ''} failed${why ? ` (${why})` : ''}${took ? ` after ${took} s` : ''}, trying next`
  },

  'tool.start': (e) => `orchestrator called ${text(e.tool) || 'a tool'}...`,

  'tool.done': (e) => `${text(e.tool) || 'tool'} finished`,

  'tool.error': (e) => {
    const why = clip(text(e.error), MAX_ERROR_CHARS)
    return `${text(e.tool) || 'tool'} failed${why ? `: ${why}` : ''}`
  },

  'ground.part': (e) => {
    const part = text(e.part)
    return `grounding${part ? ` ${part}` : ''} (${e.cached ? 'cached' : 'reading'} pages)`
  },

  'run.done': () => 'run complete',

  'run.error': (e) => {
    const status = num(e.status)
    const why = clip(text(e.error), MAX_ERROR_CHARS)
    return `run failed${status ? ` (${status})` : ''}${why ? `: ${why}` : ''}`
  },

  'assistant.message': () => 'orchestrator answered',

  'chat.done': (e) =>
    e.needs_clarification ? 'waiting for one clarification' : 'orchestrator turn complete',

  'chat.error': (e) => {
    const status = num(e.status)
    const why = clip(text(e.error), MAX_ERROR_CHARS)
    return `orchestrator failed${status ? ` (${status})` : ''}${why ? `: ${why}` : ''}`
  },

  'client.badframe': () => 'unparseable frame from server',
}

/** One event, one plain sentence for the chat feed. `t_s` is deliberately not
    in it: the feed renders its own time column. An event this does not know
    still gets a sentence, and nothing here throws on a missing field. */
export function describeStageEvent(evt) {
  const e = evt && typeof evt === 'object' ? evt : {}
  const key = text(e.event)
  const describe = lookup(DESCRIBERS, key)
  return (describe && describe(e)) || `pipeline event: ${key || 'unknown'}`
}

function stageSentence(table, evt, fallback) {
  const stage = text(evt.stage)
  const describe = lookup(table, stage)
  if (describe) return describe(evt)
  return stage ? fallback(stage) : ''
}

/** Own keys only: an event named `toString` must not reach Object.prototype. */
function lookup(table, key) {
  return Object.hasOwn(table, key) ? table[key] : null
}

function text(value) {
  if (typeof value === 'string') return value
  if (value == null || typeof value === 'symbol') return ''
  return String(value)
}

function clip(value, max) {
  return value.length > max ? value.slice(0, max) : value
}

/** null rather than 0 for an absent number, so a clause can be dropped whole
    instead of reporting a confident zero the server never sent. */
function num(value) {
  if (value == null || value === '') return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

function seconds(value) {
  const n = num(value)
  return n === null ? '' : n.toFixed(1)
}

function group(n) {
  return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ',')
}

function counterOf(index, total) {
  const i = num(index)
  const n = num(total)
  return i === null || n === null ? '' : ` (${i} of ${n})`
}

function budgetOf(value) {
  if (value === null) return ' (no solver budget)'
  const n = num(value)
  return n === null ? '' : ` (${n} s solver budget)`
}

function wireOf(value) {
  const n = num(value)
  return n === null ? '' : `${group(n)} mm wire`
}

function whereOf(stage) {
  const name = text(stage)
  return name ? ` (${name})` : ''
}
