<script>
  // Reserves the voice slot so the form does not reflow when the talk feature
  // lands. It is inert by construction: no handler, no event, always disabled.
  const LABEL = 'Voice input — not yet built'
</script>

<button type="button" class="mic" data-testid="mic-button" disabled title={LABEL} aria-label={LABEL}>
  <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
    <rect x="5.5" y="1.5" width="5" height="8" rx="2.5" stroke="var(--ink-faint)" stroke-width="1.3" />
    <path d="M3 7.5v.5a5 5 0 0 0 10 0v-.5M8 13v2M5.5 15h5" stroke="var(--ink-faint)" stroke-width="1.3" stroke-linecap="round" />
  </svg>
</button>

<style>
  .mic {
    width: var(--mic-slot);
    height: var(--mic-slot);
    display: flex;
    align-items: center;
    justify-content: center;
    background: transparent;
    border: 1px solid var(--rule-soft);
    border-radius: var(--radius);
    flex-shrink: 0;
  }
</style>
