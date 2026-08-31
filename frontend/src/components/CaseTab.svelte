<script>
  import { downloadText } from '../lib/download.js'
  import {
    MARGIN_AXES,
    SCAD_FILENAME,
    SCAD_MIME,
    formatMargin,
    formatMm,
    hasCollision,
  } from '../lib/enclosure.js'
  import { formatCount } from '../lib/format.js'
  import { logEvent } from '../lib/log.js'

  // `enclosure` is readEnclosure()'s output or null; `stage` is the run store's
  // enclosure stage row (so a failure can say why); `requested` says whether
  // the run asked for a case at all. Null enclosure renders an explicit state,
  // never a blank pane — the contract's degradation is honest and so is this.
  let { enclosure = null, stage = null, requested = false } = $props()

  const COPY_FLASH_MS = 1200
  let copyLabel = $state('Copy')
  let copyTimer = 0

  const collides = $derived(hasCollision(enclosure?.margins))
  const failed = $derived(!enclosure && stage?.state === 'failed')

  async function copy() {
    if (!enclosure) return
    try {
      await navigator.clipboard.writeText(enclosure.scad)
      copyLabel = 'Copied'
    } catch {
      copyLabel = 'Copy failed'
    }
    clearTimeout(copyTimer)
    copyTimer = setTimeout(() => (copyLabel = 'Copy'), COPY_FLASH_MS)
    logEvent('ui.case-copy', 'copied the enclosure .scad source', {
      chars: enclosure.scad.length,
    })
  }

  function save() {
    if (!enclosure) return
    downloadText(enclosure.scad, SCAD_FILENAME, SCAD_MIME)
  }
</script>

<section class="case-tab" data-testid="case-tab">
  {#if enclosure}
    <header>
      <div>
        <span class="lbl">3d-printable enclosure</span>
        <h1>Case</h1>
        <p>
          OpenSCAD source derived from the placed board's measured geometry.
          {#if enclosure.repairRounds > 0}
            Accepted after {formatCount(enclosure.repairRounds, 'repair round')}.
          {:else}
            Accepted on the first proposal.
          {/if}
        </p>
      </div>
      <div class="actions">
        <button type="button" onclick={copy} data-testid="case-copy">{copyLabel}</button>
        <button type="button" onclick={save} data-testid="case-download">Download {SCAD_FILENAME}</button>
      </div>
    </header>

    {#if enclosure.margins}
      <div class="receipt" data-material="panel" data-testid="case-fit" data-clean={!collides}>
        <span class="lbl">verified fit · signed margins</span>
        {#each MARGIN_AXES as axis (axis)}
          <span
            class="margin mono"
            class:collision={enclosure.margins[axis] < 0}
            data-testid="case-margin"
            data-axis={axis}
          >{axis} <b>{formatMargin(enclosure.margins[axis])} mm</b></span>
        {/each}
        {#if collides}
          <span class="verdict bad" data-testid="case-fit-verdict">negative margin — the cavity collides with the board</span>
        {:else}
          <span class="verdict" data-testid="case-fit-verdict">board clears the cavity on every axis</span>
        {/if}
      </div>
    {:else}
      <!-- A missing receipt is said out loud: an absent check must never read
           as a passed one. -->
      <div class="receipt" data-material="panel" data-testid="case-fit" data-clean="false">
        <span class="lbl">verified fit</span>
        <span class="verdict" data-testid="case-fit-verdict">no fit receipt arrived with this case — fit is unverified</span>
      </div>
    {/if}

    {#if enclosure.warnings.length}
      <ul class="warnings" data-testid="case-warnings">
        {#each enclosure.warnings as warning, index (index)}
          <li data-testid="case-warning">{warning}</li>
        {/each}
      </ul>
    {/if}

    {#if enclosure.params.length}
      <section class="params" data-material="panel">
        <div class="heading">
          <span class="lbl">emitted parameters</span>
          <span class="mono">{formatCount(enclosure.params.length, 'value')}</span>
        </div>
        <table data-testid="case-params">
          <tbody>
            {#each enclosure.params as param (param.name)}
              <tr data-testid="case-param" data-name={param.name}>
                <td><code>{param.name}</code></td>
                <td class="mono value">{formatMm(param.mm)} mm</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </section>
    {/if}

    <section class="source" data-material="panel">
      <div class="heading">
        <span class="lbl">enclosure.scad</span>
        <span class="mono">{enclosure.scad.length} chars</span>
      </div>
      <pre data-testid="case-scad"><code>{enclosure.scad}</code></pre>
    </section>
  {:else}
    <!-- The honest empty states: failed is not skipped, skipped is not failed. -->
    <div class="empty" data-material="panel" data-testid="case-empty" data-state={failed ? 'failed' : 'absent'}>
      {#if failed}
        <span class="lbl">case generation failed</span>
        <p>
          The enclosure stage gave up{stage.error ? `: ${stage.error}` : ''}.
          The board itself was still generated and delivered — a case failure
          never fails the run.
        </p>
      {:else if requested}
        <span class="lbl">no case arrived</span>
        <p>A case was requested, but this run's response carried none.</p>
      {:else}
        <span class="lbl">no case was generated</span>
        <p>This run did not ask for an enclosure. Case generation is opt-in per run.</p>
      {/if}
    </div>
  {/if}
</section>

<style>
  .case-tab { max-width: 1180px; margin: 0 auto; }
  header { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; margin-bottom: 14px; }
  h1 { margin-top: 5px; font-size: var(--fs-h1); font-weight: 560; }
  header p { margin-top: 7px; max-width: 70ch; color: var(--ink-mid); line-height: 1.5; }
  .actions { display: flex; gap: 8px; }
  .actions button { min-height: 38px; padding: 0 12px; border: 1px solid var(--rule); background: var(--surface); color: var(--ink-mid); white-space: nowrap; }
  .actions button:hover { color: var(--ink); }

  .receipt { display: flex; align-items: center; gap: 18px; padding: 10px 13px; border: 1px solid var(--rule-soft); background: var(--surface); color: var(--ink-mid); font-size: var(--fs-ui); flex-wrap: wrap; }
  .margin b { color: var(--green); font-weight: 600; }
  .margin.collision b { color: var(--sev-blocker-fg); }
  .verdict { margin-left: auto; color: var(--ink-soft); font-size: var(--fs-mono-sm); }
  .verdict.bad { color: var(--sev-blocker-fg); }

  .warnings { list-style: none; margin: 14px 0 0; padding: 0; }
  .warnings li { padding: 8px 13px; border: 1px solid var(--rule-soft); border-bottom: 0; background: var(--surface); color: var(--ink-mid); font-size: var(--fs-ui); }
  .warnings li::before { content: '⚠ '; color: var(--sev-marginal-fg, var(--ink-soft)); }
  .warnings li:last-child { border-bottom: 1px solid var(--rule-soft); }

  .params, .source { margin-top: 14px; border: 1px solid var(--rule-soft); background: var(--surface); }
  .heading { display: flex; justify-content: space-between; padding: 11px 13px; border-bottom: 1px solid var(--rule-soft); }
  .heading .mono { color: var(--ink-soft); font-size: var(--fs-mono-sm); }

  table { width: 100%; border-collapse: collapse; }
  td { padding: 8px 13px; border-bottom: 1px solid var(--rule-soft); }
  tr:last-child td { border-bottom: 0; }
  code { color: var(--ink); font-family: var(--font-mono); }
  .value { text-align: right; color: var(--ink); }

  pre { margin: 0; padding: 12px 13px; overflow-x: auto; max-height: 60vh; overflow-y: auto; font-family: var(--font-mono); font-size: var(--fs-mono-sm); line-height: 1.55; color: var(--ink); }

  .empty { padding: 22px 24px; border: 1px solid var(--rule-soft); background: var(--surface); }
  .empty p { margin-top: 8px; color: var(--ink-mid); line-height: 1.5; max-width: 70ch; }

  @media (max-width: 760px) {
    header { flex-direction: column; }
    .receipt { align-items: flex-start; flex-direction: column; gap: 7px; }
    .verdict { margin-left: 0; }
  }
</style>
