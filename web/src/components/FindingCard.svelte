<script>
  import Citation from './Citation.svelte'
  import SeverityChip from './SeverityChip.svelte'
  import { formatParts } from '../lib/format.js'
  import { severityInfo } from '../lib/severity.js'

  let { finding } = $props()

  const info = $derived(severityInfo(finding.severity))
  const scope = $derived(formatParts(finding.parts))
  const fix = $derived(String(finding.suggested_fix || '').trim())
  const citation = $derived(String(finding.citation || '').trim())
</script>

<article class="card" data-sev={info.key}>
  <div class="chips">
    <SeverityChip severity={finding.severity} />
    {#if scope}<span class="mono scope">{scope}</span>{/if}
  </div>

  <h3 class="title">{finding.title}</h3>
  {#if finding.detail}<p class="detail">{finding.detail}</p>{/if}

  {#if citation || fix}
    <div class="footer">
      <Citation {citation} />
      <div class="spacer"></div>
      {#if fix}
        <!-- Secondary style on purpose: nothing in the app applies a fix, so
             this must not read as the primary action. -->
        <button type="button" class="fix" title="Suggested fix — nothing is applied automatically">{fix}</button>
      {/if}
    </div>
  {/if}
</article>

<style>
  .card {
    background: var(--surface);
    border: 1px solid var(--rule-soft);
    border-left: var(--sev-bar-w) solid var(--sev-note-rule);
    padding: 15px 18px;
  }
  .card[data-sev='blocker'] { border-left-color: var(--sev-blocker-rule); }
  .card[data-sev='marginal'] { border-left-color: var(--sev-marginal-rule); }

  .chips { display: flex; align-items: center; gap: 11px; margin-bottom: 7px; flex-wrap: wrap; }
  .scope { font-size: var(--fs-mono); color: var(--teal); }

  .title {
    font-size: var(--fs-card-title);
    font-weight: 600;
    letter-spacing: -.005em;
    margin-bottom: 6px;
  }

  .detail {
    font-size: var(--fs-detail);
    color: var(--ink-mid);
    line-height: 1.6;
    margin-bottom: 11px;
    max-width: var(--measure-detail);
  }

  .footer {
    display: flex;
    align-items: center;
    gap: 16px;
    padding-top: 10px;
    border-top: 1px solid var(--rule-soft);
  }
  .spacer { flex-grow: 1; }

  .fix {
    font-size: 12px;
    padding: 6px 13px;
    background: transparent;
    color: var(--ink-mid);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    text-align: left;
  }
</style>
