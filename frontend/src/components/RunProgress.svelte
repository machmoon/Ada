<script>
  import { elapsed, run } from '../lib/run.js'
  import { formatDuration } from '../lib/format.js'

  // The API answers once, at the end — there is no streaming and no way to know
  // which stage is running. The list is what will happen, deliberately un-ticked.
  const STAGES = [
    'read the datasheets',
    'propose a circuit',
    'validate and repair',
    'place with CP-SAT',
    'adversarial review',
  ]

  const budget = $derived($run.request ? $run.request.time_limit_s : null)
</script>

<section class="progress" data-testid="run-progress">
  <h1 class="title" data-testid="run-progress-title">Working</h1>
  <p class="lead" data-testid="run-progress-lead">
    One request, one answer — the service reports nothing until the whole pipeline finishes,
    so these stages are what is queued, not a live trace.
  </p>

  <ol class="stages" data-testid="run-progress-stages">
    {#each STAGES as stage (stage)}
      <li class="stage" data-testid="run-progress-stage" data-stage={stage}><span class="box" aria-hidden="true"></span>{stage}</li>
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

  .clock {
    margin-top: 26px;
    padding-top: 16px;
    border-top: 1px solid var(--rule-soft);
    font-size: 18px;
    color: var(--ink);
  }
  .budget { font-size: var(--fs-mono-sm); color: var(--ink-soft); }
</style>
