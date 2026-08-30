import { derived, writable } from 'svelte/store'

const IDLE = { phase: 'idle', request: null, result: null, error: null, startedAt: 0 }

/** phase: idle | running | done | error */
export const run = writable(IDLE)

export function startRun(request) {
  run.set({ phase: 'running', request, result: null, error: null, startedAt: Date.now() })
}

export function finishRun(result) {
  run.update((state) => ({ ...state, phase: 'done', result, error: null }))
}

export function failRun(error) {
  run.update((state) => ({ ...state, phase: 'error', result: null, error }))
}

export function resetRun() {
  run.update((state) => ({ ...IDLE, request: state.request }))
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
