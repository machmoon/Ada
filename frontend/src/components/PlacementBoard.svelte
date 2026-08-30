<script>
  let { board, label = '', score = null } = $props()

  function size(component) {
    return component.angle === 90 || component.angle === 270
      ? [component.height, component.width]
      : [component.width, component.height]
  }
</script>

<section class="panel" data-testid="placement-board" data-label={label}>
  <div class="head">
    <div>
      <div class="lbl">{label}</div>
      <strong>{score ? `hard ${Number(score.hard).toFixed(3)} · soft ${Number(score.soft).toFixed(3)}` : ''}</strong>
    </div>
    {#if score}<span class:legal={Number(score.hard) === 0}>{Number(score.hard) === 0 ? 'legal' : 'faulty'}</span>{/if}
  </div>

  <div class="well">
    <svg viewBox={`0 0 ${board.width} ${board.height}`} role="img" aria-label={`${label} PCB placement`}>
      <rect class="edge" x=".25" y=".25" width={board.width - 0.5} height={board.height - 0.5} />
      {#each board.keepouts || [] as keepout (keepout.name)}
        <rect
          class="keepout"
          x={keepout.x}
          y={board.height - keepout.y - keepout.height}
          width={keepout.width}
          height={keepout.height}
        />
      {/each}
      {#each board.components as component (component.ref)}
        {@const dims = size(component)}
        {@const svgY = board.height - component.y - dims[1]}
        <g data-ref={component.ref}>
          <rect
            class:fixed={component.fixed}
            class:power={component.kind === 'power' || component.kind === 'driver'}
            x={component.x}
            y={svgY}
            width={dims[0]}
            height={dims[1]}
          />
          <text x={component.x + dims[0] / 2} y={svgY + dims[1] / 2}>{component.ref}</text>
        </g>
      {/each}
    </svg>
  </div>
</section>

<style>
  .panel { min-width: 0; border: 1px solid var(--rule); background: var(--surface); }
  .head { min-height: 58px; display: flex; align-items: center; justify-content: space-between; padding: 10px 13px; border-bottom: 1px solid var(--rule-soft); }
  .head strong { display: block; margin-top: 5px; font-family: var(--font-mono); font-size: var(--fs-mono-sm); font-weight: 400; color: var(--ink-mid); }
  .head > span { font-family: var(--font-mono); font-size: var(--fs-lbl); text-transform: uppercase; color: var(--sev-blocker-fg); }
  .head > span.legal { color: var(--green); }
  .well { padding: 12px; background: var(--well-bg); }
  svg { display: block; width: 100%; max-height: 360px; }
  .edge { fill: #0B3157; stroke: var(--board-edge-cuts); stroke-width: .35; }
  g rect { fill: rgba(200, 52, 52, .55); stroke: var(--board-courtyard); stroke-width: .28; }
  g rect.power { fill: rgba(77, 127, 196, .7); }
  g rect.fixed { stroke: var(--board-silkscreen); stroke-width: .5; }
  text { fill: var(--board-silkscreen); font: 1.8px var(--font-mono); text-anchor: middle; dominant-baseline: middle; transform-box: fill-box; }
  .keepout { fill: rgba(255, 255, 255, .08); stroke: var(--board-grid-dot); stroke-width: .25; stroke-dasharray: 1 1; }
</style>
