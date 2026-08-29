import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  ApiError,
  MAX_REQUEST_BYTES,
  MAX_TIME_LIMIT_S,
  MIN_TIME_LIMIT_S,
  REQUEST_TIMEOUT_MS,
  generate,
  normalizeRequest,
  requestBytes,
} from './api.js'

/** A fetch Response is duck-typed here: generate() only reads ok, status and json(). */
function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  }
}

function unparseableResponse(status) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => {
      throw new SyntaxError('Unexpected token in JSON at position 0')
    },
  }
}

function stubFetch(response) {
  const fetch = vi.fn(async () => response)
  vi.stubGlobal('fetch', fetch)
  return fetch
}

function stubFetchRejecting(error) {
  const fetch = vi.fn(async () => {
    throw error
  })
  vi.stubGlobal('fetch', fetch)
  return fetch
}

/** The minimum a successful response needs; every field is optional to the normalizer. */
const OK_BODY = { status: 'feasible', board_mm: [20, 30], parts: [{ ref: 'U1' }] }

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('normalizeRequest', () => {
  it('trims the intent', () => {
    expect(normalizeRequest({ intent: '  a 3v3 regulator  ' }).intent).toBe('a 3v3 regulator')
  })

  it('treats a missing intent as empty rather than the string "undefined"', () => {
    expect(normalizeRequest({}).intent).toBe('')
    expect(normalizeRequest({ intent: null }).intent).toBe('')
  })

  it('keeps a datasheet row that has both a part and a url', () => {
    const request = normalizeRequest({ datasheets: { STM32F103: 'https://example.com/ds.pdf' } })

    expect(request.datasheets).toEqual({ STM32F103: 'https://example.com/ds.pdf' })
  })

  it('trims both sides of a datasheet row', () => {
    const request = normalizeRequest({ datasheets: { '  U1  ': '  https://e.com/a.pdf  ' } })

    expect(request.datasheets).toEqual({ U1: 'https://e.com/a.pdf' })
  })

  it('drops a half-filled datasheet row', () => {
    const request = normalizeRequest({
      datasheets: { KeptPart: 'https://e.com/a.pdf', NoUrl: '', '': 'https://e.com/b.pdf' },
    })

    expect(request.datasheets).toEqual({ KeptPart: 'https://e.com/a.pdf' })
  })

  it('drops a row whose halves are only whitespace', () => {
    expect(normalizeRequest({ datasheets: { '   ': '   ' } }).datasheets).toEqual({})
  })

  it('produces an empty datasheet map when none were given', () => {
    expect(normalizeRequest({}).datasheets).toEqual({})
    expect(normalizeRequest({ datasheets: null }).datasheets).toEqual({})
  })

  it('leaves a solver budget inside the accepted range alone', () => {
    expect(normalizeRequest({ time_limit_s: 30 }).time_limit_s).toBe(30)
  })

  it('keeps both ends of the accepted range', () => {
    expect(normalizeRequest({ time_limit_s: MIN_TIME_LIMIT_S }).time_limit_s).toBe(5)
    expect(normalizeRequest({ time_limit_s: MAX_TIME_LIMIT_S }).time_limit_s).toBe(60)
  })

  it('clamps a solver budget below the floor up to five seconds', () => {
    expect(normalizeRequest({ time_limit_s: 4 }).time_limit_s).toBe(5)
    expect(normalizeRequest({ time_limit_s: 0 }).time_limit_s).toBe(5)
    expect(normalizeRequest({ time_limit_s: -100 }).time_limit_s).toBe(5)
  })

  it('clamps a solver budget above the ceiling down to sixty seconds', () => {
    expect(normalizeRequest({ time_limit_s: 61 }).time_limit_s).toBe(60)
    expect(normalizeRequest({ time_limit_s: 100000 }).time_limit_s).toBe(60)
  })

  it('rounds a fractional solver budget to a whole second', () => {
    expect(normalizeRequest({ time_limit_s: 12.4 }).time_limit_s).toBe(12)
    expect(normalizeRequest({ time_limit_s: 12.6 }).time_limit_s).toBe(13)
  })

  it('accepts a numeric string, since a number input yields one', () => {
    expect(normalizeRequest({ time_limit_s: '20' }).time_limit_s).toBe(20)
  })

  it('falls back to the floor when the solver budget is missing or unparseable', () => {
    expect(normalizeRequest({}).time_limit_s).toBe(5)
    expect(normalizeRequest({ time_limit_s: 'soon' }).time_limit_s).toBe(5)
    expect(normalizeRequest({ time_limit_s: Infinity }).time_limit_s).toBe(5)
  })

  it('runs the review unless it was explicitly switched off', () => {
    expect(normalizeRequest({}).review).toBe(true)
    expect(normalizeRequest({ review: true }).review).toBe(true)
    expect(normalizeRequest({ review: undefined }).review).toBe(true)
  })

  it('honours an explicit false for the review', () => {
    expect(normalizeRequest({ review: false }).review).toBe(false)
  })

  it('emits exactly the four fields the service accepts, dropping anything else', () => {
    const request = normalizeRequest({ intent: 'x', nonsense: 'drop me' })

    expect(Object.keys(request).sort()).toEqual(['datasheets', 'intent', 'review', 'time_limit_s'])
  })
})

describe('requestBytes', () => {
  it('measures the serialised request', () => {
    expect(requestBytes({ intent: 'ab' })).toBe(JSON.stringify({ intent: 'ab' }).length)
  })

  it('counts utf-8 bytes, not characters, so a multibyte intent is not undercounted', () => {
    const request = { intent: 'µ' }

    // The micro sign is one character but two bytes, so the byte count runs ahead.
    expect(JSON.stringify(request)).toHaveLength(14)
    expect(requestBytes(request)).toBe(15)
  })
})

describe('generate: the request it sends', () => {
  it('posts JSON to the same-origin generate endpoint under an abort signal', async () => {
    const fetch = stubFetch(jsonResponse(200, OK_BODY))
    const request = { intent: 'a 3v3 regulator', time_limit_s: 20 }

    await generate(request)

    expect(fetch).toHaveBeenCalledTimes(1)
    const [url, init] = fetch.mock.calls[0]
    expect(url).toBe('/generate')
    expect(init.method).toBe('POST')
    expect(init.headers).toEqual({ 'content-type': 'application/json' })
    expect(init.body).toBe(JSON.stringify(request))
    expect(init.signal).toBeInstanceOf(AbortSignal)
    expect(init.signal.aborted).toBe(false)
  })

  it('sends the request as given, leaving normalisation to the caller', async () => {
    const fetch = stubFetch(jsonResponse(200, OK_BODY))

    await generate({ time_limit_s: 9999 })

    expect(JSON.parse(fetch.mock.calls[0][1].body).time_limit_s).toBe(9999)
  })
})

describe('generate: the 1 MiB client-side guard', () => {
  it('rejects an oversized request without troubling the network', async () => {
    const fetch = stubFetch(jsonResponse(200, OK_BODY))
    const request = { intent: 'x'.repeat(MAX_REQUEST_BYTES) }

    await expect(generate(request)).rejects.toMatchObject({
      name: 'ApiError',
      kind: 'too-large',
      status: 0,
    })
    expect(fetch).not.toHaveBeenCalled()
  })

  it('names the actual and the permitted size, so the user can tell how far over they are', async () => {
    stubFetch(jsonResponse(200, OK_BODY))
    const request = { intent: 'x'.repeat(MAX_REQUEST_BYTES) }
    const bytes = requestBytes(request)

    await expect(generate(request)).rejects.toThrow(
      `The request is ${bytes} bytes; the service accepts at most ${MAX_REQUEST_BYTES}.`,
    )
  })

  it('lets a request that is exactly at the limit through', async () => {
    const fetch = stubFetch(jsonResponse(200, OK_BODY))
    const overhead = requestBytes({ intent: '' })
    const request = { intent: 'x'.repeat(MAX_REQUEST_BYTES - overhead) }
    expect(requestBytes(request)).toBe(MAX_REQUEST_BYTES)

    await generate(request)

    expect(fetch).toHaveBeenCalledTimes(1)
  })
})

describe('generate: a successful response', () => {
  it('returns the body with the fields the UI indexes into', async () => {
    stubFetch(
      jsonResponse(200, {
        status: 'feasible',
        board_mm: [20.5, 30.5],
        parts: [{ ref: 'U1' }, { ref: 'C1' }],
        duration_s: 12.5,
        repair_rounds: 2,
      }),
    )

    const result = await generate({ intent: 'x' })

    expect(result.status).toBe('feasible')
    expect(result.board_mm).toEqual([20.5, 30.5])
    expect(result.parts).toHaveLength(2)
    expect(result.duration_s).toBe(12.5)
    expect(result.repair_rounds).toBe(2)
  })

  it('substitutes an empty array for every list the response omits', async () => {
    stubFetch(jsonResponse(200, { status: 'feasible' }))

    const result = await generate({ intent: 'x' })

    expect(result.board_mm).toEqual([])
    expect(result.parts).toEqual([])
    expect(result.blockers).toEqual([])
    expect(result.warnings).toEqual([])
    expect(result.nets).toEqual([])
    expect(result.datasheets).toEqual([])
    expect(result.findings).toEqual([])
  })

  it('replaces a non-array list with an empty array rather than letting .length throw', async () => {
    stubFetch(jsonResponse(200, { parts: 'eleven', warnings: null, nets: { a: 1 } }))

    const result = await generate({ intent: 'x' })

    expect(result.parts).toEqual([])
    expect(result.warnings).toEqual([])
    expect(result.nets).toEqual([])
  })

  it('defaults the two numeric fields to zero when absent', async () => {
    stubFetch(jsonResponse(200, {}))

    const result = await generate({ intent: 'x' })

    expect(result.repair_rounds).toBe(0)
    expect(result.duration_s).toBe(0)
  })

  it('preserves fields it does not know about', async () => {
    stubFetch(jsonResponse(200, { pcb_path: '/tmp/out.kicad_pcb' }))

    const result = await generate({ intent: 'x' })

    expect(result.pcb_path).toBe('/tmp/out.kicad_pcb')
  })

  it('clears the timeout timer once the response lands', async () => {
    vi.useFakeTimers()
    stubFetch(jsonResponse(200, OK_BODY))

    await generate({ intent: 'x' })

    expect(vi.getTimerCount()).toBe(0)
  })
})

describe('generate: findings synthesized from blockers', () => {
  it('passes a real findings array through untouched', async () => {
    const findings = [{ severity: 'blocker', title: 'VDD tied to ground', detail: 'd' }]
    stubFetch(jsonResponse(200, { findings, blockers: ['ignored'] }))

    const result = await generate({ intent: 'x' })

    expect(result.findings).toEqual(findings)
  })

  it('prefers an empty findings array over synthesizing from blockers', async () => {
    stubFetch(jsonResponse(200, { findings: [], blockers: ['a blocker'] }))

    const result = await generate({ intent: 'x' })

    expect(result.findings).toEqual([])
  })

  it('builds a blocker-severity card per blocker string when findings are absent', async () => {
    stubFetch(jsonResponse(200, { blockers: ['VDD tied to ground', 'no decoupling on U1'] }))

    const result = await generate({ intent: 'x' })

    expect(result.findings).toEqual([
      {
        severity: 'blocker',
        title: 'VDD tied to ground',
        detail: '',
        parts: [],
        citation: '',
        suggested_fix: '',
      },
      {
        severity: 'blocker',
        title: 'no decoupling on U1',
        detail: '',
        parts: [],
        citation: '',
        suggested_fix: '',
      },
    ])
  })

  it('leaves the flattened blockers in place alongside the synthesized cards', async () => {
    stubFetch(jsonResponse(200, { blockers: ['VDD tied to ground'] }))

    const result = await generate({ intent: 'x' })

    expect(result.blockers).toEqual(['VDD tied to ground'])
  })

  it('gives every synthesized card the empty part list a finding card expects', async () => {
    stubFetch(jsonResponse(200, { blockers: ['one'] }))

    const [finding] = (await generate({ intent: 'x' })).findings

    expect(finding.parts).toEqual([])
    expect(finding.severity).toBe('blocker')
  })

  it('stringifies a non-string blocker rather than rendering an object', async () => {
    stubFetch(jsonResponse(200, { blockers: [42] }))

    const [finding] = (await generate({ intent: 'x' })).findings

    expect(finding.title).toBe('42')
  })

  it('synthesizes nothing when neither findings nor blockers are present', async () => {
    stubFetch(jsonResponse(200, { status: 'feasible' }))

    expect((await generate({ intent: 'x' })).findings).toEqual([])
  })

  it('synthesizes nothing when findings is absent and blockers is not an array', async () => {
    stubFetch(jsonResponse(200, { blockers: 'one big problem' }))

    expect((await generate({ intent: 'x' })).findings).toEqual([])
  })
})

describe('generate: ApiError kind classification', () => {
  it('classifies 400 as a validation failure', async () => {
    stubFetch(jsonResponse(400, { error: 'intent must be a non-empty string' }))

    await expect(generate({ intent: '' })).rejects.toMatchObject({
      name: 'ApiError',
      kind: 'validation',
      status: 400,
      message: 'intent must be a non-empty string',
      errorId: '',
    })
  })

  it('classifies 404 as not-found', async () => {
    stubFetch(jsonResponse(404, { error: 'not found' }))

    await expect(generate({ intent: 'x' })).rejects.toMatchObject({
      kind: 'not-found',
      status: 404,
    })
  })

  it('classifies 413 as too-large, matching the client-side guard', async () => {
    stubFetch(jsonResponse(413, { error: 'request body too large' }))

    await expect(generate({ intent: 'x' })).rejects.toMatchObject({
      kind: 'too-large',
      status: 413,
    })
  })

  it('classifies a 502 that names GOOGLE_API_KEY as configuration, not an outage', async () => {
    stubFetch(jsonResponse(502, { error: 'GOOGLE_API_KEY is not set' }))

    await expect(generate({ intent: 'x' })).rejects.toMatchObject({
      kind: 'no-api-key',
      status: 502,
      message: 'GOOGLE_API_KEY is not set',
    })
  })

  it('spots GOOGLE_API_KEY anywhere in the 502 message, not only at the start', async () => {
    stubFetch(jsonResponse(502, { error: 'upstream call failed: GOOGLE_API_KEY missing in env' }))

    await expect(generate({ intent: 'x' })).rejects.toMatchObject({ kind: 'no-api-key' })
  })

  it('classifies any other 502 as an upstream failure', async () => {
    stubFetch(jsonResponse(502, { error: 'gemini returned 429' }))

    await expect(generate({ intent: 'x' })).rejects.toMatchObject({
      kind: 'upstream',
      status: 502,
    })
  })

  it('classifies 500 as internal and carries the error id for the panel footer', async () => {
    stubFetch(jsonResponse(500, { error: 'unhandled', error_id: 'a1b2c3d4' }))

    await expect(generate({ intent: 'x' })).rejects.toMatchObject({
      kind: 'internal',
      status: 500,
      errorId: 'a1b2c3d4',
    })
  })

  it('leaves the error id empty when a 500 does not supply one', async () => {
    stubFetch(jsonResponse(500, { error: 'unhandled' }))

    await expect(generate({ intent: 'x' })).rejects.toMatchObject({ kind: 'internal', errorId: '' })
  })

  it('classifies other 5xx statuses as internal too', async () => {
    stubFetch(jsonResponse(503, { error: 'service unavailable', error_id: 'z9' }))

    await expect(generate({ intent: 'x' })).rejects.toMatchObject({
      kind: 'internal',
      status: 503,
      errorId: 'z9',
    })
  })

  it('falls back to upstream for an unhandled 4xx', async () => {
    stubFetch(jsonResponse(429, { error: 'slow down' }))

    await expect(generate({ intent: 'x' })).rejects.toMatchObject({
      kind: 'upstream',
      status: 429,
    })
  })

  it('writes a message from the status when the body carries no error field', async () => {
    stubFetch(jsonResponse(400, {}))

    await expect(generate({ intent: 'x' })).rejects.toThrow('request failed with status 400')
  })

  it('still classifies a failure whose body is not JSON at all', async () => {
    stubFetch(unparseableResponse(502))

    await expect(generate({ intent: 'x' })).rejects.toMatchObject({
      kind: 'upstream',
      status: 502,
      message: 'request failed with status 502',
    })
  })

  it('reports a 200 whose body is not JSON as internal', async () => {
    stubFetch(unparseableResponse(200))

    await expect(generate({ intent: 'x' })).rejects.toMatchObject({
      kind: 'internal',
      status: 200,
      message: 'The service returned a body that is not JSON.',
    })
  })

  it('throws an ApiError instance, which App.svelte checks with instanceof', async () => {
    stubFetch(jsonResponse(400, { error: 'bad' }))

    await expect(generate({ intent: 'x' })).rejects.toBeInstanceOf(ApiError)
  })
})

describe('generate: transport failures', () => {
  it('classifies the TypeError a dead connection produces as a network failure', async () => {
    stubFetchRejecting(new TypeError('Failed to fetch'))

    await expect(generate({ intent: 'x' })).rejects.toMatchObject({
      name: 'ApiError',
      kind: 'network',
      status: 0,
      message: 'Failed to fetch',
    })
  })

  it('stringifies a thrown non-error rather than reporting "undefined"', async () => {
    stubFetchRejecting('connection reset')

    await expect(generate({ intent: 'x' })).rejects.toMatchObject({
      kind: 'network',
      message: 'connection reset',
    })
  })

  it('classifies the abort its own budget timer fires as a timeout', async () => {
    vi.useFakeTimers()
    const fetch = vi.fn(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          init.signal.addEventListener('abort', () => {
            reject(new DOMException('This operation was aborted', 'AbortError'))
          })
        }),
    )
    vi.stubGlobal('fetch', fetch)

    const pending = generate({ intent: 'x' })
    const assertion = expect(pending).rejects.toMatchObject({
      name: 'ApiError',
      kind: 'timeout',
      status: 0,
      message: 'The run passed the 300 second budget and was cancelled.',
    })

    await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS)

    await assertion
  })

  it('does not abort a request that is still inside the budget', async () => {
    vi.useFakeTimers()
    let signal
    const fetch = vi.fn((_url, init) => {
      signal = init.signal
      return new Promise(() => {})
    })
    vi.stubGlobal('fetch', fetch)

    generate({ intent: 'x' })
    await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS - 1)

    expect(signal.aborted).toBe(false)
  })

  it('clears the budget timer after a transport failure', async () => {
    vi.useFakeTimers()
    stubFetchRejecting(new TypeError('Failed to fetch'))

    await expect(generate({ intent: 'x' })).rejects.toThrow()

    expect(vi.getTimerCount()).toBe(0)
  })
})
