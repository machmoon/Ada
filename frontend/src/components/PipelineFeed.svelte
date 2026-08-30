<script>
  import { formatClock } from '../lib/format.js'
  import { run } from '../lib/run.js'

  /** Distance from the bottom that still counts as "reading the tail". */
  const STICK_PX = 8
  /** The events that report the model rather than the pipeline. They are worth
      showing — a retry is why a run went slow — but they must not out-shout the
      four stages, which are what the reader is waiting on. */
  const QUIET = ['model.call', 'model.retry']

  let list = $state(null)

  // Plain let: nothing renders it, so making it reactive would schedule an
  // update for a value only the two effects below ever read.
  let stick = true

  const rows = $derived($run.feed || [])

  // Measured before the new rows are patched in: once one is in the DOM the old
  // scroll position no longer says whether the tail was being read.
  $effect.pre(() => {
    rows
    if (list) stick = list.scrollHeight - list.scrollTop - list.clientHeight <= STICK_PX
  })

  $effect(() => {
    rows
    if (list && stick) list.scrollTop = list.scrollHeight
  })
</script>

<!-- Nothing at all before the first event: the stage list above already says
     what is happening, and an empty box under it would say it worse. -->
{#if rows.length}
  <ol class="feed" bind:this={list} data-testid="pipeline-feed">
    {#each rows as row (row.id)}
      <li
        class="row"
        class:quiet={QUIET.includes(row.event)}
        data-testid="pipeline-feed-row"
        data-event={row.event}
      >
        <span class="time mono">{formatClock(row.t_s)}</span>
        <span class="text">{row.text}</span>
      </li>
    {/each}
  </ol>
{/if}

<style>
  .feed {
    list-style: none;
    margin: 22px 0 0;
    padding: 0;
    max-width: var(--measure-detail);
    max-height: 320px;
    overflow-y: auto;
    border-top: 1px solid var(--rule-soft);
  }

  .row {
    display: flex;
    align-items: baseline;
    gap: 12px;
    padding: 6px 0;
    font-size: var(--fs-ui);
    line-height: 1.5;
  }
  .row + .row { border-top: 1px solid var(--rule-soft); }

  .time {
    flex-shrink: 0;
    width: 5ch;
    text-align: right;
    font-size: var(--fs-mono-sm);
    color: var(--ink-soft);
  }
  .text { color: var(--ink); overflow-wrap: anywhere; }

  .quiet .text { color: var(--ink-faint); }
  .quiet .time { color: var(--ink-faint); }
</style>
