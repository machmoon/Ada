<script>
  import Icon from './Icon.svelte'
  import { formatCount, formatDuration, joinDot } from '../lib/format.js'
  import { prefersDark, readStored, resolveTheme, toggleTheme, writeStored } from '../lib/theme.js'

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

  // The stored choice, not the theme: no choice yet has to stay no choice so
  // the stylesheet keeps following the OS until somebody says otherwise.
  let choice = $state(readStored(globalThis.localStorage))
  const theme = $derived(resolveTheme(choice, prefersDark(globalThis)))

  function flipTheme() {
    choice = toggleTheme(theme)
    writeStored(globalThis.localStorage, choice)
    document.documentElement.dataset.theme = choice
  }
</script>

<header class="bar" data-testid="title-bar">
  <div class="brand" data-testid="title-bar-brand">
    <Icon name="brand" size={17} />
    <span class="wordmark">silkscreen</span>
  </div>
  {#if intent}<span class="intent" data-testid="title-bar-intent">{intent}</span>{/if}
  <div class="spacer"></div>
  {#if meta}<span class="mono meta" data-testid="title-bar-meta">{meta}</span>{/if}
  <button
    type="button"
    class="lbl theme"
    aria-pressed={theme === 'dark'}
    aria-label="Night mode"
    onclick={flipTheme}
    data-testid="title-bar-theme"
    data-theme-choice={theme}
  >Night</button>
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

  /* The brand rule, hung off the other edge of the bar. */
  .theme {
    display: flex;
    align-items: center;
    height: 100%;
    padding: 0 16px;
    border: none;
    border-left: 1px solid var(--rule-soft);
    background: transparent;
    color: var(--ink-faint);
  }
  .theme:hover { color: var(--ink); }
  /* Lit like a current tab, because pressed here means the app is dark. */
  .theme[aria-pressed='true'] { color: var(--ink); }
  /* The global ring sits 1 px outside the element, which the bar clips. */
  .theme:focus-visible { outline-offset: -2px; }
</style>
