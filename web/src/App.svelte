<script>
  import ErrorPanel from './components/ErrorPanel.svelte'
  import IntentForm from './components/IntentForm.svelte'
  import ReviewResults from './components/ReviewResults.svelte'
  import RunProgress from './components/RunProgress.svelte'
  import SideRail from './components/SideRail.svelte'
  import StatusBar from './components/StatusBar.svelte'
  import TitleBar from './components/TitleBar.svelte'
  import { ApiError, generate, normalizeRequest } from './lib/api.js'
  import { failRun, finishRun, resetRun, run, startRun } from './lib/run.js'

  async function submit(raw) {
    const request = normalizeRequest(raw)
    startRun(request)
    try {
      finishRun(await generate(request))
    } catch (err) {
      failRun(err instanceof ApiError ? err : new ApiError('internal', String(err)))
    }
  }

  // The review is optional, so the chrome has to know whether one happened
  // before it labels anything "checked".
  const reviewed = $derived($run.request ? $run.request.review !== false : true)

  function retry() {
    if ($run.request) submit($run.request)
  }
</script>

<div class="app">
  <TitleBar intent={$run.request ? $run.request.intent : ''} result={$run.result} />

  <div class="body">
    <main class="centre">
      {#if $run.phase === 'running'}
        <RunProgress />
      {:else if $run.phase === 'error'}
        <ErrorPanel error={$run.error} onretry={retry} ondismiss={resetRun} />
      {:else if $run.phase === 'done'}
        <ReviewResults result={$run.result} request={$run.request} onnew={resetRun} />
      {:else}
        <IntentForm onsubmit={submit} initial={$run.request} />
      {/if}
    </main>

    <SideRail result={$run.result} {reviewed} />
  </div>

  <StatusBar findings={$run.result ? $run.result.findings : null} {reviewed} />
</div>

<style>
  .app { height: 100%; display: flex; flex-direction: column; overflow: hidden; }
  .body { flex-grow: 1; display: flex; min-height: 0; }
  .centre { flex-grow: 1; min-width: 0; padding: 26px 34px; overflow-y: auto; }
</style>
