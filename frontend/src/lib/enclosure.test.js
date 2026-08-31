import { describe, expect, it } from 'vitest'
import {
  MARGIN_AXES,
  SCAD_FILENAME,
  SCAD_MIME,
  formatMargin,
  formatMm,
  hasCollision,
  readEnclosure,
} from './enclosure.js'

const FULL = {
  enclosure: {
    scad: '// generated\nmodule base() {}\n',
    params: { board_x: 48.2, board_y: 30, wall: 2, clearance: 1, cavity_z: 8.55 },
    fit: { margins_mm: { x: 1, y: 1, z: 0.55 } },
    warnings: ['U1 height defaulted to 3.0 mm'],
    repair_rounds: 1,
  },
}

describe('readEnclosure', () => {
  it('reads the frozen response shape whole', () => {
    const enclosure = readEnclosure(FULL)
    expect(enclosure).toEqual({
      scad: '// generated\nmodule base() {}\n',
      params: [
        { name: 'board_x', mm: 48.2 },
        { name: 'board_y', mm: 30 },
        { name: 'wall', mm: 2 },
        { name: 'clearance', mm: 1 },
        { name: 'cavity_z', mm: 8.55 },
      ],
      margins: { x: 1, y: 1, z: 0.55 },
      warnings: ['U1 height defaulted to 3.0 mm'],
      repairRounds: 1,
    })
  })

  it('keeps the server\'s parameter order rather than sorting', () => {
    const names = readEnclosure(FULL).params.map((p) => p.name)
    expect(names).toEqual(['board_x', 'board_y', 'wall', 'clearance', 'cavity_z'])
  })

  it('returns null for the contract\'s honest degradation', () => {
    expect(readEnclosure({ enclosure: null })).toBe(null)
    expect(readEnclosure({})).toBe(null)
    expect(readEnclosure(null)).toBe(null)
    expect(readEnclosure(undefined)).toBe(null)
  })

  it('treats a malformed enclosure the same as an absent one', () => {
    expect(readEnclosure({ enclosure: 'text' })).toBe(null)
    expect(readEnclosure({ enclosure: [] })).toBe(null)
    expect(readEnclosure({ enclosure: { scad: '' } })).toBe(null)
    expect(readEnclosure({ enclosure: { scad: '   ' } })).toBe(null)
    expect(readEnclosure({ enclosure: { params: { wall: 2 } } })).toBe(null)
  })

  it('drops non-numeric parameters instead of rendering them as millimetres', () => {
    const enclosure = readEnclosure({
      enclosure: {
        scad: 'module base() {}',
        params: { wall: 2, lid: 'friction', bogus: NaN, missing: null },
      },
    })
    expect(enclosure.params).toEqual([{ name: 'wall', mm: 2 }])
  })

  it('refuses a partial fit receipt whole rather than inventing a zero axis', () => {
    const partial = readEnclosure({
      enclosure: { scad: 'module base() {}', fit: { margins_mm: { x: 1, y: 1 } } },
    })
    expect(partial.margins).toBe(null)
    const bogus = readEnclosure({
      enclosure: { scad: 'module base() {}', fit: { margins_mm: { x: 1, y: 1, z: 'ok' } } },
    })
    expect(bogus.margins).toBe(null)
    const absent = readEnclosure({ enclosure: { scad: 'module base() {}' } })
    expect(absent.margins).toBe(null)
  })

  it('keeps negative margins signed — a collision must survive parsing', () => {
    const enclosure = readEnclosure({
      enclosure: { scad: 'module base() {}', fit: { margins_mm: { x: 1, y: -0.4, z: 0 } } },
    })
    expect(enclosure.margins).toEqual({ x: 1, y: -0.4, z: 0 })
    expect(hasCollision(enclosure.margins)).toBe(true)
  })

  it('defaults warnings and repair rounds without inventing content', () => {
    const enclosure = readEnclosure({ enclosure: { scad: 'module base() {}' } })
    expect(enclosure.warnings).toEqual([])
    expect(enclosure.repairRounds).toBe(0)
    const noisy = readEnclosure({
      enclosure: {
        scad: 'module base() {}',
        warnings: ['real', '', 42, null],
        repair_rounds: 'three',
      },
    })
    expect(noisy.warnings).toEqual(['real'])
    expect(noisy.repairRounds).toBe(0)
  })
})

describe('hasCollision', () => {
  it('is false without a receipt — no receipt is not a clean receipt', () => {
    expect(hasCollision(null)).toBe(false)
  })

  it('flags any negative axis and accepts zero as touching, not colliding', () => {
    expect(hasCollision({ x: 0, y: 0.5, z: 2 })).toBe(false)
    expect(hasCollision({ x: 0.5, y: 0.5, z: -0.01 })).toBe(true)
  })
})

describe('formatting', () => {
  it('formats millimetres with a decimal point always present', () => {
    expect(formatMm(2)).toBe('2.0')
    expect(formatMm(48.2)).toBe('48.2')
    expect(formatMm(0.55)).toBe('0.55')
    expect(formatMm(1.234)).toBe('1.234')
    expect(formatMm(-0.4)).toBe('-0.4')
  })

  it('signs margins explicitly, both ways', () => {
    expect(formatMargin(1)).toBe('+1.0')
    expect(formatMargin(0)).toBe('+0.0')
    expect(formatMargin(-0.4)).toBe('-0.4')
  })
})

describe('constants', () => {
  it('names the artifact the plan freezes', () => {
    expect(SCAD_FILENAME).toBe('enclosure.scad')
    expect(SCAD_MIME).toBe('application/octet-stream')
    expect(MARGIN_AXES).toEqual(['x', 'y', 'z'])
  })
})
