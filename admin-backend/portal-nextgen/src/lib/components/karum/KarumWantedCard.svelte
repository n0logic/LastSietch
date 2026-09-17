<script>
  import { wantedGradeLabel, tierLabel } from './grade.js';

  let { request, canFill = false, onFill, onInspect } = $props();

  let price = $derived(Number(request?.price) || 0);
  let stack = $derived(Number(request?.stack_size) || 1);
  // Rendered on the card itself, not behind the fill dialog: a filler who cannot read the
  // grade from the board would open the dialog only to find nothing of theirs qualifies.
  let wantedGrade = $derived(wantedGradeLabel(request?.quality_level, request?.quality_mode));
  let tierText = $derived(tierLabel(request?.tier));
  let funding = $derived(
    request?.funded === true ? 'funded' : request?.funded === false ? 'low' : 'unknown'
  );
</script>

<article class="card" data-funding={funding}>
  <div class="art">
    <span class="side mono">Wanted</span>
    <span class="name" title={request.display_name}>{request.display_name || request.template_id}</span>
    <span class="qty mono">exact stack x{stack.toLocaleString()}</span>
    <span class="grade mono" title="Grade this order accepts">{[tierText, wantedGrade].filter(Boolean).join(' \u00b7 ')}</span>
  </div>

  <div class="body">
    <div class="offer">
      <span class="price mono">{price.toLocaleString()}</span>
      <span class="unit mono">Solari total</span>
    </div>
    <p class="funding mono">
      {funding === 'funded' ? 'funded now' : funding === 'low' ? 'low funds' : 'funds unknown'}
    </p>
    <p class="requester mono">wanted by {request.requester_name || 'unknown'}</p>
    <button class="details" type="button" onclick={() => onInspect?.(request)}>Details and comparison</button>
    {#if canFill}
      <button class="btn" type="button" onclick={() => onFill?.(request)}>Fill order</button>
    {:else}
      <p class="hint mono">sign in to fill</p>
    {/if}
  </div>
</article>

<style>
  .details { min-height: 40px; border: 1px solid var(--edge); border-radius: 3px; background: transparent; color: var(--text); font: inherit; font-size: 13px; cursor: pointer; }
  .card { display: flex; flex-direction: column; background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm); overflow: hidden; transition: border-color var(--motion-fast) var(--ease-out), transform var(--motion-fast) var(--ease-out); }
  .card:hover { transform: translateY(-2px); border-color: var(--edge-hi); }
  .card[data-funding='funded'] { border-color: color-mix(in srgb, var(--ls-ibad) 42%, var(--edge)); }
  .art { position: relative; aspect-ratio: 16 / 9; background: var(--bg-deep); display: flex; flex-direction: column; align-items: center; justify-content: center; gap: var(--space-1); padding: var(--space-3); text-align: center; }
  .side { position: absolute; top: 7px; left: 8px; font-size: 9px; color: var(--accent); text-transform: uppercase; letter-spacing: .12em; }
  .name { font-size: var(--text-sm); color: var(--text); line-height: 1.25; }
  .qty, .funding, .requester, .hint { margin: 0; font-size: var(--text-xs); color: var(--text-muted); }
  .grade { font-size: 10px; text-transform: uppercase; letter-spacing: .08em; color: var(--accent-text); }
  .body { display: flex; flex-direction: column; gap: var(--space-2); padding: var(--space-3); }
  .offer { display: flex; align-items: baseline; gap: var(--space-1); }
  .price { font-size: var(--text-lg); color: var(--accent-bright); font-variant-numeric: tabular-nums; }
  .unit { font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: .08em; }
  .funding { text-transform: uppercase; letter-spacing: .08em; }
  .card[data-funding='funded'] .funding { color: var(--ls-ibad); }
  .card[data-funding='low'] .funding { color: var(--ls-yellow); }
  .requester { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .hint { text-transform: uppercase; letter-spacing: .08em; }
  .btn { font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase; padding: var(--space-1) var(--space-2); border-radius: var(--radius-sm); color: var(--bg-deep); background: var(--accent); border: 1px solid var(--accent); cursor: pointer; }
  .btn:hover { filter: brightness(1.08); }
  .btn:active { transform: translateY(1px); }
</style>
