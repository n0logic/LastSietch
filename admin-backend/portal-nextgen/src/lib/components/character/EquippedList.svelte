<script>
  // Equipped gear: a LIST card (the 3D gear stage is deferred per owner decision
  // #1 in the contract). Two sections: worn gear (source "equipped") and hotbar
  // weapons (source "hotbar", slots "Slot 1".."Slot 8" or "Stowed") - hotbar was
  // previously invisible and is exactly where augments live, so it is the more
  // interesting half of this card. Each row keeps the original anatomy (icon
  // glyph, name, slot + category meta, quality badge), adds a compact durability
  // readout where cur_dur/max_dur parse, and augment chips beneath rows that
  // carry them. When ctrl_scoped is false, a subtle "last logout" note shows
  // because the relay could not scope to the selected char.
  //
  // Reroll/swap ships DARK behind equipped.augments_enabled (assumed nested on
  // this same object - see the contract note in $lib/augments.svelte.js). When
  // that flag is false, no augmented row gets the action button at all: no
  // affordance, per the house "disabled feature renders nothing" rule.
  import { iconUrl } from '$lib/icons.js';
  import AugmentChips from './AugmentChips.svelte';
  import AugmentActionDialog from './AugmentActionDialog.svelte';

  let { equipped = null } = $props();

  let items = $derived(Array.isArray(equipped?.items) ? equipped.items : []);
  let hotbar = $derived(items.filter((it) => it.source === 'hotbar'));
  let worn = $derived(items.filter((it) => it.source !== 'hotbar'));
  let sections = $derived([
    { key: 'equipped', label: 'Worn gear', items: worn, emptyNote: 'No worn gear equipped.' },
    { key: 'hotbar', label: 'Hotbar', items: hotbar, emptyNote: 'No hotbar weapons equipped.' },
  ]);

  let augmentsEnabled = $derived(equipped?.augments_enabled === true);
  let playerOnline = $derived(equipped?.player_online ?? null);
  let ownedAugments = $derived(Array.isArray(equipped?.owned_augments) ? equipped.owned_augments : []);

  let dialogItem = $state(null);
  function openAugmentDialog(it) { dialogItem = it; }
  function closeAugmentDialog() { dialogItem = null; }

  // cur_dur/max_dur arrive as strings that may be empty or non-numeric; parse
  // defensively and drop the readout entirely rather than show "NaN%".
  function durability(it) {
    const cur = parseFloat(it?.cur_dur);
    const max = parseFloat(it?.max_dur);
    if (!isFinite(cur) || !isFinite(max) || max <= 0) return null;
    return {
      pct: Math.round(Math.max(0, Math.min(1, cur / max)) * 100),
      curFmt: cur.toFixed(1),
      maxFmt: max.toFixed(1),
    };
  }

  let equippedCount = $derived(equipped?.equipped_count ?? worn.length);
  let hotbarCount = $derived(equipped?.hotbar_count ?? hotbar.length);
  let augmentedCount = $derived(equipped?.augmented_count ?? 0);
</script>

<div class="equipped">
  <p class="panel-kicker mono">Equipped</p>
  {#if !equipped || items.length === 0}
    <p class="hollow">No equipped gear on record.</p>
  {:else}
    <p class="summary mono">
      {equippedCount} equipped, {hotbarCount} hotbar{augmentedCount ? `, ${augmentedCount} augmented` : ''}
    </p>
    {#each sections as sec (sec.key)}
      <section class="section">
        <div class="shead">
          <h3 class="sname">{sec.label}</h3>
          <span class="scount mono">{sec.items.length}</span>
        </div>
        {#if sec.items.length === 0}
          <p class="hollow-mini">{sec.emptyNote}</p>
        {:else}
          <ul class="list" role="list">
            {#each sec.items as it, i (it.item_id ?? i)}
              {@const d = durability(it)}
              <li class="row">
                <img class="glyph" src={iconUrl(it.icon)} alt="" aria-hidden="true" loading="lazy" />
                <div class="body">
                  <span class="name">{it.name || it.template || 'Item'}</span>
                  <span class="meta mono">
                    {#if it.slot}<span class="slot">{it.slot}</span>{/if}
                    {#if it.category}<span class="cat">{it.category}</span>{/if}
                  </span>
                  <AugmentChips augments={it.augments} />
                  {#if augmentsEnabled && it.augments?.length > 0}
                    <button class="aug-btn mono" type="button" onclick={() => openAugmentDialog(it)}>
                      <span aria-hidden="true">&#9881;</span> Reroll or swap
                    </button>
                  {/if}
                </div>
                <div class="badges">
                  {#if it.quality != null}<span class="quality mono" title="Quality">G{it.quality}</span>{/if}
                  {#if d}
                    <span class="dur mono" title="Durability: {d.curFmt} / {d.maxFmt}">
                      <span class="dur-lbl">Dur</span> {d.pct}%
                    </span>
                  {/if}
                </div>
              </li>
            {/each}
          </ul>
        {/if}
      </section>
    {/each}
    {#if equipped.ctrl_scoped === false}
      <p class="note mono">Reflects your last logout for this character.</p>
    {/if}
  {/if}
</div>

{#if dialogItem}
  <AugmentActionDialog
    item={dialogItem}
    {ownedAugments}
    {playerOnline}
    onClose={closeAugmentDialog}
  />
{/if}

<style>
  .panel-kicker { margin: 0 0 var(--space-3); color: var(--accent); text-transform: uppercase; letter-spacing: .18em; font-size: var(--text-xs); }
  .hollow { color: var(--text-muted); font-size: var(--text-sm); margin: var(--space-2) 0; }
  .summary { margin: 0 0 var(--space-3); color: var(--text-muted); font-size: var(--text-xs); letter-spacing: .04em; }

  .section { display: flex; flex-direction: column; gap: var(--space-1); margin-bottom: var(--space-4); }
  .section:last-child { margin-bottom: 0; }
  .shead { display: flex; align-items: baseline; gap: var(--space-2); }
  .sname { margin: 0; font-family: var(--font-display); font-size: var(--text-sm); text-transform: uppercase; letter-spacing: .08em; color: var(--text-muted); }
  .scount { font-size: var(--text-xs); color: var(--accent-text); }
  .hollow-mini { margin: 0; font-size: var(--text-xs); color: var(--text-muted); }

  .list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-1); }
  .row {
    display: flex; align-items: center; gap: var(--space-3);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-3);
  }
  .glyph { width: 34px; height: 34px; object-fit: contain; flex: 0 0 auto; }
  .body { display: flex; flex-direction: column; min-width: 0; flex: 1 1 auto; }
  .name { font-size: var(--text-sm); color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .meta { display: flex; gap: var(--space-2); font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .06em; }
  .slot { color: var(--accent-text); }

  .badges { display: flex; flex-direction: column; align-items: flex-end; gap: 2px; flex: 0 0 auto; }
  .quality { font-size: var(--text-xs); letter-spacing: .06em; color: var(--accent-text); }
  .dur { font-size: var(--text-xs); letter-spacing: .04em; color: var(--text-muted); white-space: nowrap; }
  .dur-lbl { text-transform: uppercase; letter-spacing: .06em; }

  .aug-btn {
    align-self: flex-start; margin-top: var(--space-1);
    display: inline-flex; align-items: center; gap: 4px;
    font-size: var(--text-xs); letter-spacing: .04em; text-transform: uppercase;
    color: var(--accent-text); background: transparent; border: 1px solid var(--edge);
    border-radius: var(--radius-sm); padding: 2px var(--space-2); cursor: pointer;
    transition: border-color var(--motion-fast) var(--ease-out), color var(--motion-fast) var(--ease-out);
  }
  .aug-btn:hover { border-color: var(--accent); color: var(--accent-bright); }
  .aug-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

  .note { margin: var(--space-3) 0 0; font-size: var(--text-xs); color: var(--text-muted); }

  @media (prefers-reduced-motion: reduce) {
    .aug-btn { transition: none; }
  }
</style>
