// Which pane the centre column shows. The name lives in the hash fragment so a
// tab switch never reaches the server, and App is the only thing that reads it.

export const TABS = ['schematic', 'board', 'review']

/** The tab a hash names, or '' when it names none of them. */
export function parseTab(hash) {
  const name = String(hash ?? '')
    .replace(/^#/, '')
    .trim()
    .toLowerCase()
  return TABS.includes(name) ? name : ''
}

/** The tab actually shown. Drawing tabs fall back to review until the current
    run carries a contract each renderer accepts. */
export function resolveTab(hash, { schematic = false, board = false } = {}) {
  const name = parseTab(hash)
  if (name === 'schematic' && schematic) return 'schematic'
  if (name === 'board' && board) return 'board'
  return 'review'
}
