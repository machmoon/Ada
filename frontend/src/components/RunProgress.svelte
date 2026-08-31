<script>
  import { elapsed, run } from '../lib/run.js'
  import { formatDuration } from '../lib/format.js'

  // The labels and their order are the things that happen, whether or not
  // the server is reporting them. `key` names the backend stage each row
  // watches; `validate` is the synthetic one, driven from propose's events.
  //
  // There is deliberately no `schematic` row. That stage only runs when the
  // engine is given an output path, and the service never gives it one, so a
  // row for it could never tick -- and an un-tickable row is exactly the kind
  // of claim this list exists not to make.
  const STAGES = [
    { key: 'read', label: 'read the datasheets' },
    { key: 'propose', label: 'propose a circuit' },
    { key: 'validate', label: 'validate and repair' },
    { key: 'place', label: 'place with CP-SAT' },
    { key: 'route', label: 'route the copper' },
    { key: 'review', label: 'adversarial review' },
    { key: 'enclosure', label: 'printable case' },
  ]

  const budget = $derived($run.request ? $run.request.time_limit_s : null)

  const stages = $derived($run.stages || {})
  // One frame is enough: a stream that opened is a stream that is reporting,
  // and until then the copy must not claim a trace it does not have.
  const live = $derived(($run.feed || []).length > 0)

  // '' for a stage nothing has been said about — which is also what a skipped
  // stage stays. A run without datasheets never reads any, and the box is
  // honestly empty rather than ticked.
  function stateOf(key) {
    const stage = stages[key]
    return stage && stage.state ? stage.state : ''
  }
</script>

<section class="progress" data-testid="run-progress">
  <h1 class="title" data-testid="run-progress-title">Working</h1>
  <p class="lead" data-testid="run-progress-lead" data-live={live}>
    {#if live}
      The service is reporting each stage as it happens — this is the live trace, and a stage
      the pipeline skips stays empty rather than ticking.
    {:else}
      One request, one answer — the service reports nothing until the whole pipeline finishes,
      so these stages are what is queued, not a live trace.
    {/if}
  </p>

  <ol class="stages" data-testid="run-progress-stages">
    {#each STAGES as stage (stage.key)}
      <li
        class="stage"
        data-testid="run-progress-stage"
        data-stage={stage.label}
        data-state={stateOf(stage.key)}
      ><span class="box" aria-hidden="true"></span>{stage.label}</li>
    {/each}
  </ol>

  <div class="clock mono" data-testid="run-progress-clock">
    {formatDuration($elapsed / 1000)}
    {#if budget}<span class="budget" data-testid="run-progress-budget">· {budget} s solver budget</span>{/if}
  </div>
</section>

<style>
  .title { font-size: var(--fs-h1); font-weight: 600; letter-spacing: -.02em; }
  .lead {
    font-size: var(--fs-body);
    color: var(--ink-mid);
    line-height: 1.55;
    max-width: var(--measure-lead);
    margin-top: 6px;
  }

  .stages {
    list-style: none;
    margin: 24px 0 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 11px;
  }
  .stage { display: flex; align-items: center; gap: 10px; font-size: var(--fs-ui); color: var(--ink-mid); }
  .box { width: 12px; height: 12px; border: 1px solid var(--rule); background: var(--surface); flex-shrink: 0; }

  /* A dot inside the box while a stage runs, the box filled once it is done:
     two marks an eye can tell apart without reading the label. */
  .stage[data-state='running'] { color: var(--ink); }
  .stage[data-state='running'] .box { border-color: var(--ink); }
  .stage[data-state='running'] .box::after {
    content: '';
    display: block;
    width: 4px;
    height: 4px;
    margin: 3px;
    background: var(--ink);
  }
  .stage[data-state='done'] .box { background: var(--ink); border-color: var(--ink); }

  /* A stage that gave up: the blocker colour, and a box filled with it, so a
     failure cannot be misread as either running or done. */
  .stage[data-state='failed'] { color: var(--sev-blocker-fg); }
  .stage[data-state='failed'] .box {
    background: var(--sev-blocker-fg);
    border-color: var(--sev-blocker-fg);
  }

  .clock {
    margin-top: 26px;
    padding-top: 16px;
    border-top: 1px solid var(--rule-soft);
    font-size: 18px;
    color: var(--ink);
  }
  .budget { font-size: var(--fs-mono-sm); color: var(--ink-soft); }
</style>
