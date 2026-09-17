<script>
  // One Karum listing card. Forked from bases/ListingCard: same carved-card shape,
  // different subject. No thumbnail (an item stack has no render), so the art slot
  // carries the grade instead, which is the thing a buyer actually squints at.
  //
  // The ONLY identity on this card is the seller's name, and it is here because
  // listing is itself the opt-in (owner decision D2). Nothing else about them is
  // knowable from this surface.
  import DurabilityMeter from '$lib/components/storage/DurabilityMeter.svelte';
  import { gradeLabel, tierLabel } from './grade.js';
  import { iconUrl } from '$lib/icons.js';

  let { listing, canBuy = false, onBuy, onInspect } = $props();

  let price = $derived(Number(listing?.price) || 0);
  let stack = $derived(Number(listing?.stack_size) || 1);
  let grade = $derived(Number(listing?.quality_level) || 0);
  let gradeText = $derived(gradeLabel(listing?.quality_level));
  let tierText = $derived(tierLabel(listing?.tier));
  let each = $derived(stack > 1 ? price / stack : null);
  let durability = $derived(
    listing?.durability_max ? { current: listing.durability_cur, max: listing.durability_max } : null
  );
</script>

<article class="card" data-grade={grade}>
  <div class="art">
    {#if listing.icon}<img class="item-icon" src={iconUrl(listing.icon)} alt="" loading="lazy" />{/if}
    <span class="name" title={listing.display_name}>{listing.display_name || listing.template_id}</span>
    <span class="qty mono">&times;{stack.toLocaleString()}</span>
    {#if tierText || grade > 0}
      <span class="grade mono" title="Tier and item grade">{[tierText, grade > 0 ? gradeText : null].filter(Boolean).join(' \u00b7 ')}</span>
    {/if}
  </div>

  <div class="body">
    <div class="ask">
      <span class="price mono">{price.toLocaleString()}</span>
      <span class="unit mono">Solari</span>
    </div>
    {#if each}
      <p class="each mono">{each.toLocaleString(undefined, { maximumFractionDigits: 2 })} each</p>
    {/if}
    {#if durability}
      <DurabilityMeter {durability} size="ring" label="Condition" />
    {/if}
    <p class="seller mono">from {listing.seller_name || 'unknown'}</p>
    <button class="details" type="button" onclick={() => onInspect?.(listing)}>Details and comparison</button>
    {#if canBuy}
      <button class="btn" type="button" onclick={() => onBuy?.(listing)}>Buy</button>
    {:else}
      <p class="hint mono">sign in to buy</p>
    {/if}
  </div>
</article>

<style>
  .item-icon { width: 56px; height: 56px; object-fit: contain; margin: 8px 0; }
  .details { min-height: 40px; border: 1px solid var(--edge); border-radius: 3px; background: transparent; color: var(--text); font: inherit; font-size: 13px; cursor: pointer; }
  .card {
    display: flex; flex-direction: column;
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    overflow: hidden;
    transition: border-color var(--motion-fast) var(--ease-out), transform var(--motion-fast) var(--ease-out);
  }
  .card:hover { transform: translateY(-2px); border-color: var(--edge-hi); }
  .art {
    position: relative; aspect-ratio: 16 / 9; background: var(--bg-deep);
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    gap: var(--space-1); padding: var(--space-3); text-align: center;
  }
  /* Grade tint on the shelf edge, the same device the bases cards use for faction. */
  .card[data-grade='4'] .art { box-shadow: inset 0 -2px 0 color-mix(in srgb, var(--ls-ibad) 60%, transparent); }
  .card[data-grade='5'] .art { box-shadow: inset 0 -2px 0 color-mix(in srgb, var(--accent) 70%, transparent); }
  .card[data-grade='6'] .art { box-shadow: inset 0 -2px 0 color-mix(in srgb, var(--ls-yellow) 80%, transparent); }
  .name { font-size: var(--text-sm); color: var(--text); line-height: 1.25; }
  .qty { font-size: var(--text-xs); color: var(--text-muted); }
  .grade {
    position: absolute; top: 6px; right: 6px; font-size: 10px;
    color: var(--bg-deep); background: var(--accent-bright);
    border-radius: var(--radius-sm); padding: 1px var(--space-1);
  }
  .body { display: flex; flex-direction: column; gap: var(--space-2); padding: var(--space-3); }
  .ask { display: flex; align-items: baseline; gap: var(--space-1); }
  .price { font-size: var(--text-lg); color: var(--accent-bright); font-variant-numeric: tabular-nums; }
  .unit { font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .1em; }
  .each { margin: 0; font-size: var(--text-xs); color: var(--text-muted); }
  .seller { margin: 0; font-size: var(--text-xs); color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .hint { margin: 0; font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .08em; }
  .btn {
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase;
    padding: var(--space-1) var(--space-2); border-radius: var(--radius-sm);
    color: var(--bg-deep); background: var(--accent); border: 1px solid var(--accent); cursor: pointer;
  }
  .btn:hover { filter: brightness(1.08); }
</style>
