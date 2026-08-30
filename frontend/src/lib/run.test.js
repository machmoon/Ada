import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { get } from 'svelte/store'
import { clearLog, log } from './log.js'
import { elapsed, failRun, finishRun, resetRun, run, startRun } from './run.js'

// The store is a module singleton, so the idle value has to be captured at import
// time, before any test mutates it. Every test then starts from this exact object.
const IDLE = get(run)

/** Collects every value the elapsed store pushes, and cleans itself up. */
function watchElapsed() {
  const seen = []
  const unsubscribe = elapsed.subscribe((value) => seen.push(value))
  return { seen, unsubscribe, last: () => seen[seen.length - 1] }
}

beforeEach(() => {
  run.set(IDLE)
  clearLog()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('the run store', () => {
  it('starts idle with nothing to show', () => {
    expect(IDLE).toEqual({
      phase: 'idle',
      request: null,
      result: null,
      error: null,
      startedAt: 0,
      id: '',
    })
  })
})

// Every entry the store writes goes through log.js's buffer, whose appends are
// batched onto a microtask; nothing is readable until that queue drains.
async function flushed() {
  await Promise.resolve()
  return get(log).entries
}

function lastEvent(entries, event) {
  return entries.filter((entry) => entry.event === event).pop()
}

describe('what the run store logs', () => {
  // Run ids come from a module-level counter that no API resets, so this is the
  // one test that can name an id: it is the first in the file to start a run.
  it('records the request shape under run.start, and mints r1 for the first run', async () => {
    startRun({
      intent: 'a 3v3 regulator',
      datasheets: { U1: 'https://example.com/u1.pdf' },
      time_limit_s: 20,
      review: true,
      ground: true,
    })

    const entry = lastEvent(await flushed(), 'run.start')

    expect(entry.level).toBe('info')
    expect(entry.data).toEqual({
      id: 'r1',
      intent: 'a 3v3 regulator',
      intent_chars: 15,
      datasheets: 1,
      time_limit_s: 20,
      review: true,
      ground: true,
    })
    expect(get(run).id).toBe('r1')
  })

  it('reads an absent ground flag as not grounded, the way the service does', async () => {
    startRun({ intent: 'x', datasheets: {}, time_limit_s: 5, review: false })

    expect(lastEvent(await flushed(), 'run.start').data).toMatchObject({
      review: false,
      ground: false,
    })
  })

  it('records the fields no other surface shows under run.done', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(1_700_000_000_000)
    startRun({ intent: 'x' })
    vi.setSystemTime(1_700_000_004_000)

    finishRun({
      status: 'feasible',
      served_by: 'gemini-3.7-flash',
      cache: { hit: ['STM32F103'], read: [], unusable: [] },
      repair_rounds: 2,
      duration_s: 3.5,
      parts: [{ ref: 'U1' }, { ref: 'C1' }],
      nets: ['vcc'],
      findings: [{ title: 'a' }],
      blockers: [],
      warnings: ['no decoupling on U1'],
      wirelength_mm: 41.2,
    })

    const entry = lastEvent(await flushed(), 'run.done')

    expect(entry.level).toBe('info')
    expect(entry.data).toMatchObject({
      id: get(run).id,
      client_ms: 4000,
      server_s: 3.5,
      overhead_ms: 500,
      status: 'feasible',
      served_by: 'gemini-3.7-flash',
      cache: { hit: ['STM32F103'], read: [], unusable: [] },
      repair_rounds: 2,
      parts: 2,
      nets: 1,
      findings: 1,
      blockers: 0,
      warnings: 1,
      first_warning: 'no decoupling on U1',
      wirelength_mm: 41.2,
    })
  })

  it('warns rather than informs when the placer fell back', async () => {
    startRun({ intent: 'x' })
    finishRun({ status: 'fallback', blockers: [] })

    expect(lastEvent(await flushed(), 'run.done').level).toBe('warn')
  })

  it('warns when the review found a blocker', async () => {
    startRun({ intent: 'x' })
    finishRun({ status: 'feasible', blockers: ['U1 has no decoupling capacitor'] })

    const entry = lastEvent(await flushed(), 'run.done')

    expect(entry.level).toBe('warn')
    expect(entry.data.blockers).toBe(1)
  })

  it('keeps the done entry small when the response carries a 200 KB board', async () => {
    const kicad_pcb = 'x'.repeat(200_000)
    startRun({ intent: 'x' })

    finishRun({ status: 'feasible', kicad_pcb })

    const entry = lastEvent(await flushed(), 'run.done')

    expect(entry.data).toMatchObject({ has_pcb: true, pcb_chars: 200_000 })
    expect(JSON.stringify(entry)).not.toContain('xxxxx')
    expect(JSON.stringify(entry).length).toBeLessThan(1024)
  })

  it('reports no board when the response carries none', async () => {
    startRun({ intent: 'x' })
    finishRun({ status: 'feasible' })

    expect(lastEvent(await flushed(), 'run.done').data).toMatchObject({
      has_pcb: false,
      pcb_chars: 0,
    })
  })

  it('records the kind, status and error id under run.error', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(1_700_000_000_000)
    startRun({ intent: 'x' })
    vi.setSystemTime(1_700_000_000_250)

    failRun({ name: 'ApiError', kind: 'internal', status: 500, errorId: 'e-42', message: 'boom' })

    const entry = lastEvent(await flushed(), 'run.error')

    expect(entry.level).toBe('error')
    expect(entry.data).toEqual({
      id: get(run).id,
      client_ms: 250,
      kind: 'internal',
      status: 500,
      errorId: 'e-42',
      message: 'boom',
      suspect_engine_bug: false,
    })
  })

  it('names known issue 10 when a 400 mentions no request field', async () => {
    startRun({ intent: 'x' })

    failRun({ kind: 'validation', status: 400, message: 'not enough values to unpack' })

    const entry = lastEvent(await flushed(), 'run.error')

    expect(entry.data.suspect_engine_bug).toBe(true)
    expect(entry.msg).toContain('known issue 10')
  })

  it('leaves a plain field-validation 400 unflagged', async () => {
    startRun({ intent: 'x' })

    failRun({ kind: 'validation', status: 400, message: 'intent must be a string' })

    const entry = lastEvent(await flushed(), 'run.error')

    expect(entry.data.suspect_engine_bug).toBe(false)
    expect(entry.msg).not.toContain('known issue 10')
  })

  it('records run.reset at debug level, naming the run it cleared', async () => {
    startRun({ intent: 'x' })
    const { id } = get(run)

    resetRun()

    const entry = lastEvent(await flushed(), 'run.reset')

    expect(entry.level).toBe('debug')
    expect(entry.data).toEqual({ id })
  })
})

describe('startRun', () => {
  it('enters the running phase and holds on to the request', () => {
    const request = { intent: 'a 3v3 regulator' }

    startRun(request)

    expect(get(run).phase).toBe('running')
    expect(get(run).request).toBe(request)
  })

  it('stamps the start time so the ticker has an origin', () => {
    vi.useFakeTimers()
    vi.setSystemTime(1_700_000_000_000)

    startRun({ intent: 'x' })

    expect(get(run).startedAt).toBe(1_700_000_000_000)
  })

  it('clears the previous result and error, so a retry shows a clean slate', () => {
    startRun({ intent: 'first' })
    failRun(new Error('boom'))

    startRun({ intent: 'second' })

    expect(get(run).result).toBeNull()
    expect(get(run).error).toBeNull()
  })
})

describe('finishRun', () => {
  it('moves a running run to done and carries the result', () => {
    const result = { status: 'feasible' }
    startRun({ intent: 'x' })

    finishRun(result)

    expect(get(run).phase).toBe('done')
    expect(get(run).result).toBe(result)
    expect(get(run).error).toBeNull()
  })

  it('keeps the request, which the chrome reads to label the run', () => {
    const request = { intent: 'x', review: false }
    startRun(request)

    finishRun({ status: 'feasible' })

    expect(get(run).request).toBe(request)
  })

  it('keeps the start time', () => {
    startRun({ intent: 'x' })
    const { startedAt } = get(run)

    finishRun({ status: 'feasible' })

    expect(get(run).startedAt).toBe(startedAt)
  })

  it('walks idle to running to done', () => {
    const phases = []
    const unsubscribe = run.subscribe((state) => phases.push(state.phase))

    startRun({ intent: 'x' })
    finishRun({ status: 'feasible' })
    unsubscribe()

    expect(phases).toEqual(['idle', 'running', 'done'])
  })
})

describe('failRun', () => {
  it('moves a running run to error and carries the error', () => {
    const error = new Error('upstream is down')
    startRun({ intent: 'x' })

    failRun(error)

    expect(get(run).phase).toBe('error')
    expect(get(run).error).toBe(error)
    expect(get(run).result).toBeNull()
  })

  it('keeps the request, which the error panel retries with', () => {
    const request = { intent: 'x' }
    startRun(request)

    failRun(new Error('boom'))

    expect(get(run).request).toBe(request)
  })

  it('walks idle to running to error', () => {
    const phases = []
    const unsubscribe = run.subscribe((state) => phases.push(state.phase))

    startRun({ intent: 'x' })
    failRun(new Error('boom'))
    unsubscribe()

    expect(phases).toEqual(['idle', 'running', 'error'])
  })

  it('drops a result left over from an earlier success', () => {
    startRun({ intent: 'x' })
    finishRun({ status: 'feasible' })

    failRun(new Error('the retry failed'))

    expect(get(run).result).toBeNull()
  })
})

describe('resetRun', () => {
  it('returns to idle', () => {
    startRun({ intent: 'x' })
    finishRun({ status: 'feasible' })

    resetRun()

    expect(get(run).phase).toBe('idle')
    expect(get(run).result).toBeNull()
    expect(get(run).error).toBeNull()
    expect(get(run).startedAt).toBe(0)
  })

  it('keeps the request, so dismissing an error leaves the form filled in', () => {
    const request = { intent: 'a 3v3 regulator' }
    startRun(request)
    failRun(new Error('boom'))

    resetRun()

    expect(get(run).request).toBe(request)
  })
})

describe('the elapsed ticker', () => {
  it('reads zero while idle', () => {
    const watcher = watchElapsed()

    expect(watcher.last()).toBe(0)

    watcher.unsubscribe()
  })

  it('runs no interval while idle', () => {
    vi.useFakeTimers()

    const watcher = watchElapsed()

    expect(vi.getTimerCount()).toBe(0)

    watcher.unsubscribe()
  })

  it('publishes zero the moment a run starts, before the first tick', () => {
    vi.useFakeTimers()
    const watcher = watchElapsed()

    startRun({ intent: 'x' })

    expect(watcher.last()).toBe(0)

    watcher.unsubscribe()
  })

  it('ticks every 250 ms while running', () => {
    vi.useFakeTimers()
    const watcher = watchElapsed()
    startRun({ intent: 'x' })

    vi.advanceTimersByTime(250)
    expect(watcher.last()).toBe(250)

    vi.advanceTimersByTime(250)
    expect(watcher.last()).toBe(500)

    vi.advanceTimersByTime(750)
    expect(watcher.last()).toBe(1250)

    watcher.unsubscribe()
  })

  it('measures from the start of the run, not from the first subscription', () => {
    vi.useFakeTimers()
    startRun({ intent: 'x' })
    vi.advanceTimersByTime(4000)

    const watcher = watchElapsed()

    expect(watcher.last()).toBe(4000)

    watcher.unsubscribe()
  })

  it('starts exactly one interval no matter how many subscribers there are', () => {
    vi.useFakeTimers()
    const first = watchElapsed()
    const second = watchElapsed()

    startRun({ intent: 'x' })

    expect(vi.getTimerCount()).toBe(1)

    first.unsubscribe()
    second.unsubscribe()
  })

  it('clears the interval when the run finishes', () => {
    vi.useFakeTimers()
    const watcher = watchElapsed()
    startRun({ intent: 'x' })
    expect(vi.getTimerCount()).toBe(1)

    finishRun({ status: 'feasible' })

    expect(vi.getTimerCount()).toBe(0)

    watcher.unsubscribe()
  })

  it('clears the interval when the run fails', () => {
    vi.useFakeTimers()
    const watcher = watchElapsed()
    startRun({ intent: 'x' })
    expect(vi.getTimerCount()).toBe(1)

    failRun(new Error('boom'))

    expect(vi.getTimerCount()).toBe(0)

    watcher.unsubscribe()
  })

  it('clears the interval on reset', () => {
    vi.useFakeTimers()
    const watcher = watchElapsed()
    startRun({ intent: 'x' })

    resetRun()

    expect(vi.getTimerCount()).toBe(0)

    watcher.unsubscribe()
  })

  it('stops publishing once the phase leaves running', () => {
    vi.useFakeTimers()
    const watcher = watchElapsed()
    startRun({ intent: 'x' })
    vi.advanceTimersByTime(1000)

    finishRun({ status: 'feasible' })
    const settled = watcher.seen.length
    vi.advanceTimersByTime(10_000)

    expect(watcher.seen).toHaveLength(settled)
    expect(watcher.last()).toBe(0)

    watcher.unsubscribe()
  })

  it('clears the interval when the last subscriber goes away mid-run', () => {
    vi.useFakeTimers()
    const watcher = watchElapsed()
    startRun({ intent: 'x' })
    expect(vi.getTimerCount()).toBe(1)

    watcher.unsubscribe()

    expect(vi.getTimerCount()).toBe(0)
  })

  it('leaks no timer across a full run and a second run after it', () => {
    vi.useFakeTimers()
    const watcher = watchElapsed()

    startRun({ intent: 'first' })
    vi.advanceTimersByTime(500)
    finishRun({ status: 'feasible' })
    startRun({ intent: 'second' })
    vi.advanceTimersByTime(500)
    finishRun({ status: 'feasible' })

    expect(vi.getTimerCount()).toBe(0)

    watcher.unsubscribe()
    expect(vi.getTimerCount()).toBe(0)
  })
})
