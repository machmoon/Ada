<script>
  // The order pane: the verdict, the twelve checks with their evidence, the
  // board, the price, the package, and then a stop.
  //
  // Two honesty rules the mockup does not get to override. Every number here
  // comes from the response — none is computed, defaulted or invented — and the
  // "Place order" control is inert, permanently, with the reason next to it
  // rather than in a tooltip. It is drawn at all because a screen that simply
  // omits the button leaves a reader wondering whether it is missing or
  // refused; this says which.
  import { downloadText } from '../lib/download.js'
  import { checks, files, isGo, quote, tally } from '../lib/order.js'

  let { result } = $props()

  const list = $derived(checks(result))
  const counts = $derived(tally(result))
  const go = $derived(isGo(result))
  const price = $derived(quote(result))
  const package_ = $derived(files(result))
  const summary = $derived(
    result && result.order && typeof result.order.summary === 'string'
      ? result.order.summary
      : '',
  )

  let open = $state(new Set())

  function toggle(id) {
    const next = new Set(open)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    open = next
  }
</script>

<section class="order" data-testid="order-pane">
  <header class="verdict" data-testid="order-verdict" data-go={go ? 'yes' : 'no'}>
    <span class="mono badge">{go ? 'GO' : 'NO-GO'}</span>
    <span class="tally" data-testid="order-tally">
      {counts.pass} of {list.length} checks passed · {counts.fail} failed ·
      {counts.warn} warned · {counts.skipped} could not run
    </span>
  </header>

  <p class="lede">
    {#if go}
      Every check cleared. Nothing has been ordered: read the evidence below,
      then decide.
    {:else}
      This order is not ready. The blocking checks are first.
    {/if}
  </p>

  <h3 class="lbl">Pre-flight gate</h3>
  <ul class="checks">
    {#each list as check (check.id)}
      <li class="check" data-testid="order-check" data-status={check.status} data-check={check.id}>
        <button
          type="button"
          class="head"
          data-testid="order-check-toggle"
          aria-expanded={open.has(check.id)}
          onclick={() => toggle(check.id)}
        >
          <span class="mono status" data-testid="order-check-status">{check.status}</span>
          <span class="title">{check.title}</span>
          <span class="mono src">{check.source}</span>
        </button>
        <p class="summary" data-testid="order-check-summary">{check.summary}</p>
        {#if open.has(check.id)}
          <ul class="evidence" data-testid="order-check-evidence">
            {#each check.evidence as item (item)}
              <li>{item}</li>
            {/each}
          </ul>
        {/if}
      </li>
    {/each}
  </ul>

  {#if price}
    <h3 class="lbl">Price</h3>
    <div class="price" data-testid="order-quote" data-priced={price.priced ? 'yes' : 'no'}>
      <div class="who">
        <strong>{price.house}</strong> — {price.service}
      </div>
      {#if price.priced}
        <dl class="facts">
          <dt>Boards</dt>
          <dd data-testid="order-boards">{price.boardsOrdered} (from {price.quantity} requested)</dd>
          <dt>Area billed</dt>
          <dd>{price.areaSqIn.toFixed(4)} sq in</dd>
          <dt>Subtotal</dt>
          <dd>{price.subtotal}</dd>
          <dt>Shipping</dt>
          <dd>{price.shipping}</dd>
          <dt>Total</dt>
          <dd class="total" data-testid="order-total">{price.total}</dd>
          <dt>Lead time</dt>
          <dd>{price.leadTime.join('–')} days</dd>
          <dt>Basis</dt>
          <dd class="mono">{price.basis}</dd>
        </dl>
      {:else}
        <p class="noprice" data-testid="order-no-price">
          <strong>No price.</strong> {price.reason}
        </p>
      {/if}
      <ul class="notes">
        {#each price.notes as note (note)}
          <li>{note}</li>
        {/each}
      </ul>
      {#if price.quoteUrl}
        <p class="src">
          Quote it yourself: <a href={price.quoteUrl} target="_blank" rel="noreferrer noopener" data-testid="order-quote-link">{price.quoteUrl}</a>
        </p>
      {/if}
    </div>
  {/if}

  {#if package_.length}
    <h3 class="lbl">Fab package · {package_.length} files</h3>
    <ul class="files" data-testid="order-files">
      {#each package_ as file (file.filename)}
        <li>
          <button
            type="button"
            class="file"
            data-testid="order-file"
            data-filename={file.filename}
            onclick={() => downloadText(file.content, file.filename)}
          >
            <span class="mono name">{file.filename}</span>
            <span class="mono size">{file.bytes} B</span>
          </button>
        </li>
      {/each}
    </ul>
    {#if summary}
      <button
        type="button"
        class="save-summary"
        data-testid="order-save-summary"
        onclick={() => downloadText(summary, 'order-summary.txt')}
      >Save the full order summary</button>
    {/if}
  {/if}

  <h3 class="lbl">Where this stops</h3>
  <div class="boundary" data-testid="order-boundary">
    <button
      type="button"
      class="place"
      data-testid="order-place"
      disabled
      aria-disabled="true"
    >Place order</button>
    <p>
      <strong>Silkscreen prepares orders. It does not place them.</strong>
      This control is inert by design, not because the run is incomplete, and
      there is no setting that enables it.
    </p>
    <p>
      The gate proves what it measures and says so. It cannot prove the circuit
      is the circuit you meant. An agent that clears its own checks and then
      buys the board turns every remaining design error — a wrong value, a
      misread pin, a topology that validates and does not work — into money, at
      machine speed. So the last step is you: take the package above to the fab
      yourself.
    </p>
  </div>
</section>

<style>
  .order { max-width: var(--measure-detail); }

  .verdict { display: flex; align-items: baseline; gap: 12px; }
  .badge {
    font-size: var(--fs-chip);
    letter-spacing: .12em;
    padding: 3px 10px;
  }
  .verdict[data-go='yes'] .badge { color: var(--sev-note-fg); background: var(--sev-note-bg); }
  .verdict[data-go='no'] .badge { color: var(--sev-blocker-fg); background: var(--sev-blocker-bg); }
  .tally { color: var(--ink-faint); font-size: var(--fs-small); }

  .lede { margin: 10px 0 22px; color: var(--ink-soft); }

  .lbl {
    margin: 26px 0 10px;
    font-size: var(--fs-chip);
    letter-spacing: .09em;
    text-transform: uppercase;
    color: var(--ink-faint);
    font-weight: 500;
  }

  .checks { list-style: none; padding: 0; margin: 0; }
  .check { border-top: 1px solid var(--rule-soft); padding: 10px 0; }
  .check:last-child { border-bottom: 1px solid var(--rule-soft); }

  .head {
    display: flex;
    align-items: baseline;
    gap: 10px;
    width: 100%;
    padding: 0;
    background: transparent;
    border: none;
    text-align: left;
    cursor: pointer;
    color: inherit;
  }
  .status {
    font-size: var(--fs-chip);
    letter-spacing: .09em;
    text-transform: uppercase;
    padding: 2px 7px;
    white-space: nowrap;
  }
  .check[data-status='pass'] .status { color: var(--sev-note-fg); background: var(--sev-note-bg); }
  .check[data-status='warn'] .status { color: var(--sev-marginal-fg); background: var(--sev-marginal-bg); }
  .check[data-status='fail'] .status,
  .check[data-status='skipped'] .status { color: var(--sev-blocker-fg); background: var(--sev-blocker-bg); }
  .title { flex-grow: 1; }
  .src { color: var(--ink-faint); font-size: var(--fs-chip); }

  .summary { margin: 4px 0 0 66px; color: var(--ink-soft); font-size: var(--fs-small); }
  .evidence { margin: 8px 0 0 66px; padding-left: 16px; color: var(--ink-faint); font-size: var(--fs-small); }
  .evidence li { margin: 2px 0; }

  .price { border: 1px solid var(--rule-soft); padding: 14px 16px; background: var(--surface); }
  .who { margin-bottom: 10px; }
  .facts { display: grid; grid-template-columns: max-content 1fr; gap: 4px 18px; margin: 0; }
  .facts dt { color: var(--ink-faint); font-size: var(--fs-small); }
  .facts dd { margin: 0; }
  .total { font-weight: 600; }
  .noprice { margin: 0; color: var(--ink-soft); }
  .notes { margin: 12px 0 0; padding-left: 16px; color: var(--ink-faint); font-size: var(--fs-small); }
  .src a { color: inherit; }

  .files { list-style: none; padding: 0; margin: 0; }
  .files li { border-top: 1px solid var(--rule-soft); }
  .files li:last-child { border-bottom: 1px solid var(--rule-soft); }
  .file {
    display: flex;
    justify-content: space-between;
    gap: 16px;
    width: 100%;
    padding: 7px 2px;
    background: transparent;
    border: none;
    cursor: pointer;
    color: inherit;
    font-size: var(--fs-small);
  }
  .file:hover { background: var(--well); }
  .size { color: var(--ink-faint); }

  .save-summary {
    margin-top: 12px;
    padding: 7px 12px;
    background: transparent;
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    cursor: pointer;
    color: inherit;
    font-size: var(--fs-small);
  }

  .boundary { border: 1px solid var(--rule); padding: 16px; }
  .boundary p { margin: 0 0 10px; color: var(--ink-soft); font-size: var(--fs-small); }
  .boundary p:last-child { margin-bottom: 0; }
  .place {
    padding: 9px 18px;
    margin-bottom: 12px;
    background: var(--accent-off);
    color: var(--accent-off-ink);
    border: none;
    border-radius: var(--radius);
    cursor: not-allowed;
  }
</style>
