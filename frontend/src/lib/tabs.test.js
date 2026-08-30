import { describe, expect, it } from 'vitest'
import { TABS, parseTab, resolveTab } from './tabs.js'

describe('parseTab', () => {
  it('reads a tab name off the hash, with or without the leading hash', () => {
    expect(parseTab('#board')).toBe('board')
    expect(parseTab('review')).toBe('review')
  })

  it('accepts any case and surrounding whitespace', () => {
    expect(parseTab('#Board')).toBe('board')
    expect(parseTab('#  review  ')).toBe('review')
  })

  it('returns nothing for a hash that names no tab', () => {
    expect(parseTab('')).toBe('')
    expect(parseTab('#')).toBe('')
    expect(parseTab('#somewhere-else')).toBe('')
    expect(parseTab(undefined)).toBe('')
    expect(parseTab(null)).toBe('')
  })

  it('knows the three tabs the status bar shows', () => {
    expect(TABS).toEqual(['schematic', 'board', 'review'])
  })
})

describe('resolveTab', () => {
  it('shows the board once a run carries placements', () => {
    expect(resolveTab('#board', { board: true })).toBe('board')
  })

  it('falls back to the review when there is no board to show', () => {
    expect(resolveTab('#board', { board: false })).toBe('review')
    expect(resolveTab('#board')).toBe('review')
  })

  it('shows the schematic once a run carries validated topology', () => {
    expect(resolveTab('#schematic', { schematic: true })).toBe('schematic')
  })

  it('falls back to review when there is no schematic to show', () => {
    expect(resolveTab('#schematic', { schematic: false, board: true })).toBe('review')
    expect(resolveTab('#schematic')).toBe('review')
  })

  it('defaults to the review for an empty or unknown hash', () => {
    expect(resolveTab('', { schematic: true, board: true })).toBe('review')
    expect(resolveTab('#nowhere', { schematic: true, board: true })).toBe('review')
  })
})
