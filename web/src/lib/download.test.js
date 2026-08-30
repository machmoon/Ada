import { describe, expect, it } from 'vitest'
import { PCB_FILENAME, downloadPcb, pcbText } from './download.js'

describe('pcbText', () => {
  it('returns the board file the response carries', () => {
    expect(pcbText({ kicad_pcb: '(kicad_pcb (version 20240108))' })).toBe(
      '(kicad_pcb (version 20240108))',
    )
  })

  it('returns nothing when the response carries no board file', () => {
    expect(pcbText({})).toBe('')
    expect(pcbText(null)).toBe('')
    expect(pcbText({ kicad_pcb: null })).toBe('')
  })

  it('returns nothing for a board file that is not a string', () => {
    expect(pcbText({ kicad_pcb: { version: 20240108 } })).toBe('')
    expect(pcbText({ kicad_pcb: 42 })).toBe('')
  })

  it('treats a whitespace-only board file as absent, so no empty file is offered', () => {
    expect(pcbText({ kicad_pcb: '   \n ' })).toBe('')
  })
})

describe('downloadPcb', () => {
  // The rest of downloadPcb is Blob and anchor work the node environment has
  // no business running; the guard is the part with a decision in it.
  it('saves nothing, and touches no browser API, when there is no text', () => {
    expect(downloadPcb('')).toBe(false)
    expect(downloadPcb(pcbText({}))).toBe(false)
  })

  it('names the file after the board', () => {
    expect(PCB_FILENAME).toBe('board.kicad_pcb')
  })
})
