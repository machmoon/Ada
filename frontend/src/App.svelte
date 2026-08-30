<script>
  import BoardWell from './components/BoardWell.svelte'
  import DebugConsole from './components/DebugConsole.svelte'
  import ErrorPanel from './components/ErrorPanel.svelte'
  import IntentForm from './components/IntentForm.svelte'
  import PipelineFeed from './components/PipelineFeed.svelte'
  import ReviewResults from './components/ReviewResults.svelte'
  import RunProgress from './components/RunProgress.svelte'
  import SideRail from './components/SideRail.svelte'
  import StatusBar from './components/StatusBar.svelte'
  import TitleBar from './components/TitleBar.svelte'
  import { ApiError, generateStream, normalizeRequest } from './lib/api.js'
  import { highlightRefs, readPlacements } from './lib/board.js'
  import { pcbText } from './lib/download.js'
  import { logEvent } from './lib/log.js'
  import { failRun, finishRun, resetRun, run, stageEvent, startRun } from './lib/run.js'
  import { resolveTab } from './lib/tabs.js'

  // The tab lives in the hash fragment; App is the only thing that reads it,
  // and the status bar's links are what write it.
  let hash = $state(window.location.hash)
  let selected = $state(-1)
  let debugOpen = $state(false)

  $effect(() => {
    const onhash = () => {
      hash = window.location.hash
    }
    window.addEventListener('hashchange', onhash)
    return () => window.removeEventListener('hashchange', onhash)
  })

  async function submit(raw) {
    const request = normalizeRequest(raw)
    selected = -1
    startRun(request)
    try {
      // Streamed, with a one-shot fallback inside generateStream for a service
      // that has no streaming endpoint: stageEvent is simply never called there.
      finishRun(await generateStream(request, stageEvent))
    } catch (err) {
      failRun(err instanceof ApiError ? err : new ApiError('internal', String(err)))
    }
  }

  // The review is optional, so the chrome has to know whether one happened
  // before it labels anything "checked".
  const reviewed = $derived($run.request ? $run.request.review !== false : true)

  const placements = $derived($run.phase === 'done' ? readPlacements($run.result) : null)
  const boardEnabled = $derived(placements !== null)
  const tab = $derived(resolveTab(hash, { board: boardEnabled }))
  const pcb = $derived($run.result ? pcbText($run.result) : '')

  const findings = $derived($run.result ? $run.result.findings : [])
  const selectedFinding = $derived(findings[selected] || null)
  // Keyed off the board's own designators, not the spec names the finding
  // is written in, and filtered against the parts actually placed — see
  // highlightRefs for why an unmatched list is worse than an empty one.
  const highlightedRefs = $derived(highlightRefs(selectedFinding, placements))

  // Plain let, not $state: the effect below both reads and writes it, and a
  // reactive one would wake the effect it just settled.
  let lastTab = ''

  // Watches the resolved tab rather than the click, so a hash link typed into
  // the address bar and the board-disabled fallback are both recorded.
  $effect(() => {
    const to = tab
    if (to === lastTab) return
    const from = lastTab
    lastTab = to
    logEvent('ui.tab', `tab ${from || 'none'} → ${to}`, { from, to, board_enabled: boardEnabled })
  })

  function goTab(name) {
    window.location.hash = name
    hash = `#${name}`
  }

  /** Clicking the same finding again deselects it. */
  function selectFinding(index) {
    selected = selected === index ? -1 : index
    const finding = findings[index] || null
    const on = selected === index
    logEvent('ui.finding', `finding ${index} ${on ? 'selected' : 'deselected'}`, {
      index,
      selected: on,
      severity: finding ? finding.severity : '',
      parts: finding ? finding.parts : [],
      title: finding ? finding.title : '',
    })
  }

  function showOnBoard(index) {
    selected = index
    logEvent('ui.show-on-board', `finding ${index} shown on the board`, { index })
    goTab('board')
  }

  function newBoard() {
    selected = -1
    logEvent('ui.new-board', 'started another board', {})
    goTab('review')
    resetRun()
  }

  function retry() {
    if (!$run.request) return
    // Logged before the submit, which mints the next run id: the id worth
    // recording here is the run being retried.
    logEvent('ui.retry', `retrying run ${$run.id}`, { id: $run.id })
    submit($run.request)
  }
</script>

<div class="app" data-testid="app-root">
  <TitleBar intent={$run.request ? $run.request.intent : ''} result={$run.result} />

  <div class="body" data-testid="app-body">
    <main class="centre" data-testid="app-main">
      {#if $run.phase === 'running'}
        <RunProgress />
        <PipelineFeed />
      {:else if $run.phase === 'error'}
        <ErrorPanel error={$run.error} onretry={retry} ondismiss={resetRun} />
      {:else if $run.phase === 'done'}
        {#if tab === 'board' && placements}
          <div class="hint" data-testid="app-hint">
            {#if selectedFinding}
              <span class="lbl">{highlightedRefs.length ? 'showing' : 'no placed part for'}</span>
              <span class="hint-title" data-testid="app-hint-title">{selectedFinding.title}</span>
              <button type="button" class="clear" data-testid="app-clear-selection" onclick={() => (selected = -1)}>
                Clear selection
              </button>
            {:else}
              <span class="lbl">every part · select a finding in the review to highlight one</span>
            {/if}
          </div>
          <BoardWell {placements} {highlightedRefs} {pcb} />
        {:else}
          <ReviewResults
            result={$run.result}
            request={$run.request}
            onnew={newBoard}
            {selected}
            {boardEnabled}
            onselect={selectFinding}
            onshowboard={showOnBoard}
          />
        {/if}
      {:else}
        <IntentForm onsubmit={submit} initial={$run.request} />
      {/if}
    </main>

    <SideRail result={$run.result} {reviewed} />
  </div>

  <!-- A push drawer between the body and the bar, not an overlay: it takes its
       height out of the layout, so nothing is ever hidden behind it. -->
  {#if debugOpen}
    <DebugConsole onclose={() => (debugOpen = false)} />
  {/if}

  <StatusBar
    findings={$run.result ? $run.result.findings : null}
    {reviewed}
    {tab}
    {boardEnabled}
    {debugOpen}
    ondebug={() => (debugOpen = !debugOpen)}
  />
</div>

<style>
  .app { height: 100%; display: flex; flex-direction: column; overflow: hidden; }
  .body { flex-grow: 1; display: flex; min-height: 0; }
  .centre { flex-grow: 1; min-width: 0; padding: 26px 34px; overflow-y: auto; }

  .hint {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 12px;
    min-height: 28px;
  }
  .hint-title {
    font-size: var(--fs-ui);
    color: var(--ink);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .clear {
    font-size: 12px;
    padding: 4px 11px;
    background: transparent;
    color: var(--ink-mid);
    border: 1px solid var(--rule);
    border-radius: var(--radius);
    white-space: nowrap;
  }
</style>
