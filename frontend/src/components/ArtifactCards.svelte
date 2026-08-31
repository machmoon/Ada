<script>
  let {
    result,
    schematicEnabled = false,
    boardEnabled = false,
    reviewed = true,
    onopen = null,
  } = $props()

  const findings = $derived(Array.isArray(result?.findings) ? result.findings.length : 0)
  const parts = $derived(Array.isArray(result?.parts) ? result.parts.length : 0)
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
  <button type="button" onclick={() => onopen?.('review')} data-material="panel">
    <span class="lbl">Review</span>
    <!-- A skipped review is "not run", never "0 findings" — an absent check
         and a clean one are different claims. -->
    <strong>{reviewed ? `${findings} findings` : 'not run'}</strong>
    <span>Open details →</span>
  </button>
</div>

<style>
  .artifacts { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin-top: 13px; }
  button { min-width: 0; padding: 10px 11px; text-align: left; background: var(--surface); border: 1px solid var(--rule-soft); }
  button:hover:not(:disabled) { border-color: var(--rule); }
  button:disabled { opacity: .45; }
  button span, button strong { display: block; }
  strong { margin-top: 5px; color: var(--ink); font-size: var(--fs-ui); font-weight: 500; overflow: hidden; text-overflow: ellipsis; }
  button > span:last-child { margin-top: 8px; color: var(--navy); font-size: 11px; }
  @media (max-width: 720px) { .artifacts { grid-template-columns: 1fr; } }
</style>
