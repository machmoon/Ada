import { describe, expect, it } from 'vitest'
import { checks, files, hasOrder, isGo, money, quote, tally } from './order.js'

const CHECK = (id, status, extra = {}) => ({
  id,
  title: `check ${id}`,
  status,
  summary: `${id} says ${status}`,
  source: 'silkscreen.gate',
  evidence: [`measured ${id}`],
  ...extra,
})

const result = (checkList, over = {}) => ({
  order: {
    gate: { go: true, checks: checkList },
    quote: {
      house: 'OSH Park',
      service: '2 Layer Prototype',
      basis: 'published-rule',
      quantity: 5,
      boards_ordered: 6,
      area_sq_in: 0.318,
      subtotal_cents: 318,
      shipping_cents: 0,
      total_cents: 318,
      currency: 'USD',
      lead_time_days: [9, 12],
      unavailable_reason: '',
      notes: ['sold in threes'],
      quote_url: 'https://oshpark.com/',
      source_url: 'https://docs.oshpark.com/services/two-layer/',
    },
    files: [{ filename: 'a.GTL', content: '%FSLAX46Y46*%\n' }],
    ...over,
  },
})

describe('reading the order block', () => {
  it('reports no order when the response carries none', () => {
    expect(hasOrder(null)).toBe(false)
    expect(hasOrder({})).toBe(false)
    expect(hasOrder({ order: {} })).toBe(false)
    expect(hasOrder(result([]))).toBe(true)
  })

  it('survives a shape it dislikes rather than throwing at render time', () => {
    expect(checks({ order: { gate: { checks: 'nope' } } })).toEqual([])
    expect(quote({ order: {} })).toBeNull()
    expect(files({ order: { files: [{ filename: 1 }] } })).toEqual([])
  })
})

describe('the verdict', () => {
  it('derives GO from the checks, never from a stored flag', () => {
    // The stored gate.go says true; a failing check must still win.
    expect(isGo(result([CHECK('a', 'pass'), CHECK('b', 'fail')]))).toBe(false)
    expect(isGo(result([CHECK('a', 'pass'), CHECK('b', 'warn')]))).toBe(true)
  })

  it('treats a skipped check as blocking, exactly as the gate does', () => {
    expect(isGo(result([CHECK('a', 'pass'), CHECK('b', 'skipped')]))).toBe(false)
  })

  it('puts blocking checks first, so the refusal is not below six passes', () => {
    const ordered = checks(
      result([CHECK('a', 'pass'), CHECK('b', 'warn'), CHECK('c', 'fail'), CHECK('d', 'skipped')]),
    )
    expect(ordered.map((c) => c.id)).toEqual(['c', 'd', 'a', 'b'])
    expect(ordered.slice(0, 2).every((c) => c.blocking)).toBe(true)
  })

  it('tallies the checks it was given', () => {
    expect(tally(result([CHECK('a', 'pass'), CHECK('b', 'pass'), CHECK('c', 'warn')]))).toEqual({
      pass: 2,
      fail: 0,
      warn: 1,
      skipped: 0,
    })
  })
})

describe('the price', () => {
  it('reads a real quote', () => {
    const q = quote(result([]))
    expect(q.priced).toBe(true)
    expect(q.total).toBe('$3.18 USD')
    expect(q.boardsOrdered).toBe(6)
    expect(q.sourceUrl).toContain('docs.oshpark.com')
  })

  it('shows no price rather than zero when the house needs credentials', () => {
    const q = quote(
      result([], {
        quote: {
          house: 'JLCPCB',
          basis: 'unavailable',
          total_cents: null,
          subtotal_cents: null,
          shipping_cents: null,
          unavailable_reason: 'JLCPCB quotes through an API needing credentials.',
          quote_url: 'https://cart.jlcpcb.com/quote',
        },
      }),
    )
    expect(q.priced).toBe(false)
    expect(q.total).toBe('no price')
    expect(q.subtotal).toBe('')
    expect(q.shipping).toBe('')
    expect(q.reason).toContain('credentials')
  })

  it('never renders a missing amount as $0.00', () => {
    expect(money(null)).toBe('')
    expect(money(undefined)).toBe('')
    expect(money('nonsense')).toBe('')
    expect(money(0)).toBe('$0.00 USD')
  })
})

describe('the package', () => {
  it('measures each file in bytes as sent', () => {
    expect(files(result([]))).toEqual([
      { filename: 'a.GTL', content: '%FSLAX46Y46*%\n', bytes: 14 },
    ])
  })
})
