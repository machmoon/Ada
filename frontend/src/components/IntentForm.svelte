<script>
  import MicButton from './MicButton.svelte'
  import { MAX_REQUEST_BYTES, MAX_TIME_LIMIT_S, MIN_TIME_LIMIT_S, normalizeRequest, requestBytes } from '../lib/api.js'
  import { constraintKinds, constraintManifestReady, suggestConstraintManifest } from '../lib/constraints.js'
  import { DATASHEET_PRESETS } from '../lib/datasheets.js'

  let {
    onsubmit,
    initial = null,
    models = [],
    initialModel = 'gemini-3.7-flash',
    initialThinkingLevel = 'auto',
    initialQuotaRpm = 'auto',
  } = $props()

  const GEMINI_OPTION = {
    id: 'gemini-3.7-flash',
    name: 'Gemini 3.7 Flash',
    note: 'Primary hackathon path',
  }

  // Seeded once, on purpose: App remounts this form whenever the phase returns
  // to idle, so the fields stay editable rather than tracking the prop.
  // svelte-ignore state_referenced_locally
  const seed = initial || {}
  const seedSheets = Object.entries(seed.datasheets || {})

  let intent = $state(seed.intent ?? '')
  let constraints = $state(seed.constraints || suggestConstraintManifest(seed.intent ?? ''))
  let constraintKey = $state(constraintKinds(seed.intent ?? '').join(','))
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
    constraints,
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
  const configuredFallback = $derived(models.find((model) => !String(model?.id ?? '').startsWith('gemini-')))
  const modelOptions = $derived([
    GEMINI_OPTION,
    ...(configuredFallback
      ? [{
          id: String(configuredFallback.id),
          name: String(configuredFallback.name || configuredFallback.id),
          note: String(configuredFallback.description || 'Configured text-only fallback'),
        }]
      : []),
  ])
  const usesGemini = $derived(orchestratorModel.startsWith('gemini-'))
  const constraintsReady = $derived(constraintManifestReady(constraints))
  const selectedUnavailable = $derived(
    advertisedModels.size > 0 && !advertisedModels.has(orchestratorModel),
  )
  const canSubmit = $derived(
    intent.trim().length > 0
      && constraints.approved === true
      && constraintsReady
      && !tooLarge
      && !selectedUnavailable,
  )

  $effect(() => {
    const nextKey = constraintKinds(intent).join(',')
    if (nextKey !== constraintKey) {
      constraints = suggestConstraintManifest(intent)
      constraintKey = nextKey
    }
  })

  $effect(() => {
    if (advertisedModels.size > 0 && !advertisedModels.has(orchestratorModel)) {
      orchestratorModel = models[0].id
    }
  })

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

  function addKeepout() {
    constraints.mechanical.keepouts = [
      ...constraints.mechanical.keepouts,
      { name: `Keepout ${constraints.mechanical.keepouts.length + 1}`, x_mm: 0, y_mm: 0, width_mm: 5, height_mm: 5 },
    ]
  }

  function addFixedPlacement() {
    constraints.mechanical.fixed_placements = [
      ...constraints.mechanical.fixed_placements,
      { ref: '', x_mm: 0, y_mm: 0, tolerance_mm: 0.25 },
    ]
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
    data-material="panel"
  ></textarea>

  <div class="counter" class:over={tooLarge} data-testid="intent-form-counter">
    {#if tooLarge}
      {bytes.toLocaleString()} bytes — over the {MAX_REQUEST_BYTES.toLocaleString()} byte limit
    {:else}
      {intent.length.toLocaleString()} characters
    {/if}
  </div>

  <section class="constraints" data-testid="intent-form-constraints" data-material="panel">
    <div class="constraint-heading">
      <div>
        <span class="lbl">nets and routing contract</span>
        <p>Confirm this before build. The manifest travels with the board, and every stage reports what it did or did not verify.</p>
      </div>
      <label class="layers">
        <span>Board layers</span>
        <input type="number" min="1" max="32" bind:value={constraints.board_layers} />
      </label>
    </div>

    <div class="constraint-legend">
      <span><strong>Hard</strong> rejects promotion when violated or unresolved.</span>
      <span><strong>Soft</strong> ranks only layouts that already pass every hard rule.</span>
    </div>

    {#each constraints.net_classes as netClass (netClass.name)}
      <article class="net-class">
        <div class="net-title">
          <strong>{netClass.name}</strong><em>Hard</em>
          <span>{netClass.concerns.join(' · ')}</span>
        </div>
        <label class="wide">
          <span>Exact net names</span>
          <input
            value={netClass.nets.join(', ')}
            oninput={(event) => (netClass.nets = event.currentTarget.value.split(',').map((value) => value.trim()).filter(Boolean))}
          />
        </label>
        <label>
          <span>Allowed copper</span>
          <input
            value={netClass.allowed_layers.join(', ')}
            oninput={(event) => (netClass.allowed_layers = event.currentTarget.value.split(',').map((value) => value.trim()).filter(Boolean))}
          />
        </label>
        <label>
          <span>Max layer shifts</span>
          <input type="number" min="0" max="32" bind:value={netClass.max_layer_transitions} />
        </label>
        <label>
          <span>Max vias per net</span>
          <input type="number" min="0" max="64" bind:value={netClass.max_vias_per_net} />
        </label>
        {#if netClass.min_trace_width_mm !== null}
          <label>
            <span>Minimum width (mm)</span>
            <input type="number" min="0.001" step="any" bind:value={netClass.min_trace_width_mm} />
          </label>
        {/if}
        {#if netClass.max_length_mm !== null}
          <label>
            <span>Maximum length (mm)</span>
            <input type="number" min="0.001" step="any" bind:value={netClass.max_length_mm} />
          </label>
        {/if}
        {#if netClass.max_skew_mm !== null}
          <label>
            <span>Maximum skew (mm)</span>
            <input type="number" min="0" step="any" bind:value={netClass.max_skew_mm} />
          </label>
        {/if}
        {#if netClass.max_stub_length_mm !== null}
          <label>
            <span>Maximum stub (mm)</span>
            <input type="number" min="0" step="any" bind:value={netClass.max_stub_length_mm} />
          </label>
        {/if}
        {#if netClass.kind === 'spi' || netClass.kind === 'clock'}
          <label>
            <span>Maximum frequency (Hz)</span>
            <input type="number" min="1" step="any" bind:value={netClass.max_frequency_hz} />
          </label>
        {/if}
        {#if netClass.pullups_required}
          <label>
            <span>Signal voltage (V)</span>
            <input type="number" min="0.01" step="any" bind:value={netClass.signal_voltage_v} />
          </label>
          <label>
            <span>Bus speed (Hz)</span>
            <input type="number" min="1" step="any" bind:value={netClass.max_frequency_hz} />
          </label>
          <label>
            <span>Pull-up rail net</span>
            <input bind:value={netClass.pullup_rail} />
          </label>
          <label>
            <span>Pull-up range (Ω)</span>
            <div class="pair-inputs">
              <input type="number" min="0.1" step="any" bind:value={netClass.pullup_min_ohms} aria-label="Minimum pull-up resistance" />
              <input type="number" min="0.1" step="any" bind:value={netClass.pullup_max_ohms} aria-label="Maximum pull-up resistance" />
            </div>
          </label>
          <label>
            <span>Bus capacitance (pF)</span>
            <input type="number" min="0.01" step="any" bind:value={netClass.bus_capacitance_pf} />
          </label>
          <label>
            <span>Rise-time limit (ns)</span>
            <input type="number" min="0.01" step="any" bind:value={netClass.max_rise_time_ns} />
          </label>
        {/if}
        {#if netClass.controlled_impedance}
          <label>
            <span>Target impedance (Ω)</span>
            <input type="number" min="1" step="any" bind:value={netClass.impedance_ohms} />
          </label>
          <label>
            <span>Impedance tolerance (%)</span>
            <input type="number" min="0.01" max="100" step="any" bind:value={netClass.impedance_tolerance_percent} />
          </label>
          <label>
            <span>Pair spacing (mm)</span>
            <input type="number" min="0.001" step="any" bind:value={netClass.pair_spacing_mm} />
          </label>
        {/if}
        {#if netClass.reference_plane}
          <label>
            <span>Reference plane</span>
            <input bind:value={netClass.reference_plane} />
          </label>
        {/if}
        {#if netClass.kind === 'power'}
          <label>
            <span>Expected current (A)</span>
            <input type="number" min="0" step="any" bind:value={netClass.expected_current_a} />
          </label>
          <label>
            <span>Copper weight (oz)</span>
            <input type="number" min="0.01" step="any" bind:value={netClass.copper_weight_oz} />
          </label>
          <label>
            <span>Maximum voltage drop (V)</span>
            <input type="number" min="0" step="any" bind:value={netClass.max_voltage_drop_v} />
          </label>
          <label>
            <span>Thermal separation (mm)</span>
            <input type="number" min="0" step="any" bind:value={netClass.min_thermal_separation_mm} />
          </label>
        {/if}
        {#if netClass.kind === 'analog' || netClass.kind === 'rf'}
          <label>
            <span>Sensitive-net separation (mm)</span>
            <input type="number" min="0" step="any" bind:value={netClass.min_separation_mm} />
          </label>
          <label class="inline-check">
            <input type="checkbox" bind:checked={netClass.guard_required} />
            <span>Guard region required</span>
          </label>
        {/if}
      </article>
    {/each}

    <details class="constraint-detail">
      <summary>Mechanical hard constraints</summary>
      <div class="mechanical-grid">
        <label><span>Maximum board width (mm)</span><input type="number" min="0.01" step="any" bind:value={constraints.mechanical.max_board_width_mm} /></label>
        <label><span>Maximum board height (mm)</span><input type="number" min="0.01" step="any" bind:value={constraints.mechanical.max_board_height_mm} /></label>
        <label><span>Maximum component height (mm)</span><input type="number" min="0.01" step="any" bind:value={constraints.mechanical.max_component_height_mm} /></label>
        <label><span>Mounting-hole refs</span><input value={constraints.mechanical.mounting_hole_refs.join(', ')} oninput={(event) => (constraints.mechanical.mounting_hole_refs = event.currentTarget.value.split(',').map((value) => value.trim()).filter(Boolean))} /></label>
      </div>
      {#each constraints.mechanical.keepouts as keepout (keepout.name)}
        <div class="mechanical-row">
          <input bind:value={keepout.name} aria-label="Keepout name" />
          <input type="number" step="any" bind:value={keepout.x_mm} aria-label="Keepout X" />
          <input type="number" step="any" bind:value={keepout.y_mm} aria-label="Keepout Y" />
          <input type="number" min="0" step="any" bind:value={keepout.width_mm} aria-label="Keepout width" />
          <input type="number" min="0" step="any" bind:value={keepout.height_mm} aria-label="Keepout height" />
        </div>
      {/each}
      {#each constraints.mechanical.fixed_placements as fixed, i (i)}
        <div class="mechanical-row">
          <input bind:value={fixed.ref} placeholder="U1" aria-label="Fixed reference" />
          <input type="number" step="any" bind:value={fixed.x_mm} aria-label="Fixed X" />
          <input type="number" step="any" bind:value={fixed.y_mm} aria-label="Fixed Y" />
          <input type="number" min="0" step="any" bind:value={fixed.tolerance_mm} aria-label="Placement tolerance" />
        </div>
      {/each}
      <div class="detail-actions">
        <button type="button" onclick={addKeepout}>Add keepout</button>
        <button type="button" onclick={addFixedPlacement}>Add fixed part</button>
      </div>
    </details>

    <details class="constraint-detail">
      <summary>Soft placement preferences</summary>
      <p>Weights rank valid alternatives. They never make a hard violation acceptable.</p>
      <div class="soft-grid">
        {#each Object.entries(constraints.soft_preferences) as [name, value] (name)}
          <label><span>{name.replaceAll('_', ' ')}</span><input type="number" min="0" step="any" value={value} oninput={(event) => (constraints.soft_preferences[name] = Number(event.currentTarget.value))} /></label>
        {/each}
      </div>
    </details>

    <label class="approval">
      <input type="checkbox" bind:checked={constraints.approved} data-testid="intent-form-constraints-approved" />
      <span>I approve these net names and limits. Unknown high-risk values must stay unresolved, not guessed.</span>
    </label>
    {#if !constraintsReady}
      <p class="constraint-warning">Enter the exact critical net names before approving the build.</p>
    {/if}
  </section>

  <details class="sheets" bind:open={showDatasheets} data-testid="intent-form-datasheets">
    <summary class="lbl" data-testid="intent-form-datasheets-summary">with datasheets</summary>
    <div class="presets" data-testid="intent-form-datasheet-presets">
      <div class="preset-heading lbl">available to use</div>
      {#each DATASHEET_PRESETS as preset (preset.part)}
        <div class="preset" data-testid="intent-form-datasheet-preset" data-material="panel">
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
          <input class="mono part" bind:value={row.part} placeholder="U1" aria-label="Part reference" data-testid="intent-form-datasheet-part" data-material="panel" />
          <input class="mono url" bind:value={row.url} placeholder="https://…/STM32F030C8.pdf" aria-label="Datasheet URL" data-testid="intent-form-datasheet-url" data-material="panel" />
        </div>
      {/each}
      <button type="button" class="add" data-testid="intent-form-add-datasheet" onclick={addRow}>Add another datasheet</button>
    </div>
  </details>

  <section class="orchestrator" data-testid="intent-form-orchestrator" data-material="panel">
    <div class="orchestrator-heading">
      <span class="lbl">orchestrator</span>
      <span class="orchestrator-note">Chooses clarification and calls the board generator</span>
    </div>
    <div class="orchestrator-controls">
      <label class="orchestrator-control">
        <span>Model</span>
        <select bind:value={orchestratorModel} data-testid="intent-form-orchestrator-model" data-material="tint">
          {#each modelOptions as option (option.id)}
            <option
              value={option.id}
              disabled={advertisedModels.size > 0 && !advertisedModels.has(option.id)}
            >
              {option.name}{advertisedModels.size > 0 && !advertisedModels.has(option.id) ? ' · unavailable' : ''}
            </option>
          {/each}
        </select>
        <small>{modelOptions.find((option) => option.id === orchestratorModel)?.note}</small>
      </label>

      <label class="orchestrator-control">
        <span>Reasoning effort</span>
        <select bind:value={thinkingLevel} data-testid="intent-form-thinking-level" data-material="tint">
          <option value="auto">Auto · model default</option>
          <option value="low">Fast · low</option>
          <option value="medium">Standard · medium</option>
          <option value="high">Deep · high</option>
        </select>
        <small>{usesGemini ? 'Gemini 3 always thinks internally; Fast uses its lowest supported effort.' : 'The fallback uses its provider default.'}</small>
      </label>

      <label class="orchestrator-control">
        <span>Request pace</span>
        <select bind:value={quotaRpm} data-testid="intent-form-quota-rpm" data-material="tint">
          <option value="auto">Auto · no app limit</option>
          <option value="15">Fast · 15 RPM</option>
          <option value="6">Demo-safe · 6 RPM</option>
          <option value="3">Conservative · 3 RPM</option>
        </select>
        <small>Spaces model calls across this service instance. It cannot increase provider quota.</small>
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
        data-material="panel"
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

  .constraints {
    margin-top: 18px;
    padding: 14px;
    border: 1px solid var(--rule);
    background: var(--well);
  }
  .constraint-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
  .constraint-heading p { margin: 5px 0 0; color: var(--ink-soft); font-size: var(--fs-ui); line-height: 1.45; }
  .constraint-legend { display: flex; gap: 18px; margin-top: 12px; color: var(--ink-soft); font-size: var(--fs-ui); }
  .layers { width: 92px; flex: 0 0 auto; }
  .net-class {
    display: grid;
    grid-template-columns: 1.4fr repeat(3, minmax(96px, .7fr));
    gap: 10px;
    margin-top: 12px;
    padding-top: 12px;
    border-top: 1px solid var(--rule-soft);
  }
  .net-title { grid-column: 1 / -1; display: flex; align-items: baseline; gap: 10px; }
  .net-title span { color: var(--ink-faint); font-size: var(--fs-ui); }
  .net-title em { padding: 2px 6px; border: 1px solid var(--rule); color: var(--ink-mid); font-size: 10px; font-style: normal; text-transform: uppercase; }
  .constraints label { display: flex; flex-direction: column; gap: 5px; color: var(--ink-mid); font-size: var(--fs-ui); }
  .constraints input[type='number'], .constraints label > input:not([type]) {
    width: 100%;
    min-width: 0;
    padding: 7px 9px;
    border: 1px solid var(--rule-soft);
    background: var(--surface);
    color: var(--ink);
    font-family: var(--font-mono);
  }
  .approval { flex-direction: row !important; align-items: flex-start; margin-top: 14px; color: var(--ink) !important; }
  .inline-check { flex-direction: row !important; align-items: center; }
  .pair-inputs { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
  .constraint-detail { margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--rule-soft); }
  .constraint-detail summary { color: var(--ink); font-weight: 600; }
  .constraint-detail p { color: var(--ink-soft); font-size: var(--fs-ui); }
  .mechanical-grid, .soft-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-top: 10px; }
  .mechanical-row { display: grid; grid-template-columns: 1.3fr repeat(4, .7fr); gap: 8px; margin-top: 8px; }
  .detail-actions { display: flex; gap: 8px; margin-top: 10px; }
  .detail-actions button { padding: 6px 10px; border: 1px solid var(--rule); background: transparent; color: var(--ink-mid); }
  .constraint-warning { margin: 8px 0 0; color: var(--sev-marginal-fg); font-size: var(--fs-ui); }

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
  @media (max-width: 680px) {
    .constraint-heading { flex-direction: column; }
    .constraint-legend { flex-direction: column; gap: 6px; }
    .net-class { grid-template-columns: 1fr; }
    .net-title { grid-column: 1; flex-direction: column; gap: 4px; }
    .mechanical-grid, .soft-grid, .mechanical-row { grid-template-columns: 1fr; }
    .orchestrator-controls { grid-template-columns: 1fr; }
  }
</style>
