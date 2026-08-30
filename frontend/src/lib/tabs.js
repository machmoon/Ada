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

/** The tab actually shown. Board falls back to the review until a run carries
    placements, and Schematic is not built at all, so it never resolves. */
export function resolveTab(hash, { board = false } = {}) {
  const name = parseTab(hash)
  if (name === 'board' && board) return 'board'
  return 'review'
}
