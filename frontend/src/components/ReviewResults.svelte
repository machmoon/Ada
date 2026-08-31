<script>
  import FindingCard from './FindingCard.svelte'
  import { downloadPcb, pcbText } from '../lib/download.js'
  import { formatBoard, formatCount, joinDot } from '../lib/format.js'

  let {
    result,
    request = null,
    onnew,
    selected = -1,
    schematicEnabled = false,
    boardEnabled = false,
    onselect = null,
    onshowschematic = null,
    onshowboard = null,
  } = $props()

  const pcb = $derived(pcbText(result))

  const skipped = $derived(request ? request.review === false : false)
  const findings = $derived(result.findings)
  const constraintReceipt = $derived(result.constraint_receipt || null)

  const summary = $derived(
    joinDot([
      formatBoard(result.board_mm),
      formatCount(result.parts.length, 'part'),
      result.status,
      result.repair_rounds ? formatCount(result.repair_rounds, 'repair round') : '',
    ]),
  )
</script>

<section class="results" data-testid="review-results">
  <div class="summary">
    <span class="mono" data-testid="review-results-summary">{summary}</span>
    <span class="spacer"></span>
    {#if pcb}
      <button type="button" class="download" data-testid="review-results-download" onclick={() => downloadPcb(pcb)}>
        Download .kicad_pcb
      </button>
    {/if}
  </div>

  <!-- Gated on the list, not on the status: the placer attaches a warning to
       every FEASIBLE solve, which is the ordinary outcome on a real board. -->
  {#if result.warnings.length}
    <ul class="warnings" data-testid="review-results-warnings" data-material="panel" class:fallback={result.status === 'fallback'}>
      {#each result.warnings as warning, i (i)}
        <li data-testid="review-results-warning">{warning}</li>
      {/each}
    </ul>
  {/if}

  {#if constraintReceipt}
    <section
      class="constraint-receipt"
      class:violated={constraintReceipt.overall === 'violated'}
      data-testid="constraint-receipt"
      data-material="panel"
    >
      <div class="receipt-head">
        <strong>Constraint receipt</strong>
        <span>{constraintReceipt.overall === 'violated' ? 'approved nets missing' : 'approved nets present'}</span>
      </div>
      {#each constraintReceipt.checks as check (check.net_class)}
        <div class="receipt-row">
          <span>{check.net_class}</span>
          <span>connectivity {check.net_presence}</span>
          <span>routing {check.routing.replace('_', ' ')}</span>
        </div>
      {/each}
      <p>Layer changes, vias, impedance, and pull-ups stay “not checked” until routed copper and KiCad DRC can measure them.</p>
    </section>
  {/if}

  <div class="head">
    <h1 class="title" data-testid="review-results-title">Design review</h1>
    <span class="mono count" data-testid="review-results-count">
      {skipped ? 'not run' : formatCount(findings.length, 'finding')}
    </span>
  </div>

  <p class="lead" data-testid="review-results-lead">
    {#if skipped}
      Nothing was checked against the datasheets on this run. The board below was placed from
      the netlist alone.
    {:else}
      Every connection was checked against the pin definitions in the manufacturer's datasheet,
      not just against the netlist. Each finding cites the page it came from.
    {/if}
  </p>

  <!-- double rule, drafting convention -->
  <div class="rule"></div>
  <div class="rule last"></div>

  {#if skipped}
    <div class="state" data-testid="review-results-state" data-state="skipped" data-material="panel">
      <div class="state-title" data-testid="review-results-state-title">Review was skipped</div>
      <p class="state-body" data-testid="review-results-state-body">
        This run was submitted with the review turned off, so the board was placed but nothing
        checked it against the datasheets. Run it again with the review on to get findings.
      </p>
    </div>
  {:else if findings.length === 0}
    <div class="state" data-testid="review-results-state" data-state="clean" data-material="panel">
      <div class="state-title" data-testid="review-results-state-title">Nothing to flag</div>
      <p class="state-body" data-testid="review-results-state-body">
        The reviewer found no blockers, marginal choices, or notes worth raising against the
        datasheets it read. That covers pin function and passive values in context — not signal
        integrity, EMC, thermal margins, or manufacturability at your fab. A clean review here
        is not a substitute for a human sign-off before you order boards.
      </p>
    </div>
  {:else}
    <div class="cards" data-testid="review-results-cards">
      {#each findings as finding, i (i)}
        <FindingCard
          {finding}
          {schematicEnabled}
          {boardEnabled}
          selected={selected === i}
          onselect={onselect ? () => onselect(i) : null}
          onshowschematic={onshowschematic ? () => onshowschematic(i) : null}
          onshowboard={onshowboard ? () => onshowboard(i) : null}
        />
      {/each}
    </div>
  {/if}

  <button type="button" class="again" data-testid="review-results-new-board" onclick={onnew}>Start another board</button>
</section>

<style>
  .summary {
    display: flex;
    align-items: center;
    gap: 14px;
    font-size: var(--fs-mono-sm);
    color: var(--ink-soft);
    margin-bottom: 16px;
    max-width: var(--measure-detail);
  }
  .spacer { flex-grow: 1; }

  /* Secondary, like the suggested-fix buttons: saving the board file is an
     exit, not the point of the page. */
  .download {
    font-size: 12px;
    padding: 6px 13px;
    background: transparent;
    color: var(--ink-mid);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    white-space: nowrap;
  }

  .warnings {
    margin: 0 0 18px;
    padding: 10px 14px 10px 30px;
    background: var(--sev-marginal-bg);
    border-left: var(--sev-bar-w) solid var(--sev-marginal-rule);
    font-size: var(--fs-ui);
    color: var(--sev-marginal-fg);
    line-height: 1.6;
  }

  .warnings.fallback {
    background: var(--sev-blocker-bg);
    border-left-color: var(--sev-blocker-rule);
    color: var(--sev-blocker-fg);
  }

  .constraint-receipt {
    max-width: var(--measure-detail);
    margin: 0 0 20px;
    padding: 14px 16px;
    border: 1px solid var(--rule-soft);
    border-left: var(--sev-bar-w) solid var(--green);
    background: var(--surface);
  }
  .constraint-receipt.violated { border-left-color: var(--sev-blocker-rule); }
  .receipt-head, .receipt-row { display: flex; align-items: baseline; gap: 14px; }
  .receipt-head { justify-content: space-between; margin-bottom: 9px; }
  .receipt-head span, .receipt-row { color: var(--ink-soft); font-size: var(--fs-ui); }
  .receipt-row span:first-child { color: var(--ink); margin-right: auto; }
  .constraint-receipt p { margin: 10px 0 0; color: var(--ink-faint); font-size: var(--fs-ui); line-height: 1.45; }

  .head { display: flex; align-items: baseline; gap: 14px; margin-bottom: 4px; }
  .title { font-size: var(--fs-h1); font-weight: 600; letter-spacing: -.02em; }
  .count { font-size: 12px; color: var(--ink-soft); }

  .lead {
    font-size: var(--fs-body);
    color: var(--ink-mid);
    margin-bottom: 22px;
    max-width: var(--measure-lead);
    line-height: 1.55;
  }

  .rule { border-top: 1px solid var(--rule); margin-bottom: 1px; }
  .rule.last { margin-bottom: 20px; }

  .cards { display: flex; flex-direction: column; gap: 16px; }

  .state {
    background: var(--surface);
    border: 1px solid var(--rule-soft);
    border-left: var(--sev-bar-w) solid var(--green);
    padding: 15px 18px;
    max-width: var(--measure-detail);
  }
  .state-title { font-size: var(--fs-card-title); font-weight: 600; margin-bottom: 6px; }
  .state-body { font-size: var(--fs-detail); color: var(--ink-mid); line-height: 1.6; }

  .again {
    margin-top: 24px;
    font-size: 12px;
    padding: 6px 13px;
    background: transparent;
    color: var(--ink-mid);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
  }
</style>
