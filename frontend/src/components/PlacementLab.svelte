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
  let policy = $state('deterministic')
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

  function rewardValue(value, digits) {
    const number = Number(value)
    return Number.isFinite(number) ? number.toFixed(digits) : 'not scored'
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
        <label class="lbl" for="policy">Policy</label>
        <select id="policy" bind:value={policy} onchange={() => run()} disabled={busy}>
          <option value="deterministic">Deterministic repair oracle</option>
          <option value="gemini" disabled={result && !result.available_policies?.gemini}>Gemini proposal policy</option>
          <option value="ollama" disabled={result && !result.available_policies?.ollama}>Base Gemma on private 5090</option>
          <option value="tinker" disabled={result && !result.available_policies?.tinker}>Tinker Qwen SFT checkpoint</option>
          <option value="hybrid" disabled={result && !result.available_policies?.hybrid}>Fast policy + Gemini recovery</option>
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

      <section class="evidence">
        <div class="metric">
          <span class="lbl">Hard geometry H</span>
          <strong>{Number(result.score.before.hard).toFixed(2)} → {Number(result.score.after.hard).toFixed(2)} mm</strong>
          <p>Summed penetration depth · {result.score.before.violations.length} starting faults · {result.score.after.violations.length} remaining</p>
        </div>
        <div class="metric">
          <span class="lbl">Active policy</span>
          <strong>{result.policy === 'hybrid' ? 'Fast policy + Gemini recovery' : result.policy === 'ollama' ? 'Base Gemma on private 5090' : result.policy === 'tinker' ? 'Tinker Qwen SFT checkpoint' : result.policy === 'gemini' ? 'Gemini proposal policy' : 'Deterministic repair oracle'}</strong>
          <p>{result.policy === 'ollama' ? 'Gemma 3 4B selects bounded candidates. It is not fine-tuned.' : result.available_policies?.tinker ? 'A promoted Tinker checkpoint is configured.' : result.available_policies?.ollama ? 'The private 5090 fast path is configured.' : 'No learned fast policy is configured.'}</p>
        </div>
        <div class="metric">
          <span class="lbl">Training reward</span>
          <strong>{rewardValue(result.reward.outcome, 1)} outcome · {rewardValue(result.reward.progress, 2)} progress · {rewardValue(result.reward.preference, 3)} preference</strong>
          <p>This trains a policy later. It does not decide whether an action is accepted now.</p>
        </div>
        <div class="metric">
          <span class="lbl">Company memory</span>
          <strong>{result.profile_memory === 'none' ? 'Base profile' : result.profile_memory}</strong>
          <p>{result.profile.fixed_refs.length} fixed refs · {result.profile.groups.length} functional groups</p>
        </div>
      </section>

      <section class="loop" aria-label="Derived placement loop">
        <div class="loop-head">
          <span class="lbl">Complete derived loop</span>
          <strong>Policy proposes. Verifier measures. Gate decides.</strong>
        </div>
        <ol>
          <li><b>1</b><span><strong>Intent becomes a profile</strong>Team corrections set grouping, access, compactness, thermal, keepout, and fixed-part preferences.</span></li>
          <li><b>2</b><span><strong>Fast policy selects</strong>Base Gemma now, or a promoted Tinker policy later, selects a short ordered batch from bounded PLACE candidates.</span></li>
          <li><b>3</b><span><strong>Rules measure H</strong>H is summed overlap, boundary, keepout, and clearance penetration in millimetres.</span></li>
          <li><b>4</b><span><strong>Profile measures P</strong>P is weighted grouping, edge access, compactness, and thermal cost. Lower is better.</span></li>
          <li><b>5</b><span><strong>Verifier commits a prefix</strong>It accepts moves only while (H, P) improves. In hybrid mode Gemini is called only if the fast policy stalls.</span></li>
        </ol>
        <p class="formula mono">reward = legality[H = 0] + progress[(H₀ − Hₜ) / H₀] + preference[0.1 / (1 + P), legal boards only]</p>
      </section>

      <section class="trace">
        <div class="trace-head">
          <div><span class="lbl">Every measured proposal</span><h2>The verifier’s receipt</h2></div>
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
  .evidence { display: grid; grid-template-columns: repeat(4, 1fr); border: 1px solid var(--rule); border-top: 0; background: var(--surface); }
  .metric { padding: 14px; border-right: 1px solid var(--rule-soft); }
  .metric:last-child { border-right: 0; }
  .metric strong { display: block; margin-top: 8px; font-size: 17px; font-weight: 550; }
  .metric p { margin-top: 5px; color: var(--ink-soft); font-size: var(--fs-mono-sm); }
  .trace { margin-top: 18px; border: 1px solid var(--rule); background: var(--surface); }
  .loop { margin-top: 18px; border: 1px solid var(--rule); background: var(--surface); }
  .loop-head { padding: 14px; border-bottom: 1px solid var(--rule-soft); }
  .loop-head strong { display: block; margin-top: 6px; font-size: 17px; }
  .loop ol { display: grid; grid-template-columns: repeat(5, 1fr); }
  .loop li { min-height: 118px; display: flex; align-items: flex-start; padding: 13px; border-right: 1px solid var(--rule-soft); border-bottom: 0; }
  .loop li:last-child { border-right: 0; }
  .loop li b { color: var(--oxblood); font-family: var(--font-mono); }
  .loop li span, .loop li strong { display: block; }
  .loop li span { color: var(--ink-mid); font-size: var(--fs-mono-sm); line-height: 1.45; }
  .loop li strong { color: var(--ink); margin-bottom: 5px; font-size: var(--fs-ui); }
  .formula { padding: 11px 14px; border-top: 1px solid var(--rule-soft); color: var(--ink-soft); font-size: var(--fs-mono-sm); }
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
    .boards, .evidence { grid-template-columns: 1fr; }
    .metric { border-right: 0; border-bottom: 1px solid var(--rule-soft); }
    .profiles { grid-template-columns: 1fr; width: 100%; }
    .trace-head { align-items: flex-start; flex-direction: column; }
    .loop ol { grid-template-columns: 1fr; }
    .loop li { min-height: auto; border-right: 0; border-bottom: 1px solid var(--rule-soft); }
  }
</style>
