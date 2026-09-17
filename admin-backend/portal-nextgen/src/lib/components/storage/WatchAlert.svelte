<script>
  // Add-price-alert popover form. Available on BOTH panels (bank + container) and
  // NOT offline-gated (a market watch is a safe online action). Posts a target
  // price to the watchlist; a semantic failure surfaces inline, never a fake ok.
  import { api } from '$lib/api.js';

  let { item, onDone } = $props();

  let price = $state('');
  let busy = $state(false);
  let phase = $state('idle'); // idle | ok | failed
  let message = $state('');

  let priceNum = $derived(Math.floor(Number(price)) || 0);
  let name = $derived(item?.name || item?.template || 'item');

  async function add() {
    if (busy || priceNum <= 0) return;
    busy = true; phase = 'idle'; message = '';
    try {
      const r = await api.market.watchAdd({ template_id: item?.template, max_price: priceNum });
      phase = 'ok';
      message = `Alert set at or below ${(r?.max_price ?? priceNum).toLocaleString()} Solari.`;
      setTimeout(() => onDone?.(), 900);
    } catch (e) {
      phase = 'failed';
      message = e?.message?.includes('cap') ? 'Your watchlist is full.' : 'Could not set the alert.';
    } finally { busy = false; }
  }
</script>

<div class="form">
  <p class="head mono">Price alert &middot; {name}</p>
  <label class="fld"><span>Notify at or below</span>
    <input type="number" min="1" step="1" inputmode="numeric" bind:value={price} placeholder="Solari" aria-label="Target price" />
  </label>
  <div class="row">
    <button class="btn primary" type="button" onclick={add} disabled={busy || priceNum <= 0}>{busy ? 'Saving' : 'Set alert'}</button>
    <button class="btn" type="button" onclick={() => onDone?.()}>Cancel</button>
  </div>
  {#if phase !== 'idle' && message}
    <p class="status" data-phase={phase} role="status" aria-live="polite">{message}</p>
  {/if}
</div>

<style>
  .form { display: flex; flex-direction: column; gap: var(--space-2); }
  .head { margin: 0; font-size: var(--text-xs); color: var(--accent); text-transform: uppercase; letter-spacing: .1em; }
  /* min-width:0 lets these shrink inside the 1fr 1fr grid when the popover is
     width-capped to a narrow panel; grid/flex items default to min-width:auto
     and would otherwise refuse to go below their content width. */
  .fld { min-width: 0; display: flex; flex-direction: column; gap: var(--space-1); font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .06em; }
  .fld input {
    font-family: var(--font-mono); font-variant-numeric: tabular-nums; font-size: var(--text-sm); color: var(--text);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-1) var(--space-2); text-transform: none; letter-spacing: normal;
  }
  .fld input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .row { display: flex; gap: var(--space-2); }
  .btn { flex: 1 1 0; font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase; padding: var(--space-1) var(--space-2); border-radius: var(--radius-sm); color: var(--text); background: var(--metal-1); border: 1px solid var(--edge); cursor: pointer; }
  .btn.primary { color: var(--bg-deep); background: var(--accent); border-color: var(--accent); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
  .status { margin: 0; font-size: var(--text-xs); }
  .status[data-phase='ok'] { color: var(--ls-green); }
  .status[data-phase='failed'] { color: var(--ls-red); }
</style>
