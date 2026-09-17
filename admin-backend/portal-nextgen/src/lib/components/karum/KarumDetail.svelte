<script>
  import { onMount, untrack } from 'svelte';
  import { api } from '$lib/api.js';
  import { iconUrl } from '$lib/icons.js';
  import Modal from '$lib/components/Modal.svelte';
  import WatchAlert from '$lib/components/storage/WatchAlert.svelte';
  import { gradeLabel, wantedGradeLabel, tierLabel } from './grade.js';

  let { item, canTrade = false, onTrade, onClose } = $props();
  let chosen = $state(untrack(() => item));
  let related = $state([]);
  let status = $state('loading');
  let more = $state(false);
  let watch = $state(false);
  let wanted = $derived(chosen.request_id != null);
  let quantity = $derived(Number(chosen.stack_size));
  let total = $derived(Number(chosen.price));
  let unit = $derived(Number.isFinite(total) && quantity > 0 ? total / quantity : null);
  let grade = $derived(wanted ? wantedGradeLabel(chosen.quality_level, chosen.quality_mode) : gradeLabel(chosen.quality_level));
  let template = $derived(chosen.template_id);

  onMount(() => {
    let active = true;
    api.karum.search({ template: item.template_id, side: item.request_id != null ? 'wanted' : 'sell', sort: 'cheap' })
      .then((response) => {
        if (!active) return;
        related = Array.isArray(response?.rows) ? response.rows : [];
        more = response?.more === true;
        status = 'ready';
      }).catch(() => { if (active) status = 'error'; });
    return () => { active = false; };
  });

  function price(value) {
    return Number.isFinite(value) ? value.toLocaleString(undefined, { maximumFractionDigits: 2 }) : 'Unavailable';
  }
</script>

<Modal size="karum-detail" title={wanted ? 'Wanted order details' : 'Item details'} {onClose}>
  <div class="identity"><img src={iconUrl(chosen.icon)} alt="" /><div><p>{tierLabel(chosen.tier) || 'Tier unavailable'} · {grade || 'Grade unavailable'}</p><h2>{chosen.display_name || template}</h2><span>{wanted ? 'Wanted by' : 'Listed by'} {chosen.requester_name || chosen.seller_name || 'another player'}</span></div></div>
  <dl class="figures">
    <div><dt>{wanted ? 'Required stack' : 'Quantity'}</dt><dd>{price(quantity)}</dd></div>
    <div><dt>Solari total</dt><dd>{price(total)}</dd></div>
    <div><dt>Solari per unit</dt><dd>{price(unit)}</dd></div>
  </dl>
  {#if chosen.durability_max != null}<p class="condition">Condition: {price(Number(chosen.durability_cur))} / {price(Number(chosen.durability_max))}</p>{/if}
  {#if wanted}<p class="note">This order needs one exact stack of {price(quantity)}. Accepted grade: {grade}. Funding is checked again when you fill it.</p>{/if}
  {#if canTrade}<button class="primary" type="button" onclick={() => onTrade?.(chosen)}>{wanted ? 'Review matching items' : 'Review purchase'}</button>{:else}<p class="note">Sign in to {wanted ? 'fill this order' : 'buy this item'}.</p>{/if}

  <section class="comparison">
    <h3>{wanted ? 'Compare wanted orders' : 'Compare listings'}</h3>
    <p class="note">Each row is a separate stack. Check grade and quantity before comparing prices.</p>
    {#if status === 'loading'}<p class="skeleton">Reading listings</p>{:else if status === 'error'}<p class="note">Other listings could not be read.</p>{:else if !related.length}<p class="note">No active listings remain for this item.</p>{:else}
      <div class="comparison-head"><span>Trader / grade</span><span>Quantity</span><span>Per unit / total</span></div>
      {#each related as row (row.listing_id ?? row.request_id)}
        <button class="comparison-row" class:selected={(row.listing_id ?? row.request_id) === (chosen.listing_id ?? chosen.request_id)} type="button" onclick={() => { chosen = row; watch = false; }}>
          <span>{row.seller_name || row.requester_name}<small>{row.request_id != null ? wantedGradeLabel(row.quality_level, row.quality_mode) : gradeLabel(row.quality_level) || 'Grade unavailable'}</small></span>
          <span class="mono">{price(Number(row.stack_size))}</span>
          <span class="mono">{price(Number(row.price) / Number(row.stack_size))}<small>{price(Number(row.price))} total</small></span>
        </button>
      {/each}
      {#if more}<p class="note">Showing the first {related.length} listings.</p>{/if}
    {/if}
  </section>
  <div class="market-links"><a href={`/exchange?tpl=${encodeURIComponent(template)}`}>Compare on the Exchange ↗</a>{#if canTrade}<button type="button" aria-expanded={watch} onclick={() => (watch = !watch)}>Watch an Exchange price</button>{/if}</div>
  {#if watch}<div class="watch"><p class="note">This watches the CHOAM Exchange price for this item. It does not watch Karum listings or a specific grade.</p><WatchAlert item={{ template, name: chosen.display_name }} onDone={() => (watch = false)} /></div>{/if}
</Modal>

<style>
  :global(.modal.size-karum-detail) { top: 0; right: 0; left: auto; bottom: 0; transform: none; width: min(600px, 100vw); max-width: none; max-height: 100dvh; border-radius: 0; }
  .identity { display: flex; gap: 18px; align-items: center; }
  .identity img { width: 72px; height: 72px; object-fit: contain; background: var(--bg-deep); border: 1px solid var(--edge); border-radius: 6px; padding: 8px; }
  .identity p { margin: 0 0 4px; color: var(--accent-text); font-size: 13px; }
  .identity h2 { font-size: 32px; line-height: 1.1; margin-bottom: 6px; }
  .identity span { font-size: 13px; color: var(--text-muted); }
  .figures { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; border-block: 1px solid var(--edge); padding: 20px 0; margin: 24px 0 16px; }
  dt { color: var(--text-muted); font-size: 12px; } dd { margin: 6px 0 0; font: 600 18px var(--font-mono); overflow-wrap: anywhere; }
  .condition, .note { font-size: 13px; line-height: 1.6; color: var(--text-muted); }
  .primary { min-height: 44px; width: 100%; padding: 10px 16px; background: var(--accent-soft); color: var(--text); border: 1px solid var(--accent); border-radius: 4px; font: inherit; cursor: pointer; }
  .comparison { margin-top: 28px; }
  h3 { font-size: 23px; }
  .comparison-head, .comparison-row { display: grid; grid-template-columns: minmax(0, 1fr) 70px minmax(110px, .8fr); align-items: center; gap: 12px; }
  .comparison-head { color: var(--text-muted); font-size: 11px; padding: 12px 10px; }
  .comparison-row { width: 100%; text-align: left; background: transparent; color: var(--text); padding: 12px 10px; border: 1px solid var(--edge); margin-bottom: 6px; border-radius: 4px; cursor: pointer; font: inherit; font-size: 13px; }
  .comparison-row.selected { border-color: var(--accent-soft); background: var(--bg-elevated); }
  .comparison-row span:last-child, .comparison-head span:last-child { text-align: right; }
  small { display: block; font-size: 11px; color: var(--text-muted); margin-top: 4px; }
  .market-links { display: flex; flex-direction: column; align-items: flex-start; gap: 12px; margin-top: 24px; }
  .market-links a, .market-links button { padding: 8px 0; background: none; border: 0; color: var(--accent-text); font: inherit; font-size: 14px; text-decoration: none; cursor: pointer; }
  .watch { padding: 16px; border: 1px solid var(--edge); border-radius: 4px; }
  @media (max-width: 600px) { :global(.modal.size-karum-detail) { top: 4dvh; max-height: 96dvh; border-radius: 12px 12px 0 0; } .identity h2 { font-size: 27px; } .identity img { width: 56px; height: 56px; } dd { font-size: 16px; } }
</style>
