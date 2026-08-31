<script>
  import { readEnclosure } from '../lib/enclosure.js'

  let {
    result,
    schematicEnabled = false,
    placementEnabled = false,
    boardEnabled = false,
    onopen = null,
  } = $props()

  const findings = $derived(Array.isArray(result?.findings) ? result.findings.length : 0)
  const parts = $derived(Array.isArray(result?.parts) ? result.parts.length : 0)
  const enclosure = $derived(readEnclosure(result))
</script>

<div class="artifacts" data-testid="chat-artifacts">
  <button type="button" disabled={!schematicEnabled} onclick={() => onopen?.('schematic')} data-material="panel">
    <span class="lbl">Schematic</span>
    <strong>{schematicEnabled ? 'Validated topology' : 'Unavailable'}</strong>
    <span>Open drawing →</span>
  </button>
  <button type="button" disabled={!boardEnabled} onclick={() => onopen?.('board')} data-material="panel">
    <span class="lbl">PCB</span>
    <strong>{parts} placed parts</strong>
    <span>Open board →</span>
  </button>
  <button type="button" disabled={!placementEnabled} onclick={() => onopen?.('placement')} data-material="panel">
    <span class="lbl">Placement</span>
    <strong>{placementEnabled ? `${result.placement_repair?.steps?.length || 0} verified rounds` : 'Not requested'}</strong>
    <span>Open receipts →</span>
  </button>
  <!-- Always clickable, like Review: the tab's empty state explains a run
       that produced no case, which a greyed card would hide. -->
  <button type="button" onclick={() => onopen?.('case')} data-material="panel">
    <span class="lbl">Case</span>
    <strong>{enclosure ? 'Printable enclosure' : 'Not generated'}</strong>
    <span>Open case →</span>
  </button>
  <button type="button" onclick={() => onopen?.('review')} data-material="panel">
    <span class="lbl">Review</span>
    <strong>{findings} findings</strong>
    <span>Open details →</span>
  </button>
</div>

<style>
  .artifacts { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; margin-top: 13px; }
  button { min-width: 0; padding: 10px 11px; text-align: left; background: var(--surface); border: 1px solid var(--rule-soft); }
  button:hover:not(:disabled) { border-color: var(--rule); }
  button:disabled { opacity: .45; }
  button span, button strong { display: block; }
  strong { margin-top: 5px; color: var(--ink); font-size: var(--fs-ui); font-weight: 500; overflow: hidden; text-overflow: ellipsis; }
  button > span:last-child { margin-top: 8px; color: var(--navy); font-size: 11px; }
  @media (max-width: 720px) { .artifacts { grid-template-columns: 1fr; } }
</style>
