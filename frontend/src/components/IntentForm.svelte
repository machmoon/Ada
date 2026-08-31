<script>
  import MicButton from './MicButton.svelte'
  import { MAX_REQUEST_BYTES, MAX_TIME_LIMIT_S, MIN_TIME_LIMIT_S, normalizeRequest, requestBytes } from '../lib/api.js'
  import { DATASHEET_PRESETS } from '../lib/datasheets.js'

  let {
    onsubmit,
    initial = null,
    models = [],
    initialModel = 'gemini-3.7-flash',
    initialThinkingLevel = 'auto',
    initialQuotaRpm = 'auto',
  } = $props()

  const ORCHESTRATOR_MODELS = [
    {
      id: 'gemini-3.7-flash',
      name: 'Gemini 3.7 Flash',
      note: 'Stable · strongest current agent model',
    },
    {
      id: 'gemini-3.1-pro-preview',
      name: 'Gemini 3.1 Pro',
      note: 'Preview · deeper reasoning by default',
    },
  ]

  // Seeded once, on purpose: App remounts this form whenever the phase returns
  // to idle, so the fields stay editable rather than tracking the prop.
  // svelte-ignore state_referenced_locally
  const seed = initial || {}
  const seedSheets = Object.entries(seed.datasheets || {})

  let intent = $state(seed.intent ?? '')
  let timeLimit = $state(seed.time_limit_s ?? 20)
  let noSolverBudget = $state(seed.no_solver_budget !== false)
  let review = $state(seed.review !== false)
  let ground = $state(seed.ground === true)
  // Seeded once with the rest of the editable form state.
  // svelte-ignore state_referenced_locally
  let orchestratorModel = $state(initialModel || 'gemini-3.7-flash')
  // svelte-ignore state_referenced_locally
  let thinkingLevel = $state(initialThinkingLevel || 'auto')
  // svelte-ignore state_referenced_locally
  let quotaRpm = $state(String(initialQuotaRpm || 'auto'))
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
    no_solver_budget: noSolverBudget,
    review,
    ground: ground && hasDatasheets,
  })

  // The service rejects oversize bodies with 413; blocking here keeps the user
  // from waiting on a round trip that can only fail.
  const bytes = $derived(requestBytes(normalizeRequest(request)))
  const tooLarge = $derived(bytes > MAX_REQUEST_BYTES)
  const advertisedModels = $derived(new Set(models.map((model) => String(model?.id ?? ''))))
  const selectedUnavailable = $derived(
    advertisedModels.size > 0 && !advertisedModels.has(orchestratorModel),
  )
  const canSubmit = $derived(intent.trim().length > 0 && !tooLarge && !selectedUnavailable)

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

  function useDatasheet(preset) {
    const next = rows.map((row) => ({ ...row }))
    const existing = next.findIndex((row) => String(row.part ?? '').trim() === preset.part)
    const blank = next.findIndex(
      (row) => !String(row.part ?? '').trim() && !String(row.url ?? '').trim(),
    )

    if (existing >= 0) next[existing] = { part: preset.part, url: preset.url }
    else if (blank >= 0) next[blank] = { part: preset.part, url: preset.url }
    else next.push({ part: preset.part, url: preset.url })

    rows = next
    showDatasheets = true
  }

  function submit(event) {
    event.preventDefault()
    if (!canSubmit) return
    onsubmit(request, { model: orchestratorModel, thinkingLevel, quotaRpm })
  }
</script>

<form class="form" data-testid="intent-form" onsubmit={submit}>
  <label class="lbl" for="intent">What do you want on the board</label>
  <textarea
    id="intent"
    bind:this={textarea}
    bind:value={intent}
    rows="3"
    placeholder="an STM32F030 board with a 3.3 V regulator and a motor driver"
    data-testid="intent-form-intent"
  ></textarea>

  <div class="counter" class:over={tooLarge} data-testid="intent-form-counter">
    {#if tooLarge}
      {bytes.toLocaleString()} bytes — over the {MAX_REQUEST_BYTES.toLocaleString()} byte limit
    {:else}
      {intent.length.toLocaleString()} characters
    {/if}
  </div>

  <details class="sheets" bind:open={showDatasheets} data-testid="intent-form-datasheets">
    <summary class="lbl" data-testid="intent-form-datasheets-summary">with datasheets</summary>
    <div class="presets" data-testid="intent-form-datasheet-presets">
      <div class="preset-heading lbl">available to use</div>
      {#each DATASHEET_PRESETS as preset (preset.part)}
        <div class="preset" data-testid="intent-form-datasheet-preset">
          <div class="preset-copy">
            <span class="mono preset-part">{preset.part}</span>
            <span class="preset-maker">{preset.manufacturer}</span>
          </div>
          <a class="pdf" href={preset.url} target="_blank" rel="noreferrer">View PDF</a>
          <button
            type="button"
            class="use"
            data-testid="intent-form-use-datasheet"
            disabled={rows.some(
              (row) => String(row.part ?? '').trim() === preset.part
                && String(row.url ?? '').trim() === preset.url,
            )}
            onclick={() => useDatasheet(preset)}
          >
            {rows.some(
              (row) => String(row.part ?? '').trim() === preset.part
                && String(row.url ?? '').trim() === preset.url,
            ) ? 'Selected' : 'Use datasheet'}
          </button>
        </div>
      {/each}
    </div>
    <div class="sheet-rows">
      {#each rows as row, i (i)}
        <div class="sheet-row" data-testid="intent-form-datasheet-row">
          <input class="mono part" bind:value={row.part} placeholder="U1" aria-label="Part reference" data-testid="intent-form-datasheet-part" />
          <input class="mono url" bind:value={row.url} placeholder="https://…/STM32F030C8.pdf" aria-label="Datasheet URL" data-testid="intent-form-datasheet-url" />
        </div>
      {/each}
      <button type="button" class="add" data-testid="intent-form-add-datasheet" onclick={addRow}>Add another datasheet</button>
    </div>
  </details>

  <section class="orchestrator" data-testid="intent-form-orchestrator">
    <div class="orchestrator-heading">
      <span class="lbl">orchestrator</span>
      <span class="orchestrator-note">Chooses clarification and calls the board generator</span>
    </div>
    <div class="orchestrator-controls">
      <label class="orchestrator-control">
        <span>Model</span>
        <select bind:value={orchestratorModel} data-testid="intent-form-orchestrator-model">
          {#each ORCHESTRATOR_MODELS as option (option.id)}
            <option
              value={option.id}
              disabled={advertisedModels.size > 0 && !advertisedModels.has(option.id)}
            >
              {option.name}{advertisedModels.size > 0 && !advertisedModels.has(option.id) ? ' · unavailable' : ''}
            </option>
          {/each}
        </select>
        <small>{ORCHESTRATOR_MODELS.find((option) => option.id === orchestratorModel)?.note}</small>
      </label>

      <label class="orchestrator-control">
        <span>Reasoning effort</span>
        <select bind:value={thinkingLevel} data-testid="intent-form-thinking-level">
          <option value="auto">Auto · model default</option>
          <option value="low">Fast · low</option>
          <option value="medium">Standard · medium</option>
          <option value="high">Deep · high</option>
        </select>
        <small>Gemini 3 always thinks internally; Fast uses its lowest supported effort.</small>
      </label>

      <label class="orchestrator-control">
        <span>Request pace</span>
        <select bind:value={quotaRpm} data-testid="intent-form-quota-rpm">
          <option value="auto">Auto · no app limit</option>
          <option value="15">Fast · 15 RPM</option>
          <option value="6">Demo-safe · 6 RPM</option>
          <option value="3">Conservative · 3 RPM</option>
        </select>
        <small>Spaces Gemini calls across this service instance. It cannot increase daily or token quota.</small>
      </label>
    </div>
    {#if selectedUnavailable}
      <p class="model-warning" role="status">This model is not advertised by the configured API key.</p>
    {/if}
  </section>

  <div class="controls">
    <label class="control">
      <span class="lbl">solver budget</span>
      <input
        class="mono budget"
        type="number"
        bind:value={timeLimit}
        disabled={noSolverBudget}
        min={MIN_TIME_LIMIT_S}
        max={MAX_TIME_LIMIT_S}
        step="1"
        data-testid="intent-form-budget"
      />
      <span class="unit">{noSolverBudget ? 'unlimited' : 'seconds'}</span>
      <button
        type="button"
        class="no-budget"
        class:active={noSolverBudget}
        aria-pressed={noSolverBudget}
        data-testid="intent-form-no-budget"
        title="Let CP-SAT run without a solver time limit; deployment request limits may still apply"
        onclick={() => (noSolverBudget = !noSolverBudget)}
      >NO budget</button>
    </label>

    <label class="control checkbox">
      <input type="checkbox" bind:checked={review} data-testid="intent-form-review" />
      <span>Run the adversarial design review</span>
    </label>

    <label
      class="control checkbox"
      class:off={!hasDatasheets}
      title={hasDatasheets
        ? 'Findings are retrieved against the pages of the datasheets above'
        : 'Add a datasheet above — there is nothing to ground findings against'}
    >
      <input type="checkbox" bind:checked={ground} disabled={!hasDatasheets} data-testid="intent-form-ground" />
      <span>Ground findings against datasheet pages</span>
    </label>

    <div class="spacer"></div>
    <MicButton />
    <button type="submit" class="run" data-testid="intent-form-submit" disabled={!canSubmit}>
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
  .presets { margin-top: 12px; }
  .preset-heading { margin-bottom: 6px; }
  .preset {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 9px 10px;
    background: var(--surface);
    border: 1px solid var(--rule-soft);
  }
  .preset-copy { display: flex; flex-direction: column; min-width: 0; }
  .preset-part { color: var(--ink); }
  .preset-maker { font-size: 12px; color: var(--ink-soft); }
  .pdf { margin-left: auto; font-size: 12px; color: var(--ink-mid); white-space: nowrap; }
  .use {
    padding: 5px 10px;
    background: transparent;
    color: var(--ink-mid);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    font-size: 12px;
    white-space: nowrap;
  }
  .use:disabled { color: var(--ink-faint); border-color: var(--rule-soft); }
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

  .orchestrator {
    margin-top: 18px;
    padding: 14px;
    border: 1px solid var(--rule);
    background: var(--well);
  }
  .orchestrator-heading { display: flex; align-items: baseline; gap: 10px; }
  .orchestrator-note { color: var(--ink-soft); font-size: var(--fs-ui); }
  .orchestrator-controls { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-top: 12px; }
  .orchestrator-control { display: flex; flex-direction: column; gap: 6px; color: var(--ink-mid); font-size: var(--fs-ui); }
  .orchestrator-control select {
    width: 100%;
    padding: 8px 10px;
    border: 1px solid var(--rule-soft);
    background: var(--surface);
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: var(--fs-mono);
  }
  .orchestrator-control small { color: var(--ink-faint); line-height: 1.4; }
  .model-warning { margin-top: 10px; color: var(--sev-marginal-fg); font-size: var(--fs-ui); }

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
  .budget:disabled { color: var(--ink-faint); }

  .no-budget {
    padding: 6px 9px;
    background: transparent;
    border: 1px solid var(--rule);
    color: var(--ink-mid);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
  }
  .no-budget.active { background: var(--ink); color: var(--paper); }

  .spacer { flex-grow: 1; }

  .run {
    height: var(--mic-slot);
    font-size: var(--fs-body);
    font-weight: 500;
    padding: 0 20px;
    background: var(--accent);
    color: var(--accent-ink);
    border: none;
    border-radius: var(--radius);
  }
  .run:disabled { background: var(--accent-off); color: var(--accent-off-ink); }
  @media (max-width: 680px) { .orchestrator-controls { grid-template-columns: 1fr; } }
</style>
