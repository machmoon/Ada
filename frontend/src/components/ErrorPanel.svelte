<script>
  let { error, onretry, ondismiss } = $props()

  const kind = $derived(error ? error.kind : 'internal')
</script>

<section class="panel" data-testid="error-panel" data-kind={kind}>
  {#if kind === 'no-api-key'}
    <!-- Not an outage: an unkeyed clone is the ordinary first run. Setup
         instructions come first, and there is no retry button above them. -->
    <h1 class="title" data-testid="error-panel-title">Silkscreen has no Gemini key yet</h1>
    <p class="lead" data-testid="error-panel-lead">
      The engine and the placer run without any key, but reading datasheets and proposing a
      circuit go through Gemini. Set the key where the service can see it, then restart it.
    </p>
    <ol class="steps" data-testid="error-panel-steps">
      <li><code class="mono">cp .env.example .env</code></li>
      <li>Put your key in the new file as <code class="mono">GOOGLE_API_KEY=…</code></li>
      <li>Restart the service so it picks the key up.</li>
    </ol>
    <p class="note" data-testid="error-panel-note">
      The <strong>Install</strong> section of the repo README covers where the key comes from
      and which parts of the pipeline need it.
    </p>
    <button type="button" class="secondary" data-testid="error-panel-action" data-action="dismiss" onclick={ondismiss}>Back to the form</button>

  {:else if kind === 'timeout'}
    <h1 class="title" data-testid="error-panel-title">The run ran too long</h1>
    <p class="lead" data-testid="error-panel-lead">
      Nothing came back within 300 seconds, so the request was cancelled. A smaller board or a
      shorter solver budget usually finishes; the service may still be working on this one.
    </p>
    <button type="button" class="primary" data-testid="error-panel-action" data-action="retry" onclick={onretry}>Try again</button>

  {:else if kind === 'network'}
    <h1 class="title" data-testid="error-panel-title">The service is not running</h1>
    <p class="lead" data-testid="error-panel-lead">
      The browser could not reach it at all. Start it with
      <code class="mono">PORT=8081 python -m service.app</code> and try again.
    </p>
    <button type="button" class="primary" data-testid="error-panel-action" data-action="retry" onclick={onretry}>Try again</button>

  {:else if kind === 'validation'}
    <h1 class="title" data-testid="error-panel-title">The request was rejected</h1>
    <p class="lead" data-testid="error-panel-lead">{error.message}</p>
    <button type="button" class="primary" data-testid="error-panel-action" data-action="dismiss" onclick={ondismiss}>Edit the request</button>

  {:else if kind === 'too-large'}
    <h1 class="title" data-testid="error-panel-title">The request is too large</h1>
    <p class="lead" data-testid="error-panel-lead">{error.message}</p>
    <button type="button" class="primary" data-testid="error-panel-action" data-action="dismiss" onclick={ondismiss}>Edit the request</button>

  {:else if kind === 'not-found'}
    <h1 class="title" data-testid="error-panel-title">No such endpoint</h1>
    <p class="lead" data-testid="error-panel-lead">
      {error.message} The bundle and the service are probably different versions of the app.
    </p>
    <button type="button" class="primary" data-testid="error-panel-action" data-action="dismiss" onclick={ondismiss}>Back to the form</button>

  {:else if kind === 'internal'}
    <h1 class="title" data-testid="error-panel-title">The service hit an internal error</h1>
    <p class="lead" data-testid="error-panel-lead">
      Nothing was produced. If you report this, quote the error id below — it is the only thing
      that ties your request to a line in the service log.
    </p>
    {#if error.errorId}<div class="mono errid" data-testid="error-panel-error-id">{error.errorId}</div>{/if}
    <button type="button" class="primary" data-testid="error-panel-action" data-action="retry" onclick={onretry}>Try again</button>

  {:else}
    <h1 class="title" data-testid="error-panel-title">The model provider failed</h1>
    <p class="lead" data-testid="error-panel-lead">{error.message}</p>
    <button type="button" class="primary" data-testid="error-panel-action" data-action="retry" onclick={onretry}>Try again</button>
  {/if}
</section>

<style>
  .panel { max-width: var(--measure-detail); }

  .title { font-size: var(--fs-h1); font-weight: 600; letter-spacing: -.02em; }
  .lead {
    font-size: var(--fs-body);
    color: var(--ink-mid);
    line-height: 1.55;
    max-width: var(--measure-lead);
    margin-top: 8px;
  }
  .note {
    font-size: var(--fs-ui);
    color: var(--ink-soft);
    line-height: 1.6;
    max-width: var(--measure-lead);
    margin-top: 14px;
  }

  .steps {
    margin: 18px 0 0;
    padding-left: 20px;
    display: flex;
    flex-direction: column;
    gap: 9px;
    font-size: var(--fs-ui);
    color: var(--ink-mid);
    line-height: 1.6;
  }

  code {
    background: var(--well);
    padding: 2px 6px;
    font-size: var(--fs-mono);
  }

  .errid {
    margin-top: 16px;
    padding: 8px 12px;
    background: var(--well);
    border: 1px solid var(--rule-soft);
    font-size: var(--fs-mono);
    color: var(--ink);
    display: inline-block;
  }

  .primary, .secondary {
    display: block;
    margin-top: 22px;
    font-size: 12px;
    font-weight: 500;
    padding: 8px 15px;
    border-radius: var(--radius);
  }
  .primary { background: var(--oxblood); color: var(--surface); border: none; }
  .secondary { background: transparent; color: var(--ink-mid); border: 1px solid var(--rule); font-weight: 400; }
</style>
