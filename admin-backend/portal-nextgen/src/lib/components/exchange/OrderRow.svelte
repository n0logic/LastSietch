<script>
  // One of my listings. Active rows carry Cancel + Relist (both online-safe, wired to
  // the v2 order endpoints); completed/history rows are read-only. There is NO expiry
  // countdown anywhere: the backend exposes no expires_at/created_at, so we ship an
  // honest "Listed" state instead of faking a clock. All amber chrome.
  import { cancelOrder, relistOrder } from '$lib/exchange.svelte.js';
  import { iconUrl } from '$lib/icons.js';

  // bucket: 'active' | 'completed' | 'history'
  let { order, bucket = 'active' } = $props();

  const DURATIONS = [1, 3, 7, 14];

  // The server names the completion (Sold / Purchased / Canceled / Expired from the
  // game's completion_type); only a sale or a purchase earns the green state. A
  // pulled listing showed as SOLD here before (live QA 2026-09-03).
  let settled = $derived(order?.completion_type === 4 || order?.completion_type === 5);
  let canRelist = $derived(bucket === 'completed' && order?.completion_type === 3);
  let busy = $state(false);
  let relisting = $state(false);
  // Prefill the relist price from the current listing (one-shot seed, not reactive).
  // svelte-ignore state_referenced_locally
  let price = $state(String(Number(order?.price) || ''));
  let duration = $state(7);

  let name = $derived(order?.name || order?.template_id || 'Listing');
  let qty = $derived(Number(order?.qty) || Number(order?.count) || 0);
  let unit = $derived(Number(order?.price) || 0);
  let grade = $derived(order?.quality != null ? Number(order.quality) : null);
  let priceNum = $derived(Math.floor(Number(price)) || 0);

  async function doCancel() {
    if (busy) return;
    busy = true; await cancelOrder(order); busy = false;
  }
  async function doRelist() {
    if (busy || priceNum <= 0) return;
    busy = true;
    const ok = await relistOrder(order, priceNum, duration);
    busy = false;
    if (ok) relisting = false;
  }
</script>

<div class="order">
  <img class="icon" src={iconUrl(order?.icon)} alt="" aria-hidden="true" loading="lazy" />
  <div class="body">
    <span class="name" title={name}>{name}{#if grade != null}<span class="grade mono">G{grade}</span>{/if}</span>
    <span class="meta mono">
      {qty.toLocaleString()} &times; {unit.toLocaleString()}
      {#if bucket === 'active'}<span class="state listed">Listed</span>
      {:else if bucket === 'completed'}<span class="state" class:done={settled} class:past={!settled} title={order?.status_sub || ''}>{order?.status_label || 'Closed'}</span>
      {:else}<span class="state past">{order?.status_label || 'Closed'}</span>{/if}
    </span>
  </div>

  <!-- The writer's rules, not the tab's: only an ACTIVE listing can be cancelled and
       only a CANCELED one (completion_type 3) can be relisted. Relist used to sit on
       the active row, where the writer refuses it, and never on the canceled row
       (live QA 2026-09-03). -->
  {#if bucket === 'active' || canRelist}
    <div class="ops">
      {#if !relisting}
        {#if canRelist}<button class="op" type="button" onclick={() => (relisting = true)} disabled={busy}>Relist</button>{/if}
        {#if bucket === 'active'}<button class="op danger" type="button" onclick={doCancel} disabled={busy}>{busy ? '...' : 'Cancel'}</button>{/if}
      {/if}
    </div>
    {#if relisting}
      <div class="relist">
        <label class="fld"><span>Price</span>
          <input type="number" min="1" step="1" inputmode="numeric" bind:value={price} aria-label="Relist price" />
        </label>
        <label class="fld"><span>Days</span>
          <select bind:value={duration} aria-label="Relist duration">
            {#each DURATIONS as d}<option value={d}>{d}d</option>{/each}
          </select>
        </label>
        <div class="relist-actions">
          <button class="op primary" type="button" onclick={doRelist} disabled={busy || priceNum <= 0}>{busy ? '...' : 'Renew'}</button>
          <button class="op" type="button" onclick={() => (relisting = false)} disabled={busy}>Back</button>
        </div>
      </div>
    {/if}
  {/if}
</div>

<style>
  .order {
    display: grid; align-items: center; gap: var(--space-2);
    grid-template-columns: 30px minmax(0, 1fr) auto;
    padding: var(--space-2) var(--space-3);
    border: 1px solid var(--edge); border-radius: var(--radius-sm); background: var(--metal-0);
  }
  .icon { width: 30px; height: 30px; object-fit: contain; }
  .body { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
  .name { font-size: var(--text-sm); color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; display: inline-flex; align-items: baseline; gap: var(--space-2); }
  .grade { font-size: 10px; color: var(--text-muted); }
  .meta { font-size: var(--text-xs); color: var(--text-muted); font-variant-numeric: tabular-nums; display: inline-flex; align-items: center; gap: var(--space-2); }
  .state { font-size: 9px; text-transform: uppercase; letter-spacing: .08em; padding: 1px 5px; border-radius: 2px; border: 1px solid var(--edge); }
  .state.listed { color: var(--accent-text); border-color: color-mix(in srgb, var(--accent) 45%, var(--edge)); }
  .state.done { color: var(--ls-green); }
  .state.past { color: var(--text-muted); }

  .ops { display: inline-flex; gap: var(--space-1); }
  .op { font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .08em; text-transform: uppercase; padding: var(--space-1) var(--space-2); border-radius: var(--radius-sm); color: var(--text); background: var(--metal-1); border: 1px solid var(--edge); cursor: pointer; }
  .op:hover:not(:disabled) { border-color: var(--accent); }
  .op.primary { color: var(--bg-deep); background: var(--accent); border-color: var(--accent); }
  .op.danger:hover:not(:disabled) { border-color: var(--ls-red); color: var(--ls-red); }
  .op:disabled { opacity: .45; cursor: not-allowed; }

  .relist { grid-column: 1 / 4; display: flex; align-items: flex-end; gap: var(--space-2); margin-top: var(--space-2); padding-top: var(--space-2); border-top: 1px solid var(--edge); flex-wrap: wrap; }
  .fld { display: flex; flex-direction: column; gap: 2px; font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: .06em; }
  .fld input { width: 6rem; }
  .fld input, .fld select {
    font-family: var(--font-mono); font-variant-numeric: tabular-nums; font-size: var(--text-sm); color: var(--text);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm); padding: var(--space-1) var(--space-2); text-transform: none; letter-spacing: normal;
  }
  .fld input:focus-visible, .fld select:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .relist-actions { display: inline-flex; gap: var(--space-1); margin-left: auto; }
</style>
