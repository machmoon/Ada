// The board file the service already returns, given a way out of the browser.

export const PCB_FILENAME = 'board.kicad_pcb'

/** The .kicad_pcb text on a response, or '' when it carries none. Callers gate
    the download control on this, so a run without a board offers no button. */
export function pcbText(result) {
  const text = result && typeof result.kicad_pcb === 'string' ? result.kicad_pcb : ''
  return text.trim() ? text : ''
}

/** Writes the text to a Blob and saves it. Returns whether anything was saved. */
export function downloadPcb(text, filename = PCB_FILENAME) {
  if (!text) return false
  const url = URL.createObjectURL(new Blob([text], { type: 'application/octet-stream' }))
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
  return true
}
