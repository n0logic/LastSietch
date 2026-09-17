<script>
  // Augmented-gear roll-up: every item across the player's containers + the
  // CHOAM bank that carries an installed augment. Augments are rare (279 items
  // in the whole live DB), so most players see an empty list here - that is
  // the expected, honest state, not a bug.
  //
  // WIRED 2026-08-02. The storage read path now sends `augments` in the same
  // {name, label, grade, rolls[], effects[]} shape the Character card consumes.
  // Getting there meant unpicking THREE separate whitelists that each selected
  // the field and then dropped it: build_all() in dune-container-items.py (the
  // mirror blob's item projection), _decorate_portal_items() in portal.py (the
  // API's), and the label/effects decoration that only the equipped reader
  // applied. Until all three agreed this panel read empty for every player.
  // It stays defensive anyway - an item with no `augments` key renders nothing
  // rather than breaking - so a partial deploy degrades quietly.
  import { untrack } from 'svelte';
  import { goto } from '$app/navigation';
  import { base } from '$app/paths';
  import { storage, selectContainer } from '$lib/storage.svelte.js';
  import { api } from '$lib/api.js';
  import { iconUrl } from '$lib/icons.js';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import ItemInfoPopover from '$lib/components/item/ItemInfoPopover.svelte';
  import AugmentActionDialog from '$lib/components/character/AugmentActionDialog.svelte';
  import { groupRolls } from '$lib/augments.svelte.js';

  let rows = $state([]);
  let fetchStatus = $state('idle'); // idle | loading | ready
  let fetchedSig = $state(null);
  let failed = $state(false);
  let truncated = $state(false);
  let augmentsEnabled = $state(false);
  let playerOnline = $state(null);
  let ownedAugments = $state([]);
  let dialogItem = $state(null);

  // Cheap signature so a re-render (e.g. after a move settles) only refetches
  // when the container set or its item counts actually changed.
  function containerSig() {
    const c = storage.containers.map((x) => `${x.id}:${x.item_count}`).join(',');
    const b = `${storage.bank?.inv_id ?? ''}:${storage.bank?.items?.length ?? 0}`;
    return `${c}|${b}`;
  }

  $effect(() => {
    if (storage.status !== 'ready') return;
    const sig = containerSig();
    if (sig === fetchedSig) return;
    untrack(() => loadAugmented(sig));
  });

  // ONE request. This used to fan out on the client - a container-items call per
  // owned container, all awaited in a single Promise.all - and it HUNG in
  // production 2026-08-02: the owner has 55 non-empty containers, only 12 of the
  // 55 requests ever reached the server, and Promise.all never settled, so the
  // page sat on loading skeletons forever. The server now does this pass over the
  // mirror blob it already holds.
  //
  // Two independent guarantees so this cannot wedge again:
  //   * one request instead of N, so there is no partial-completion state at all;
  //   * `finally` sets 'ready' unconditionally, so ANY failure lands on the honest
  //     empty message rather than an eternal skeleton. A page that says "none"
  //     when it cannot tell is still wrong, but it is recoverable by the player;
  //     a spinner that never stops is not.
  async function loadAugmented(sig) {
    fetchedSig = sig;
    fetchStatus = 'loading';
    try {
      const r = await api.storage.augmentedItems();
      rows = (Array.isArray(r?.rows) ? r.rows : []).map((x) => ({
        item: x.item,
        augments: Array.isArray(x.augments) ? x.augments : [],
        containerId: x.container_id,
        containerName: x.container_name || 'Container',
        isDD: !!x.is_deep_desert,
        isBank: !!x.is_bank,
        // Server's verdict on whether this row can be rerolled at all. Defaults
        // CLOSED: an older backend that does not send it renders no button
        // rather than one whose every write the writer refuses.
        canAugment: x.can_augment === true,
      }));
      truncated = !!r?.truncated;
      // Reroll/swap trio, same names the Character page uses so one dialog
      // component serves both surfaces. `augments_enabled` false => render no
      // entry point at all (house rule: never offer a door that is locked).
      augmentsEnabled = r?.augments_enabled === true;
      playerOnline = r?.player_online ?? null;
      ownedAugments = Array.isArray(r?.owned_augments) ? r.owned_augments : [];
    } catch (e) {
      rows = [];
      failed = true;
    } finally {
      fetchStatus = 'ready';
    }
  }

  // Jump back to the main Storage page with the item's container opened. The
  // bank grid is always visible on that page, so a bank row just navigates.
  function locate(row) {
    if (!row.isBank) selectContainer(row.containerId);
    goto(`${base}/storage`);
  }

  // `label` may be a curated string ("Scattergun Rampage-Enhancement") or a
  // raw template id ("T6_Augment_Scattergun6"); the latter is the only case
  // with underscores, so this is enough to make it readable without a table.
  function readable(raw) {
    const s = raw || 'Augment';
    return s.includes('_') ? s.replace(/_/g, ' ') : s;
  }
  function pct(roll) {
    return Math.round(Math.max(0, Math.min(1, Number(roll) || 0)) * 100);
  }
</script>

<div class="panel">
  {#if fetchStatus === 'loading' && rows.length === 0}
    <div class="skel-list">
      {#each Array(3) as _, i (i)}<div class="row-skel skeleton"></div>{/each}
    </div>
  {:else if failed}
    <!-- Never claim "you have none" when the read did not complete: that is the
         same false-negative the empty page told everyone for a day. -->
    <SealedPanel
      status="error"
      errorText="Your augmented gear could not be read just now. Nothing is wrong with your items; reload the page to try again."
    />
  {:else if rows.length === 0}
    <SealedPanel
      status="empty" action="none" art="no-augments"
      emptyText="No augmented gear found across your containers or bank. Augments are a rare end-game modification; most players will never see one drop."
    />
  {:else}
    <p class="count mono">{rows.length} augmented item{rows.length === 1 ? '' : 's'}</p>
    {#if truncated}
      <p class="empty">You own more containers than this page scans, so a few may be missing.</p>
    {/if}
    <ul class="rows" role="list">
      {#each rows as r (r.item.item_id)}
        <li class="row">
          <div class="row-head">
            <span class="item-icon-trigger">
              <ItemInfoPopover item={r.item} label="Item details: {r.item.name || r.item.template || 'Item'}">
                <img class="item-icon" src={iconUrl(r.item.icon)} alt="" aria-hidden="true" loading="lazy" />
              </ItemInfoPopover>
            </span>
            <div class="row-id">
              <span class="item-name" title={r.item.name}>{r.item.name || r.item.template || 'Item'}</span>
              <div class="row-loc">
                <button class="loc mono" type="button" onclick={() => locate(r)}>
                  {r.containerName}
                </button>
                {#if r.isDD}<span class="dd-badge">Deep Desert</span>{/if}
              </div>
            </div>
          </div>
          <ul class="augs" role="list">
            {#each r.augments as a, i (i)}
              {@const label = readable(a.label || a.name)}
              {@const rolls = Array.isArray(a.rolls) ? a.rolls : []}
              <li class="aug">
                <span class="glyph" aria-hidden="true">&#9670;</span>
                <span class="aug-label">{label}</span>
                {#if a.grade != null}<span class="grade mono" title="Augment grade {a.grade} of 5">G{a.grade}</span>{/if}
                <span class="rolls">
                  {#each groupRolls(rolls) as g, ri (ri)}
                    <span class="roll mono" class:perfect={g.value >= 1}
                          title="{g.count} roll{g.count === 1 ? '' : 's'} at {pct(g.value)}%">
                      {#if g.value >= 1}<span class="tag">Perfect</span>{/if}
                      <span class="num">{pct(g.value)}%</span>
                      {#if g.count > 1}<span class="mult">&times;{g.count}</span>{/if}
                    </span>
                  {/each}
                </span>
              </li>
            {/each}
          </ul>
          <!-- `can_augment` comes from the server and is true for PAWN-SIDE
               storage: the CHOAM bank and the character's own backpack. Both
               hang off the pawn actor and re-hydrate from the DB at login, and
               every augment write is offline-gated.
               A placed container or vehicle is held in its partition server's
               RAM while that partition is up, where a reroll's UPDATE is
               retracted and a swap's DELETE is resurrected (duplicating a rare
               augment), so the writer refuses those. Showing the button anyway
               would just move the refusal to after the player committed. -->
          {#if augmentsEnabled && r.augments.length > 0}
            {#if r.canAugment}
              <button class="aug-btn mono" type="button" onclick={() => (dialogItem = r.item)}>
                <span aria-hidden="true">&#9881;</span> Reroll or swap
              </button>
            {:else}
              <p class="aug-note">
                In a container, so it cannot be rerolled. Carry it on your character or
                put it in the CHOAM bank, then log out.
              </p>
            {/if}
          {/if}
        </li>
      {/each}
    </ul>
  {/if}
</div>

{#if dialogItem}
  <!-- Same dialog the Character page uses. It reads `item.augments`, so the row
       object passed here must be the ITEM (which carries them), not the wrapper. -->
  <AugmentActionDialog
    item={dialogItem}
    {ownedAugments}
    {playerOnline}
    onClose={() => { dialogItem = null; loadAugmented(fetchedSig); }}
  />
{/if}

<style>
  .panel { display: flex; flex-direction: column; gap: var(--space-3); }
  .empty { color: var(--text-muted); font-size: var(--text-sm); margin: var(--space-2) 0; max-width: 52ch; }
  .count { margin: 0; color: var(--text-muted); font-size: var(--text-xs); text-transform: uppercase; letter-spacing: .12em; }

  .skel-list { display: flex; flex-direction: column; gap: var(--space-2); }
  .row-skel { height: 84px; border-radius: var(--radius-sm); }

  .rows { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-3); }
  .row {
    display: flex; flex-direction: column; gap: var(--space-2); min-width: 0;
    padding: var(--space-3); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    background: var(--metal-0); box-shadow: inset 0 1px 0 var(--metal-hi);
  }
  .row-head { display: flex; gap: var(--space-2); align-items: center; min-width: 0; }
  /* The wrapper carries the fixed size (matches the old plain <img> exactly)
     so the ItemInfoPopover trigger inside it, sized 100%/100%, has a real box
     to fill; the img itself just fills that trigger. */
  .item-icon-trigger { display: block; width: 36px; height: 36px; flex: 0 0 auto; }
  .item-icon { width: 100%; height: 100%; object-fit: contain; opacity: .92; }
  .row-id { min-width: 0; flex: 1; display: flex; flex-direction: column; gap: 2px; }
  .item-name { font-size: var(--text-sm); font-weight: 600; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .row-loc { display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; }
  .loc {
    font-size: var(--text-xs); color: var(--text-muted); background: transparent;
    border: 1px solid var(--edge); border-radius: var(--radius-sm); padding: 1px var(--space-2);
    cursor: pointer; transition: border-color var(--motion-fast) var(--ease-out), color var(--motion-fast) var(--ease-out);
  }
  .loc:hover { border-color: var(--accent); color: var(--accent-text); }
  .dd-badge { font-size: var(--text-xs); color: var(--ls-orange); border: 1px solid color-mix(in srgb, var(--ls-orange) 45%, var(--edge)); border-radius: 3px; padding: 0 5px; }

  .augs { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 3px; }
  .aug {
    display: flex; align-items: center; gap: var(--space-1); flex-wrap: wrap; min-width: 0;
    background: color-mix(in srgb, var(--accent) 7%, var(--metal-0));
    border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: 3px var(--space-2);
  }
  /* Matches EquippedList's reroll affordance exactly: the same action on the
     same kind of item should not look like two different features. */
  .aug-note {
    align-self: flex-start; margin: var(--space-1) 0 0;
    font-size: var(--text-xs); color: var(--text-muted); max-width: 46ch;
  }
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

  .glyph { color: var(--accent); font-size: 9px; flex: 0 0 auto; }
  /* Does NOT grow - see the same note in AugmentChips.svelte: growing stranded
     the roll chips against the right edge of a wide row. */
  .aug-label { font-size: var(--text-xs); color: var(--text); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 0 1 auto; }
  .grade { font-size: var(--text-xs); color: var(--accent-text); flex: 0 0 auto; }
  /* Shrinkable on purpose - see the same note in AugmentChips.svelte: pinned
     at max-content, a 6-7 roll augment overflows the row instead of wrapping. */
  .rolls { display: flex; gap: 3px; flex-wrap: wrap; flex: 0 1 auto; }
  .roll {
    display: inline-flex; align-items: center; gap: 3px; white-space: nowrap;
    font-size: var(--text-xs); color: var(--text-muted);
    background: var(--metal-1); border: 1px solid var(--edge);
    border-radius: 2px; padding: 0 4px; line-height: 1.5;
  }
  /* Perfect (1.0) roll: never hue-only - the fill colour is always paired
     with the "Perfect" text tag and the 100% numeral. */
  .roll.perfect { color: var(--bg-deep); background: var(--ls-melange); border-color: var(--ls-melange); font-weight: 700; }
  .roll .tag { text-transform: uppercase; letter-spacing: .04em; }
  .roll .mult { opacity: .75; font-weight: 400; }
  .roll.perfect .mult { opacity: .65; }

  @media (max-width: 520px) {
    .row-head { align-items: flex-start; }
  }
</style>
