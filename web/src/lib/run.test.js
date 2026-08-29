import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { get } from 'svelte/store'
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
    })
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
