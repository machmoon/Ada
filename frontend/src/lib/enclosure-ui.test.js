import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

function source(relative) {
  return readFileSync(new URL(relative, import.meta.url), 'utf8')
}

// The demo-first enclosure contract: a fresh form defaults the case ON while a
// restored request keeps what it actually said, and rigor stays a second
// opt-in toggle rather than a hard-coded behavior.
describe('enclosure form contract', () => {
  it('defaults the case on for a fresh form and respects a restored explicit false', () => {
    const form = source('../components/IntentForm.svelte')

    expect(form).toContain('let enclosure = $state(initial ? seed.enclosure === true : true)')
  })

  it('offers rigorous fit checks as an off-by-default toggle shown with the case', () => {
    const form = source('../components/IntentForm.svelte')

    expect(form).toContain("let enclosureRigorous = $state(seed.enclosure_rigorous === true)")
    expect(form).toContain('data-testid="intent-form-enclosure-rigorous"')
    expect(form).toContain('rigorous fit checks (slower)')
  })

  it('promotes the download path in the case tab with the OpenSCAD hint', () => {
    const tab = source('../components/CaseTab.svelte')

    expect(tab).toContain('data-testid="case-hint"')
    expect(tab).toContain('open it in OpenSCAD')
    expect(tab).toMatch(/class="primary"[^>]*data-testid="case-download"/)
  })
})
