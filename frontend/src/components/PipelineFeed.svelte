<script>
  import { formatClock } from '../lib/format.js'
  import { run } from '../lib/run.js'

  /** Distance from the bottom that still counts as "reading the tail". */
  const STICK_PX = 8
  /** The events that report the model rather than the pipeline. They are worth
      showing — a retry is why a run went slow — but they must not out-shout the
      four stages, which are what the reader is waiting on. */
  const QUIET = ['model.call', 'model.retry', 'model.response']

  let list = $state(null)

  /** Which rows have their raw text open, keyed by feed row id. Per row, and
      never open by default: a raw answer runs to thousands of characters, and
      unfolding all of them would bury the run it is meant to explain. */
  let open = $state({})

  function toggle(id) {
    open = { ...open, [id]: !open[id] }
  }

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
        <div class="line">
          <span class="time mono">{formatClock(row.t_s)}</span>
          <span class="text">{row.text}</span>
          <!-- Only a row that carries the model's own answer has anything to
               unfold; every other event is already whole in its sentence. -->
          {#if row.detail}
            <button
              type="button"
              class="toggle"
              data-testid="pipeline-feed-toggle"
              data-event={row.event}
              aria-expanded={open[row.id] === true}
              onclick={() => toggle(row.id)}
            >{open[row.id] ? 'hide raw' : 'show raw'}</button>
          {/if}
        </div>
        {#if row.detail && open[row.id]}
          <pre
            class="detail mono"
            data-testid="pipeline-feed-detail"
            data-event={row.event}>{row.detail}</pre>
        {/if}
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
    padding: 6px 0;
    font-size: var(--fs-ui);
    line-height: 1.5;
  }
  .row + .row { border-top: 1px solid var(--rule-soft); }

  .line {
    display: flex;
    align-items: baseline;
    gap: 12px;
  }

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

  .toggle {
    flex-shrink: 0;
    padding: 0;
    border: 0;
    background: none;
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
    color: var(--ink-soft);
    text-decoration: underline;
    text-underline-offset: 2px;
  }
  .toggle:hover { color: var(--ink); }

  /* Muted like the row that opened it, but readable: this is the one thing in
     the feed somebody opened deliberately. */
  .detail {
    margin: 6px 0 0 calc(5ch + 12px);
    padding: 0 0 0 10px;
    max-height: 220px;
    overflow: auto;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font-size: var(--fs-mono-sm);
    line-height: 1.55;
    color: var(--ink-soft);
    border-left: 1px solid var(--rule-soft);
  }
</style>
