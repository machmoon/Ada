<script>
  import { onMount } from 'svelte'
  import PlacementBoard from './PlacementBoard.svelte'
  import TitleBar from './TitleBar.svelte'
  import { ApiError, repairPlacement } from '../lib/api.js'
  import { downloadText } from '../lib/download.js'

  const profiles = [
    { id: 'compact-control', name: 'Compact Control', note: 'Tight functional groups and short local connections.' },
    { id: 'thermal-first', name: 'Thermal First', note: 'Separates hot power devices before chasing density.' },
  ]

  let profile = $state('compact-control')
  let policy = $state('fast')
  let result = $state(null)
  let busy = $state(false)
  let error = $state('')
  let teaching = $state(false)

  const receipts = $derived(
    result
      ? result.steps.flatMap((step) =>
          (step.receipts || [])
            .map((receipt) => ({ action: receipt.action, receipt, step })),
        )
      : [],
  )
  const accepted = $derived(receipts.filter((item) => item.receipt.accepted))
  const teachRef = $derived(accepted.length ? accepted[0].action.ref : '')

  async function run(feedback = null) {
    busy = true
    error = ''
    try {
      result = await repairPlacement({
        profile,
        policy,
        profile_id: `demo-team-${profile}`,
        ...(feedback ? { feedback } : {}),
      })
    } catch (err) {
      error = err instanceof ApiError ? err.message : String(err)
    } finally {
      busy = false
      teaching = false
    }
  }

  function choose(next) {
    profile = next
    run()
  }

  function teach() {
    if (!teachRef) return
    teaching = true
    run({ fixed_refs_add: [teachRef] })
  }

  function save() {
    if (!result) return
    downloadText(JSON.stringify(result.board, null, 2), 'repaired-placement.json', 'application/json')
  }

  onMount(() => run())
</script>

<div class="lab" data-testid="placement-lab">
  <TitleBar intent="PCB placement agent" />
  <main>
    <header class="hero">
      <div>
        <div class="eyebrow lbl">Collaborative Partner · verifier grounded</div>
        <h1>Repair the fault. Keep the team’s judgment.</h1>
        <p>One damaged motor-control board, two valid layouts. Every move is measured by real geometry before it is accepted.</p>
      </div>
      <a href="/" class="back">Build a board</a>
    </header>

    <section class="controls" aria-label="Placement controls">
      <div class="profiles">
        {#each profiles as item}
          <button class:active={profile === item.id} type="button" onclick={() => choose(item.id)} disabled={busy}>
            <strong>{item.name}</strong><span>{item.note}</span>
          </button>
        {/each}
      </div>
      <div class="policy">
        <label class="lbl" for="policy">Mode</label>
        <select id="policy" bind:value={policy} onchange={() => run()} disabled={busy}>
          <option value="fast">Verified fast policy</option>
          <option value="gemini" disabled={result && !result.available_policies?.gemini}>Gemini directly · demo</option>
        </select>
        <button class="run" type="button" onclick={() => run()} disabled={busy}>{busy ? 'Repairing…' : 'Run repair'}</button>
      </div>
    </section>

    {#if error}
      <div class="error" role="alert">{error}</div>
    {:else if result}
      <section class="boards">
        <PlacementBoard board={result.start} score={result.score.before} label="Before · intentionally damaged" />
        <PlacementBoard board={result.board} score={result.score.after} label={`After · ${result.profile.name}`} />
      </section>

      <section class="trace">
        <div class="trace-head">
          <div>
            <span class="lbl">{result.score.before.violations.length} faults repaired · {result.score.after.violations.length} remaining</span>
            <h2>Verified moves</h2>
          </div>
          <div class="actions">
            {#if teachRef}
              <button type="button" onclick={teach} disabled={busy || teaching}>Reject {teachRef} move and remember</button>
            {/if}
            <button type="button" class="primary" onclick={save}>Download placement</button>
          </div>
        </div>
        <ol>
          {#each receipts as receipt, index}
            <li>
              <span class="index">{String(index + 1).padStart(2, '0')}</span>
              <div>
                <code>{receipt.action.kind} {receipt.action.ref} {Number(receipt.action.x).toFixed(1)} {Number(receipt.action.y).toFixed(1)}</code>
                <small>{receipt.step.proposer} · H {Number(receipt.receipt.hard_before).toFixed(2)} → {Number(receipt.receipt.hard_after).toFixed(2)} mm · P {Number(receipt.receipt.soft_before).toFixed(3)} → {Number(receipt.receipt.soft_after).toFixed(3)}</small>
              </div>
              <span class:rejected={!receipt.receipt.accepted}>{receipt.receipt.accepted ? 'accepted' : 'rejected'} · {receipt.receipt.reason}</span>
            </li>
          {:else}
            <li><span>No score-improving action was accepted.</span></li>
          {/each}
        </ol>
      </section>
    {/if}
  </main>
</div>

<style>
  .lab { min-height: 100%; background: var(--paper); }
  main { max-width: 1320px; margin: 0 auto; padding: 30px 34px 56px; }
  .hero { display: flex; gap: 24px; align-items: flex-start; justify-content: space-between; padding: 10px 0 26px; border-bottom: 1px solid var(--rule); }
  .eyebrow { color: var(--oxblood); margin-bottom: 10px; }
  h1 { font-size: 31px; font-weight: 580; letter-spacing: -.035em; max-width: 760px; }
  .hero p { max-width: 720px; margin-top: 11px; color: var(--ink-mid); font-size: var(--fs-body); line-height: 1.55; }
  .back { color: var(--ink-soft); font-family: var(--font-mono); font-size: var(--fs-mono-sm); white-space: nowrap; }
  .controls { display: flex; gap: 20px; justify-content: space-between; padding: 18px 0; }
  .profiles { display: grid; grid-template-columns: repeat(2, minmax(210px, 320px)); gap: 10px; }
  .profiles button { text-align: left; border: 1px solid var(--rule); background: var(--surface); padding: 11px 13px; }
  .profiles button.active { border-color: var(--oxblood); box-shadow: inset 3px 0 var(--oxblood); }
  .profiles strong, .profiles span { display: block; }
  .profiles strong { font-size: var(--fs-ui); }
  .profiles span { color: var(--ink-soft); font-size: var(--fs-mono-sm); margin-top: 5px; }
  .policy { display: flex; align-items: center; gap: 8px; }
  select, .run, .actions button { height: 34px; border: 1px solid var(--rule); background: var(--surface); padding: 0 11px; }
  .run, .actions .primary { color: var(--accent-ink); background: var(--accent); border-color: var(--accent); }
  .boards { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  .trace { margin-top: 18px; border: 1px solid var(--rule); background: var(--surface); }
  .trace-head { display: flex; align-items: center; justify-content: space-between; gap: 20px; padding: 14px; border-bottom: 1px solid var(--rule-soft); }
  h2 { margin-top: 5px; font-size: 18px; font-weight: 550; }
  .actions { display: flex; gap: 8px; }
  ol { list-style: none; margin: 0; padding: 0; }
  .trace li { min-height: 38px; display: grid; grid-template-columns: 42px 1fr auto; align-items: center; gap: 10px; padding: 8px 14px; border-bottom: 1px solid var(--rule-soft); }
  .trace li:last-child { border-bottom: 0; }
  .trace li code { color: var(--ink); font-family: var(--font-mono); }
  .trace li small { display: block; margin-top: 4px; color: var(--ink-soft); font-family: var(--font-mono); font-size: var(--fs-mono-sm); }
  .trace li > span:last-child, .index { color: var(--ink-soft); font-family: var(--font-mono); font-size: var(--fs-mono-sm); }
  .trace li > span.rejected { color: var(--sev-blocker-fg); }
  .error { padding: 18px; border: 1px solid var(--sev-blocker-rule); color: var(--sev-blocker-fg); background: var(--sev-blocker-bg); }
  @media (max-width: 860px) {
    main { padding: 20px 16px 40px; }
    .controls, .hero { flex-direction: column; }
    .boards { grid-template-columns: 1fr; }
    .profiles { grid-template-columns: 1fr; width: 100%; }
    .trace-head { align-items: flex-start; flex-direction: column; }
  }
</style>
