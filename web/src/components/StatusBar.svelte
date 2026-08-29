<script>
  import { countBySeverity } from '../lib/severity.js'
  import { formatCount, joinDot } from '../lib/format.js'

  let { findings = null, reviewed = true } = $props()

  const count = $derived(findings ? findings.length : 0)
  const counts = $derived(countBySeverity(findings || []))

  // "Review · 0" would read as a clean review; a skipped one has to say so.
  const tab = $derived(
    findings ? (reviewed ? `Review · ${count}` : 'Review · not run') : 'Review',
  )

  const breakdown = $derived(
    findings && findings.length
      ? joinDot([
          counts.blocker ? formatCount(counts.blocker, 'blocker') : '',
          counts.marginal ? `${counts.marginal} marginal` : '',
          counts.note ? formatCount(counts.note, 'note') : '',
        ])
      : '',
  )
</script>

<footer class="bar">
  <!-- Schematic and Board are the Option B seam. They are hash fragments so a
       tab switch never reaches the server, and both stay inert until built. -->
  <span class="lbl tab" aria-disabled="true">Schematic</span>
  <span class="lbl tab" aria-disabled="true">Board</span>
  <a class="lbl tab current" href="#review" aria-current="page">{tab}</a>
  <div class="spacer"></div>
  {#if breakdown}<span class="mono breakdown">{breakdown}</span>{/if}
</footer>

<style>
  .bar {
    height: var(--statusbar-h);
    border-top: 1px solid var(--rule);
    background: var(--well);
    display: flex;
    align-items: center;
    flex-shrink: 0;
  }

  .tab {
    display: flex;
    align-items: center;
    height: 100%;
    padding: 0 14px;
    border-right: 1px solid var(--rule-soft);
    color: var(--ink-faint);
    text-decoration: none;
  }
  .tab[aria-disabled='true'] { cursor: default; }
  .current { color: var(--ink); }
  .current:hover { color: var(--ink); }

  .spacer { flex-grow: 1; }
  .breakdown { font-size: var(--fs-lbl); color: var(--ink-soft); padding: 0 16px 0 0; }
</style>
