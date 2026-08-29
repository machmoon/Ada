<script>
  import Icon from './Icon.svelte'
  import { formatCount, formatDuration, joinDot } from '../lib/format.js'

  let { intent = '', result = null } = $props()

  // Every number here comes off the response; nothing is filled in when there
  // is no run to describe.
  const meta = $derived(
    result
      ? joinDot([
          `read ${formatCount(result.datasheets.length, 'datasheet')}`,
          formatDuration(result.duration_s),
        ])
      : '',
  )
</script>

<header class="bar">
  <div class="brand">
    <Icon name="brand" size={17} />
    <span class="wordmark">silkscreen</span>
  </div>
  {#if intent}<span class="intent">{intent}</span>{/if}
  <div class="spacer"></div>
  {#if meta}<span class="mono meta">{meta}</span>{/if}
</header>

<style>
  .bar {
    height: var(--titlebar-h);
    display: flex;
    align-items: center;
    border-bottom: 1px solid var(--rule);
    background: var(--surface);
    flex-shrink: 0;
  }

  .brand {
    display: flex;
    align-items: center;
    gap: 9px;
    padding: 0 16px;
    border-right: 1px solid var(--rule-soft);
    height: 100%;
  }
  .wordmark { font-weight: 600; font-size: 14px; letter-spacing: -.01em; }

  .intent {
    font-size: var(--fs-ui);
    color: var(--ink-mid);
    padding: 0 12px;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .spacer { flex-grow: 1; }
  .meta { font-size: var(--fs-mono-sm); color: var(--ink-soft); padding: 0 16px; }
</style>
