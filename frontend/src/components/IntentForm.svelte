<script>
  import MicButton from './MicButton.svelte'
  import { MAX_REQUEST_BYTES, MAX_TIME_LIMIT_S, MIN_TIME_LIMIT_S, normalizeRequest, requestBytes } from '../lib/api.js'

  let { onsubmit, initial = null } = $props()

  // Seeded once, on purpose: App remounts this form whenever the phase returns
  // to idle, so the fields stay editable rather than tracking the prop.
  // svelte-ignore state_referenced_locally
  const seed = initial || {}
  const seedSheets = Object.entries(seed.datasheets || {})

  let intent = $state(seed.intent ?? '')
  let timeLimit = $state(seed.time_limit_s ?? 20)
  let review = $state(seed.review !== false)
  let ground = $state(seed.ground === true)
  let showDatasheets = $state(seedSheets.length > 0)
  let rows = $state(
    seedSheets.length ? seedSheets.map(([part, url]) => ({ part, url })) : [{ part: '', url: '' }],
  )

  let textarea = $state(null)

  // Grounding retrieves against datasheet pages, so with no datasheet it has
  // nothing to ground on and is never sent, however the box was left.
  const hasDatasheets = $derived(
    rows.some((r) => String(r.part ?? '').trim() && String(r.url ?? '').trim()),
  )

  const request = $derived({
    intent,
    datasheets: Object.fromEntries(rows.map((r) => [r.part, r.url])),
    time_limit_s: timeLimit,
    review,
    ground: ground && hasDatasheets,
  })

  // The service rejects oversize bodies with 413; blocking here keeps the user
  // from waiting on a round trip that can only fail.
  const bytes = $derived(requestBytes(normalizeRequest(request)))
  const tooLarge = $derived(bytes > MAX_REQUEST_BYTES)
  const canSubmit = $derived(intent.trim().length > 0 && !tooLarge)

  function grow() {
    if (!textarea) return
    textarea.style.height = 'auto'
    textarea.style.height = `${textarea.scrollHeight}px`
  }

  $effect(() => {
    intent
    grow()
  })

  function addRow() {
    rows = [...rows, { part: '', url: '' }]
  }

  function submit(event) {
    event.preventDefault()
    if (!canSubmit) return
    onsubmit(request)
  }
</script>

<form class="form" onsubmit={submit}>
  <label class="lbl" for="intent">What do you want on the board</label>
  <textarea
    id="intent"
    bind:this={textarea}
    bind:value={intent}
    rows="3"
    placeholder="an STM32F030 board with a 3.3 V regulator and a motor driver"
  ></textarea>

  <div class="counter" class:over={tooLarge}>
    {#if tooLarge}
      {bytes.toLocaleString()} bytes — over the {MAX_REQUEST_BYTES.toLocaleString()} byte limit
    {:else}
      {intent.length.toLocaleString()} characters
    {/if}
  </div>

  <details class="sheets" bind:open={showDatasheets}>
    <summary class="lbl">with datasheets</summary>
    <div class="sheet-rows">
      {#each rows as row, i (i)}
        <div class="sheet-row">
          <input class="mono part" bind:value={row.part} placeholder="U1" aria-label="Part reference" />
          <input class="mono url" bind:value={row.url} placeholder="https://…/STM32F030C8.pdf" aria-label="Datasheet URL" />
        </div>
      {/each}
      <button type="button" class="add" onclick={addRow}>Add another datasheet</button>
    </div>
  </details>

  <div class="controls">
    <label class="control">
      <span class="lbl">solver budget</span>
      <input
        class="mono budget"
        type="number"
        bind:value={timeLimit}
        min={MIN_TIME_LIMIT_S}
        max={MAX_TIME_LIMIT_S}
        step="1"
      />
      <span class="unit">seconds</span>
    </label>

    <label class="control checkbox">
      <input type="checkbox" bind:checked={review} />
      <span>Run the adversarial design review</span>
    </label>

    <label
      class="control checkbox"
      class:off={!hasDatasheets}
      title={hasDatasheets
        ? 'Findings are retrieved against the pages of the datasheets above'
        : 'Add a datasheet above — there is nothing to ground findings against'}
    >
      <input type="checkbox" bind:checked={ground} disabled={!hasDatasheets} />
      <span>Ground findings against datasheet pages</span>
    </label>

    <div class="spacer"></div>
    <MicButton />
    <button type="submit" class="run" disabled={!canSubmit}>
      {review ? 'Run review' : 'Place board'}
    </button>
  </div>
</form>

<style>
  .form { max-width: var(--measure-detail); }

  textarea {
    display: block;
    width: 100%;
    margin-top: 8px;
    padding: 12px 14px;
    background: var(--surface);
    border: 1px solid var(--rule-soft);
    border-radius: 0;
    font-size: var(--fs-body);
    line-height: 1.55;
    resize: none;
    overflow: hidden;
  }
  textarea:focus { border-color: var(--rule); outline: none; }

  .counter {
    margin-top: 6px;
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
    color: var(--ink-faint);
  }
  .counter.over { color: var(--sev-blocker-fg); }

  .sheets { margin-top: 18px; border-top: 1px solid var(--rule-soft); padding-top: 14px; }
  summary { cursor: pointer; }
  .sheet-rows { display: flex; flex-direction: column; gap: 8px; margin-top: 12px; }
  .sheet-row { display: flex; gap: 8px; }
  .sheet-row input {
    background: var(--surface);
    border: 1px solid var(--rule-soft);
    padding: 7px 10px;
    font-size: var(--fs-mono);
  }
  .part { width: 90px; }
  .url { flex-grow: 1; min-width: 0; }

  .add {
    align-self: flex-start;
    font-size: 12px;
    padding: 5px 11px;
    background: transparent;
    color: var(--ink-mid);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
  }

  .controls {
    display: flex;
    align-items: center;
    gap: 16px;
    margin-top: 22px;
    padding-top: 16px;
    border-top: 1px solid var(--rule-soft);
    flex-wrap: wrap;
  }
  .control { display: flex; align-items: center; gap: 8px; }
  .checkbox { font-size: var(--fs-ui); color: var(--ink-mid); cursor: pointer; }
  .checkbox.off { color: var(--ink-faint); cursor: default; }
  .unit { font-size: var(--fs-ui); color: var(--ink-soft); }

  .budget {
    width: 62px;
    background: var(--surface);
    border: 1px solid var(--rule-soft);
    padding: 6px 8px;
    font-size: var(--fs-mono);
  }

  .spacer { flex-grow: 1; }

  .run {
    height: var(--mic-slot);
    font-size: var(--fs-body);
    font-weight: 500;
    padding: 0 20px;
    background: var(--oxblood);
    color: var(--surface);
    border: none;
    border-radius: var(--radius);
  }
  .run:disabled { background: var(--rule); color: var(--surface); }
</style>
