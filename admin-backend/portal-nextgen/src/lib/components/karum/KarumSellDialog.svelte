<script>
  // List one bank or backpack stack at a fixed ask. Forked from storage/SellDialog,
  // which was
  // already ~90% of a "list this" form; the differences are that the whole stack
  // goes (no partial split, because a split reintroduces a delete-plus-insert
  // window) and there is no duration, because a Karum listing has no TTL.
  //
  // 🔒 THE ONLY OFFLINE-GATED LEG IN THE FEATURE. Listing TAKES the item out of your
  // bank or backpack, and a take from a loaded session is restored under its original id,
  // which is a duplication path. So the form seals while you are online OR while we
  // cannot confirm you are logged out. The server hard-gates twice more regardless;
  // this is honest UI, not the boundary.
  import { karum, listItem, loadCatalog, loadSellable, uuidv4 } from '$lib/karum.svelte.js';
  import KarumItemSearch from './KarumItemSearch.svelte';
  import { gradeLabel } from './grade.js';

  let { onClose } = $props();

  let itemId = $state(null);
  let price = $state('');
  let busy = $state(false);
  let phase = $state('idle');   // idle | ok | deferred | failed
  let message = $state('');

  let locked = $derived(!karum.offlineOk);
  let undetermined = $derived(karum.online == null);
  let items = $derived(karum.sellable.items);
  let catalogByTemplate = $derived(new Map(
    (karum.catalog.items || []).map((entry) => [entry.template_id, entry])
  ));
  let itemOptions = $derived(items.map((item) => {
    const meta = catalogByTemplate.get(item.template) || {};
    const grade = Number(item?.durability?.quality) || 0;
    return {
      key: item.item_id,
      name: meta.name || item.name || item.template,
      template: item.template,
      category: meta.category || item.category || '',
      detail: `x${Number(item.stack_size || 1).toLocaleString()}${grade > 0 ? ` | ${gradeLabel(grade)}` : ''}${item.src === 'backpack' ? ' | backpack' : ''}`,
    };
  }));
  // Tradeable bank items the Karum will not touch because their template has no CHOAM
  // Exchange category, and so no way to be handed back if the sale is cancelled. Named
  // out loud: silently withholding an item the player can see in-game reads as a bug.
  let hiddenNoCategory = $derived(karum.sellable.hiddenNoCategory || 0);
  let noCategoryNote = $derived(
    `${hiddenNoCategory} ${hiddenNoCategory === 1 ? 'item is' : 'items are'} held back: ` +
    'nobody has ever listed one on the CHOAM Exchange, so it has no Exchange category and ' +
    'the Karum would have no way to give it back to you.');
  let chosen = $derived(items.find((i) => i.item_id === itemId) || null);
  let priceNum = $derived(Math.floor(Number(price)) || 0);
  let maxPrice = $derived(karum.caps.max_price || 900000000);
  let priceOk = $derived(priceNum >= 1 && priceNum <= maxPrice);
  let capLeft = $derived(Math.max(0, (karum.caps.listings_per_day || 0) - (karum.caps.listed_today || 0)));
  let canList = $derived(!busy && !locked && chosen != null && priceOk && capLeft > 0);

  // One key per intended listing, reused across retries so a resend replays
  // server-side instead of escrowing twice. Reset only after a real outcome.
  let uuid = uuidv4();

  $effect(() => {
    if (karum.sellable.status === 'idle') loadSellable();
    if (karum.catalog.status === 'idle') loadCatalog();
  });

  async function submit() {
    if (!canList) return;
    busy = true; phase = 'idle'; message = '';
    const r = await listItem({
      itemId: chosen.item_id,
      containerId: chosen?.container_id ?? karum.sellable.containerId,
      price: priceNum,
      template: chosen.template || '',
      uuid,
    });
    if (r.ok) {
      phase = 'ok';
      message = `Listed ${chosen.name || chosen.template} for ${priceNum.toLocaleString()} Solari.`;
      uuid = uuidv4();
      itemId = null; price = '';
      setTimeout(() => onClose?.(), 1200);
    } else if (r.deferred) {
      phase = 'deferred';
      message = r.message;
    } else {
      phase = 'failed';
      message = r.message;
    }
    busy = false;
  }
</script>

<div class="form">
  <p class="head mono">List on the Karum</p>

  {#if !karum.flags.karum_enabled}
    <p class="soon" role="note">
      The Karum is not open yet. You can line up a listing, but nothing moves until it is.
    </p>
  {/if}

  {#if locked}
    <p class="gate" role="note">
      {#if undetermined}
        Cannot confirm you are logged out, so listing is locked. Items only leave your bank or backpack while you are offline.
      {:else}
        Log out of the game to list an item. Items only leave your bank or backpack while you are offline.
      {/if}
    </p>
  {/if}

  {#if karum.sellable.status === 'loading'}
    <p class="hollow mono">reading your bank and backpack</p>
  {:else if items.length === 0}
    <p class="hollow">
      Nothing in your CHOAM bank or backpack can be listed right now. Only tradeable items count, and
      anything already listed is hidden.
    </p>
    {#if hiddenNoCategory > 0}
      <p class="hollow">{noCategoryNote}</p>
    {/if}
  {:else}
    {#if hiddenNoCategory > 0}
      <p class="hollow">{noCategoryNote}</p>
    {/if}
    <KarumItemSearch id="karum-sell-item" options={itemOptions} selected={itemId}
                     onSelect={(value) => (itemId = value)} label="Item from your bank"
                     placeholder="Search your eligible stacks" disabled={locked} />

    <label class="fld"><span>Ask (whole stack)</span>
      <input type="number" min="1" max={maxPrice} step="1" inputmode="numeric"
             bind:value={price} placeholder="Solari" disabled={locked}
             aria-label="Asking price in Solari" />
    </label>

    <p class="note mono">
      The whole stack goes. Cap {maxPrice.toLocaleString()} Solari &middot;
      {capLeft} of {karum.caps.listings_per_day} listings left today
    </p>
  {/if}

  <div class="row">
    <button class="btn primary" type="button" onclick={submit} disabled={!canList}>
      {busy ? 'Listing' : 'List'}
    </button>
    <button class="btn" type="button" onclick={() => onClose?.()}>Close</button>
  </div>

  {#if phase !== 'idle' && message}
    <p class="status" data-phase={phase} role="status" aria-live="polite">{message}</p>
  {/if}
</div>

<style>
  .form { display: flex; flex-direction: column; gap: var(--space-2); }
  .head { margin: 0; font-size: var(--text-xs); color: var(--accent); text-transform: uppercase; letter-spacing: .1em; }
  .soon { margin: 0; font-size: var(--text-xs); color: var(--accent-text); line-height: 1.4; }
  .gate {
    margin: 0; font-size: var(--text-xs); line-height: 1.4; color: var(--text-muted);
    padding: var(--space-1) var(--space-2); border-radius: var(--radius-sm);
    border: 1px solid color-mix(in srgb, var(--ls-yellow) 45%, var(--edge));
    background: color-mix(in srgb, var(--ls-yellow) 8%, var(--metal-0));
  }
  .hollow { margin: 0; font-size: var(--text-xs); color: var(--text-muted); line-height: 1.4; }
  .note { margin: 0; font-size: 10px; color: var(--text-muted); }
  .fld { min-width: 0; display: flex; flex-direction: column; gap: var(--space-1); font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .06em; }
  .fld input {
    font-family: var(--font-mono); font-variant-numeric: tabular-nums; font-size: var(--text-sm); color: var(--text);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-1) var(--space-2); text-transform: none; letter-spacing: normal;
  }
  .fld input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .fld input:disabled { opacity: .5; }
  .row { display: flex; gap: var(--space-2); }
  .btn { flex: 1 1 0; font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase; padding: var(--space-1) var(--space-2); border-radius: var(--radius-sm); color: var(--text); background: var(--metal-1); border: 1px solid var(--edge); cursor: pointer; }
  .btn.primary { color: var(--bg-deep); background: var(--accent); border-color: var(--accent); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
  .status { margin: 0; font-size: var(--text-xs); line-height: 1.4; }
  .status[data-phase='ok'] { color: var(--ls-green); }
  .status[data-phase='deferred'] { color: var(--accent-text); }
  .status[data-phase='failed'] { color: var(--ls-red); }
</style>
