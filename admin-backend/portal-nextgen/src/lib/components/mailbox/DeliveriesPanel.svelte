<script>
  // The full list of packages the server sent this player: the Welcome Package
  // on a new account, the Return Package after a long time away. One slab per
  // package, its legs as rows (where each part of it went), and recorded items
  // grouped by destination. Contents start open and can be collapsed.
  //
  // The anchor id is load-bearing: the Home card links to `/mailbox#deliveries`,
  // and this section is mounted above the mailbox itself, so an arrival from
  // that link scrolls straight onto the package rather than the inbox.
  //
  // EVERY SEAL LIVES HERE. The Mailbox page keeps exactly one login gate of its
  // own; a second one inside this component would ask a player who is already
  // signed in to sign in again, so none of these branches carry an action.
  //
  // A leg is never re-decided on the client. The server says delivered, waiting
  // or pending and this renders the word; the exchange leg in particular stays
  // waiting forever by design, because nothing watches the terminal a player
  // takes the item from.
  import { onMount } from 'svelte';
  import { deliveries, loadDeliveries } from '$lib/deliveries.svelte.js';
  import { iconUrl } from '$lib/icons.js';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';

  // A welcome package can carry more lines than anybody reads in a list. The
  // rest are counted rather than dropped silently.
  const ITEM_CAP = 40;

  // Section 1c's package states. The card on Home carries the same three keys.
  const STATE_WORD = {
    delivered: 'delivered',
    partial: 'partly delivered',
    pending: 'not landed yet',
  };

  // Section 1c's leg states.
  const LEG_WORD = {
    delivered: 'delivered',
    waiting: 'waiting for you',
    pending: 'not landed yet',
    unknown: 'not recorded',
  };

  // Where a thing actually is, in the wording the public site uses. This is the
  // sentence players act on, so it names the tab and the button.
  const WHERE_TEXT = {
    backpack: 'your backpack',
    bank: 'your CHOAM bank',
    exchange: 'any tradepost CHOAM Exchange terminal under the Completed tab (it shows as CANCELED, press Take item)',
    unknown: 'not recorded',
  };

  const WHERE_TITLE = {
    backpack: 'Your backpack',
    bank: 'Your CHOAM bank',
    exchange: 'CHOAM Exchange',
    unknown: 'Destination not recorded',
  };

  function stateWord(s) {
    return STATE_WORD[s] || 'state unknown';
  }

  function legWord(s) {
    return LEG_WORD[s] || LEG_WORD.unknown;
  }

  function whereText(w) {
    return WHERE_TEXT[w] || WHERE_TEXT.unknown;
  }

  // Every item carries its own state, and it does NOT always agree with where
  // the line says it went: a db_owed line and a failed one are both listed
  // against a place they have not reached yet. Only `delivered` stays quiet,
  // because for a delivered item the place IS the whole answer.

  function when(t) {
    const d = new Date(t);
    return Number.isNaN(d.getTime()) ? String(t ?? '') : d.toLocaleString();
  }

  // A cooldown that has already run out is not a date to promise anybody. The
  // server stops sending those, and a panel that printed one would tell a player
  // to wait for a day that has been and gone.
  function future(iso) {
    const t = new Date(iso).getTime();
    return Number.isFinite(t) && t > Date.now();
  }

  function legs(p) {
    return Array.isArray(p?.legs) ? p.legs : [];
  }

  function items(p) {
    return Array.isArray(p?.items) ? p.items : [];
  }

  function shown(p) {
    return items(p).slice(0, ITEM_CAP);
  }

  function itemGroups(p) {
    const groups = Object.fromEntries(Object.keys(WHERE_TEXT).map((where) => [where, []]));
    for (const it of shown(p)) {
      const where = Object.hasOwn(groups, it?.where) ? it.where : 'unknown';
      groups[where].push(it);
    }
    return Object.entries(groups).filter(([, rows]) => rows.length > 0)
      .map(([where, rows]) => ({ where, items: rows }));
  }

  function overflow(p) {
    const n = items(p).length - ITEM_CAP;
    if (n > 0) return n;
    return null;
  }

  function quantity(it) {
    const q = it?.quantity;
    if (typeof q === 'number' && Number.isFinite(q)) return q;
    return null;
  }

  // The logout line speaks for PENDING legs only. A waiting leg (the exchange
  // one) is not waiting on a logout: the item sits on a terminal until the
  // player presses Take item, and it carries its own note saying so. Under a
  // package that is fully delivered the line is simply not true, so it does not
  // render at all.
  let anyPending = $derived(
    deliveries.packages.some((p) => legs(p).some((l) => l?.state === 'pending'))
  );

  let anchor;

  onMount(() => {
    loadDeliveries();
    // The Home CTA lands on this hash. SvelteKit has already restored scroll by
    // the time the panel mounts, and the section is one of several on the page,
    // so the jump is made here rather than left to the browser's anchor pass.
    if (typeof location !== 'undefined' && location.hash === '#deliveries') {
      anchor?.scrollIntoView({ block: 'start' });
    }
  });
</script>

<section id="deliveries" class="deliveries" bind:this={anchor}>
  <p class="kicker mono">Deliveries | what the server sent you</p>

  {#if deliveries.status === 'idle' || deliveries.status === 'loading'}
    <SealedPanel slab={true} status="loading" action="none" loadingText="reading your deliveries" />
  {:else if deliveries.status === 'error'}
    <SealedPanel
      slab={true} status="error" action="none"
      errorText="Your deliveries could not be read right now. Try again shortly."
    />
  {:else if deliveries.available === false}
    <SealedPanel
      slab={true} status="empty" action="none" art="no-mail"
      emptyText="The game host is not answering, so what was sent to you cannot be read right now. Nothing has been lost; try again shortly."
    />
  {:else if deliveries.status === 'empty'}
    <SealedPanel
      slab={true} status="empty" action="none" art="no-mail"
      emptyText="Nothing has been sent to you yet. The welcome package is sent after you first join the game, and a return package waits for you after 28 days away."
    />
  {:else}
    <div class="stack">
      {#each deliveries.packages as p, i (`${p?.kind ?? 'pack'}-${p?.granted_at ?? i}`)}
        <CarvedSlab elevation={2}>
          <div class="phead">
            <b class="label">{p?.label || 'Package'}</b>
            <span class="date mono">{when(p?.granted_at)}</span>
            <span class="chip mono" data-state={p?.state}>{stateWord(p?.state)}</span>
          </div>

          {#if legs(p).length > 0}
            <ul class="legs" role="list">
              {#each legs(p) as leg, li (leg?.key ?? li)}
                <li class="leg">
                  <span class="leg-label">{leg?.label || 'Part of the package'}</span>
                  <span class="leg-state mono" data-state={leg?.state}>{legWord(leg?.state)}</span>
                  {#if leg?.note}<small class="note">{leg.note}</small>{/if}
                </li>
              {/each}
            </ul>
          {/if}

          {#if items(p).length > 0}
            <details class="items" open>
              <summary>What was in it</summary>
              <p class="receipt-note">Recorded package contents, not your current inventory.</p>
              {#each itemGroups(p) as group (group.where)}
                <div class="item-group" data-destination={group.where}>
                  <h3>{WHERE_TITLE[group.where]}</h3>
                  {#if group.where === 'exchange'}<p class="destination-note">Collect waiting items at {whereText(group.where)}.</p>{/if}
                  <ul class="itemlist" role="list">
                    {#each group.items as it, ii (`${it?.name ?? 'item'}-${ii}`)}
                      <li class="item">
                        <img class="icon" src={iconUrl(it?.icon)} alt="" aria-hidden="true" loading="lazy" />
                        <span class="name">{it?.name || 'Unnamed item'}{#if quantity(it) === null}<small class="quantity-note">Quantity not recorded</small>{/if}</span>
                        {#if quantity(it) !== null}<span class="qty mono">x{quantity(it).toLocaleString()}</span>{/if}
                        <span class="item-state mono" data-state={it?.state}>
                          {#if it?.state !== 'delivered'}{legWord(it?.state)}{/if}
                        </span>
                      </li>
                    {/each}
                  </ul>
                </div>
              {/each}
              {#if overflow(p) !== null}
                <p class="more">and {overflow(p)} more</p>
              {/if}
            </details>
          {:else}
            <p class="items-unavailable">Item names and quantities are not available in this delivery record.</p>
          {/if}
        </CarvedSlab>
      {/each}

      {#if deliveries.skip}
        <CarvedSlab elevation={2}>
          <p class="kicker mono">Fresh start</p>
          <p class="skip">
            You started over inside the 30 day identity cooldown, so no second welcome
            package was sent.
            {#if future(deliveries.skip.eligible_at)}
              You are eligible for one again on {when(deliveries.skip.eligible_at)}.
            {/if}
          </p>
        </CarvedSlab>
      {/if}

      {#if anyPending}
        <p class="foot">Parts marked pending land after your next logout.</p>
      {/if}
    </div>
  {/if}
</section>

<style>
  .deliveries { display: block; scroll-margin-top: var(--space-6); }
  .kicker { margin: 0 0 var(--space-3); color: var(--accent); text-transform: uppercase; letter-spacing: .24em; font-size: var(--text-xs); }
  .stack { display: flex; flex-direction: column; gap: var(--space-4); }

  .phead {
    display: flex; align-items: baseline; gap: var(--space-3); flex-wrap: wrap;
    padding-bottom: var(--space-2); border-bottom: 1px solid var(--border-subtle);
  }
  .label { font-size: var(--text-base); font-weight: 600; color: var(--text); }
  .date { font-size: var(--text-xs); color: var(--text-muted); }
  .chip {
    margin-left: auto; font-size: var(--text-xs); text-transform: uppercase;
    letter-spacing: .12em; color: var(--text-muted); white-space: nowrap;
  }
  .chip[data-state='delivered'] { color: var(--ls-green); }
  .chip[data-state='partial'] { color: var(--accent-text); }

  .legs { list-style: none; margin: var(--space-3) 0 0; padding: 0; display: flex; flex-direction: column; }
  .leg {
    display: grid; grid-template-columns: minmax(0, 1fr) 9rem;
    gap: var(--space-2) var(--space-3); align-items: baseline;
    padding: var(--space-2) 0; border-top: 1px solid var(--border-subtle);
    font-size: var(--text-sm);
  }
  .leg:first-child { border-top: 0; }
  .leg-label { color: var(--text); }
  .leg-state {
    font-size: var(--text-xs); text-transform: uppercase; letter-spacing: .1em;
    color: var(--text-muted); text-align: right;
  }
  .leg-state[data-state='delivered'] { color: var(--ls-green); }
  .note { grid-column: 1 / -1; font-size: var(--text-xs); color: var(--text-muted); line-height: 1.45; }

  .items { margin-top: var(--space-3); }
  .items summary {
    cursor: pointer; font-size: var(--text-sm); font-weight: 500; color: var(--accent-text);
  }
  .items summary:focus-visible { outline: 2px solid var(--accent-text); outline-offset: 4px; }
  .receipt-note, .destination-note, .items-unavailable { font-size: var(--text-xs); color: var(--text-muted); line-height: 1.5; margin: var(--space-2) 0; }
  .items-unavailable { border-top: 1px solid var(--border-subtle); padding-top: var(--space-3); margin-top: var(--space-3); }
  .item-group { margin-top: var(--space-4); min-width: 0; }
  .item-group h3 { font-size: var(--text-sm); font-weight: 500; color: var(--text); margin: 0; }
  .itemlist { list-style: none; margin: var(--space-2) 0 0; padding: 0; display: flex; flex-direction: column; }
  .item {
    display: grid; grid-template-columns: 22px minmax(0, 1fr) 5rem 9rem;
    gap: var(--space-2) var(--space-3); align-items: center;
    padding: var(--space-1) 0; border-top: 1px solid var(--border-subtle);
    font-size: var(--text-sm);
  }
  .item:first-child { border-top: 0; }
  .icon { width: 22px; height: 22px; object-fit: contain; }
  .name { color: var(--text); overflow-wrap: anywhere; line-height: 1.45; }
  .qty { font-size: var(--text-xs); color: var(--accent-text); font-variant-numeric: tabular-nums; text-align: right; }
  .quantity-note { display: block; font-size: var(--text-xs); color: var(--text-muted); }
  /* Same word list and the same chrome as the leg rows: one vocabulary for the
     whole panel. Waiting is the only one that asks the player to do something,
     so it is the only one that takes the accent. */
  .item-state {
    grid-column: 4;
    font-size: var(--text-xs); text-transform: uppercase; letter-spacing: .1em;
    color: var(--text-muted); text-align: right; white-space: nowrap;
  }
  .item-state[data-state='waiting'] { color: var(--accent-text); }
  .more { margin: var(--space-2) 0 0; font-size: var(--text-xs); color: var(--text-muted); }

  .skip { margin: 0; font-size: var(--text-sm); color: var(--text-muted); line-height: 1.5; }
  .foot { margin: 0; font-size: var(--text-xs); color: var(--text-muted); }

  @media (max-width: 640px) {
    .leg { grid-template-columns: minmax(0, 1fr); }
    .leg-state { text-align: left; }
    .item { grid-template-columns: 22px minmax(0, 1fr) 3.5rem; }
    .item-state { grid-column: 2 / -1; text-align: left; }
  }
</style>
