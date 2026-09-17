<script>
  // List-on-Exchange popover form. Pawn-side sources only: the CHOAM bank, and the
  // character's own backpack while its kill switch is on. Both re-hydrate from the DB at
  // login, which is what the writer's offline gate covers; base containers and vehicles
  // live in the partition's RAM and are refused. Tradeable items only, and the grid that
  // renders the cell decides whether the entry point exists at all (allowMarketSell ->
  // ItemCell). Posts the V2 JSON sell path (market.sell),
  // which funds the listing fee from the bank and answers a clean {ok, fee,
  // bank_after, error, message} envelope instead of the V1 HTML token fragment. The
  // seller's controller + ownership are resolved and re-verified server-side; the
  // client only supplies the container, item, count, price, duration + a uuid
  // idempotency key. Fail-closed: a non-2xx / {ok:false} throws and surfaces inline.
  // This write is OFFLINE-gated server-side; the honest gate copy comes back on a refusal.
  import { api, uuidv4 } from '$lib/api.js';

  let { item, containerId, onDone } = $props();

  const DURATIONS = [1, 3, 7, 14];

  let price = $state('');
  // svelte-ignore state_referenced_locally
  let count = $state(String(Number(item?.stack_size) || 1));
  let duration = $state(7);
  let busy = $state(false);
  let phase = $state('idle'); // idle | ok | failed
  let message = $state('');

  let priceNum = $derived(Math.floor(Number(price)) || 0);
  let countNum = $derived(Math.max(1, Math.min(Math.floor(Number(count)) || 1, Number(item?.stack_size) || 1)));
  let name = $derived(item?.name || item?.template || 'item');

  // Mint the idempotency key once per intended listing; reuse it across retries so
  // a resend never double-posts. Reset it only on a fresh outcome (ok / hard fail).
  let uuid = uuidv4();

  async function list() {
    if (busy || priceNum <= 0) return;
    busy = true; phase = 'idle'; message = '';
    try {
      const r = await api.market.sell({
        container_id: containerId,
        item_id: item?.item_id,
        count: countNum,
        price: priceNum,
        duration_days: duration,
        tpl: item?.template || '',
        uuid,
      });
      phase = 'ok';
      const fee = typeof r?.fee === 'number' ? ` Fee ${r.fee.toLocaleString()}.` : '';
      message = `Listed ${countNum} for ${priceNum.toLocaleString()} each (${duration}d).${fee}`;
      uuid = uuidv4(); // next listing gets a fresh key
      setTimeout(() => onDone?.(), 1000);
    } catch (e) {
      phase = 'failed';
      message = e?.message === 'player_online'
        ? 'Log out of the game to list items.'
        : 'The listing did not post.';
    } finally { busy = false; }
  }
</script>

<div class="form">
  <p class="head mono">List on Exchange &middot; {name}</p>
  <div class="grid2">
    <label class="fld"><span>Price each</span>
      <input type="number" min="1" step="1" inputmode="numeric" bind:value={price} placeholder="Solari" aria-label="Unit price" />
    </label>
    <label class="fld"><span>Count</span>
      <input type="number" min="1" step="1" inputmode="numeric" bind:value={count} aria-label="Count to list" />
    </label>
  </div>
  <label class="fld"><span>Duration</span>
    <select bind:value={duration} aria-label="Listing duration in days">
      {#each DURATIONS as d}<option value={d}>{d} day{d === 1 ? '' : 's'}</option>{/each}
    </select>
  </label>
  <!-- Claim lane, said plainly: the exchange keeps the item in its own escrow and hands
       it back through the in-game Completed tab. Nothing returns to the inventory it came
       from, so a player who lists from the backpack should not go looking there. -->
  <p class="lane">A canceled or expired listing returns to the CHOAM Completed tab in game, not to your backpack.</p>
  <div class="row">
    <button class="btn primary" type="button" onclick={list} disabled={busy || priceNum <= 0}>{busy ? 'Listing' : 'List'}</button>
    <button class="btn" type="button" onclick={() => onDone?.()}>Cancel</button>
  </div>
  {#if phase !== 'idle' && message}
    <p class="status" data-phase={phase} role="status" aria-live="polite">{message}</p>
  {/if}
</div>

<style>
  .form { display: flex; flex-direction: column; gap: var(--space-2); }
  .head { margin: 0; font-size: var(--text-xs); color: var(--accent); text-transform: uppercase; letter-spacing: .1em; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-2); }
  /* min-width:0 lets these shrink inside the 1fr 1fr grid when the popover is
     width-capped to a narrow panel; grid/flex items default to min-width:auto
     and would otherwise refuse to go below their content width. */
  .fld { min-width: 0; display: flex; flex-direction: column; gap: var(--space-1); font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .06em; }
  .fld input, .fld select {
    font-family: var(--font-mono); font-variant-numeric: tabular-nums; font-size: var(--text-sm); color: var(--text);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-1) var(--space-2); text-transform: none; letter-spacing: normal;
  }
  .fld input:focus-visible, .fld select:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .lane { margin: 0; font-size: var(--text-xs); color: var(--text-muted); line-height: 1.4; }
  .row { display: flex; gap: var(--space-2); }
  .btn { flex: 1 1 0; font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase; padding: var(--space-1) var(--space-2); border-radius: var(--radius-sm); color: var(--text); background: var(--metal-1); border: 1px solid var(--edge); cursor: pointer; }
  .btn.primary { color: var(--bg-deep); background: var(--accent); border-color: var(--accent); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
  .status { margin: 0; font-size: var(--text-xs); }
  .status[data-phase='ok'] { color: var(--ls-green); }
  .status[data-phase='failed'] { color: var(--ls-red); }
</style>
