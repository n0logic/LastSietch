<script>
  // Storage: the Carved-Sietch 3-panel surface. LEFT = CHOAM bank (Solari + coin
  // transfers + the inv30 bank grid + repair). MIDDLE = owned-container rail (PNG
  // tiles, lazy GLB hero on the selected tile). RIGHT = the selected container grid
  // with a cross-container locator pinned above. Drag an item onto a container tile
  // or the other grid to MOVE it (optimistic + fail-closed, offline-gated).
  //
  // Every write is CSRF + uuid-idempotent and fails closed. Reads are never gated,
  // so a signed-in player can always browse; the write controls lock behind the
  // offline gate. Signed-out seals to an honest Connect-Discord panel.
  import { untrack } from 'svelte';
  import { base } from '$app/paths';
  import { useAuthGate } from '$lib/authGate.svelte.js';
  import { storage, loadAll, selectedContainer } from '$lib/storage.svelte.js';
  import { getPref, setPref } from '$lib/prefs.svelte.js';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import Notice from '$lib/components/Notice.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import ChoamBankCard from '$lib/components/storage/ChoamBankCard.svelte';
  import ItemGrid from '$lib/components/storage/ItemGrid.svelte';
  import ContainerRail from '$lib/components/storage/ContainerRail.svelte';
  import ItemLocator from '$lib/components/storage/ItemLocator.svelte';
  import RepairControls from '$lib/components/storage/RepairControls.svelte';
  import VehicleParts from '$lib/components/storage/VehicleParts.svelte';
  import OnlineGateBanner from '$lib/components/storage/OnlineGateBanner.svelte';

  const gate = useAuthGate();

  let sel = $derived(selectedContainer());
  // A vehicle with no cargo module (Scout/Carrier ornithopter, Sandbike): show its
  // installed-parts panel only, no cargo grid.
  //
  // Capacity reaches V2 as `mic`/`miv` — `_v2_container()` renames max_item_count /
  // max_item_volume on the way out. Reading `max_item_count` here left it undefined for
  // EVERY container, so every vehicle claimed "no cargo storage" and its cargo grid was
  // replaced by the parts panel (a Cargo Container reading 41/150 in the rail still said
  // "no storage"). Semantics per the read path:
  //   mic: >0 slot cap | -1 unlimited | 0 volume-gated only | null unknown (old snapshot)
  //   miv: volume cap
  // item_count is the fail-safe term: never hide items we can already see exist.
  let selMic = $derived(Number(sel?.mic ?? 0) || 0);
  let selMiv = $derived(Number(sel?.miv ?? 0) || 0);
  let hasStorage = $derived(
    selMic > 0 || selMic === -1 || selMiv > 0 || (Number(sel?.item_count) || 0) > 0
  );
  let noStorage = $derived(!!sel?.is_vehicle && !hasStorage);
  let bankInvId = $derived(storage.bank?.inv_id);
  let bankWritable = $derived(bankInvId != null && storage.backpack?.inv_id != null);
  let selStorageKind = $derived(sel?.is_bank ? 'bank' : (sel?.is_backpack ? 'backpack' : ''));
  let selWritable = $derived(!!sel?.is_pawn_storage && !!selStorageKind);

  // Grid / List is an ACCOUNT preference, not a device one: read through getPref so
  // a change made on the Settings page lands here without a reload, and write at
  // identity scope so it follows the player to any browser or character.
  let storageView = $derived(getPref('storage_view') === 'list' ? 'list' : 'grid');

  let loaded = false;
  $effect(() => {
    const status = gate.status;
    if (status === 'authed' && !loaded) { loaded = true; untrack(loadAll); }
    else if (status === 'anon') { loaded = false; }
  });
</script>

<svelte:head>
  <title>Storage | Last Sietch</title>
</svelte:head>

<div class="page">
  <PageHeader
    kicker="Last Sietch | CHOAM vault"
    title="Storage"
    sub="Your Solari on account, every container you own, and the gear inside them. Bank your coin, find a buried item, and reorganise your stores when you are logged out."
  >
    <div class="head-controls">
      <a class="submenu-link mono" href="{base}/storage/workshop">
        <span class="glyph" aria-hidden="true">&#9670;</span> Workshop
      </a>
      {#if !gate.loading && !gate.anon}
        <div class="view-toggle" role="group" aria-label="Item view">
          <button
            class="view-btn mono"
            class:on={storageView === 'grid'}
            aria-pressed={storageView === 'grid'}
            onclick={() => setPref('storage_view', 'grid')}
          >Grid</button>
          <button
            class="view-btn mono"
            class:on={storageView === 'list'}
            aria-pressed={storageView === 'list'}
            onclick={() => setPref('storage_view', 'list')}
          >List</button>
        </div>
      {/if}
    </div>
  </PageHeader>

  {#if gate.loading}
    <p class="skeleton">loading</p>
  {:else if gate.anon}
    <SealedPanel
      status="empty" action="login"
      emptyText="Sign in to see your CHOAM balance, containers, and stored gear."
    />
  {:else if storage.status === 'error'}
    <SealedPanel
      status="error"
      errorText="Your storage could not be read right now. Try again in a moment."
    />
  {:else}
    <OnlineGateBanner />
    <!-- Write-outcome line. Keyed on noticeSeq so a repeated identical message
         (move, move, move) re-announces and replays the flash instead of sitting
         there looking like the previous one never cleared. -->
    <div class="notice-slot">
      {#if storage.notice}
        {#key storage.noticeSeq}
          <Notice tone={storage.noticeTone} text={storage.notice} />
        {/key}
      {/if}
    </div>

    <div class="grid">
      <!-- LEFT: CHOAM bank -->
      <div class="col left">
        <CarvedSlab>
          <ChoamBankCard />
        </CarvedSlab>
        <CarvedSlab sharp={true}>
          <p class="panel-kicker mono">Bank &middot; offline transfer</p>
          <!-- Exchange listing sources follow the writer. Two inventories re-hydrate
               from the DB at login and are therefore covered by the offline gate: the
               CHOAM bank (inv_type 30) and the character's own backpack. Base containers
               and vehicles sit in the partition's RAM and stay refused by the writer's
               stop-ship source gate, whoever asks. The bank lane has been open since
               June, so this grid is unconditional; the backpack lane is newer and rides
               storage.marketBackpackEnabled on the right-hand grid. -->
          <ItemGrid
            items={storage.bank?.items ?? []}
            mic={storage.bank?.mic ?? 0}
            miv={storage.bank?.miv ?? 0}
            status={storage.status === 'loading' ? 'loading' : 'ready'}
            panel="bank"
            containerId={bankInvId}
            draggableItems={bankWritable}
            storageKind="bank"
            writable={bankWritable}
            allowMarketSell={true}
            view={storageView}
          />
        </CarvedSlab>
        <CarvedSlab>
          <RepairControls />
        </CarvedSlab>
      </div>

      <!-- MIDDLE: container rail -->
      <div class="col mid">
        <CarvedSlab>
          <ContainerRail
            containers={storage.containers}
            selectedId={storage.selectedId}
            status={storage.status === 'loading' ? 'loading' : 'ready'}
          />
        </CarvedSlab>
      </div>

      <!-- RIGHT: selected container grid + locator -->
      <div class="col right">
        <CarvedSlab sharp={true}>
          <ItemLocator />
        </CarvedSlab>
        <CarvedSlab sharp={true}>
          <p class="panel-kicker mono">
            <!-- `name` is the player's CUSTOM name and is EMPTY for the bank + every
                 vehicle (dune-containers.py emits '' AS name for those branches; only
                 placeables carry permission_actor.actor_name). Falling straight through
                 to the "nothing selected" copy made a selected Ornithopter read
                 "NO CONTAINER SELECTED - 12 ITEMS". Fall back to the type label, same
                 as the container rail does. -->
            {sel?.name || sel?.type || 'No container selected'}
            {#if sel}<span class="sub-count">&middot; {noStorage ? 'no storage' : `${(sel.item_count ?? 0)} item${(sel.item_count ?? 0) === 1 ? '' : 's'}`}</span>{/if}
            {#if sel}<span class="access" class:writable={selWritable}>{selWritable ? 'offline transfer' : 'read-only'}</span>{/if}
          </p>
          {#if storage.selectedId == null}
            <SealedPanel
              status="empty" action="none" art="empty-vault"
              emptyText="Pick a container from the rail to see what is inside."
            />
          {:else if noStorage}
            <SealedPanel
              status="empty" action="none"
              emptyText="This vehicle has no cargo storage. Its installed parts and durability are shown below."
            />
          {:else}
            <ItemGrid
              items={storage.items}
              mic={storage.itemsMic}
              miv={storage.itemsMiv}
              status={storage.itemsStatus}
              panel="container"
              containerId={storage.selectedId}
              draggableItems={selWritable}
              storageKind={selStorageKind}
              writable={selWritable}
              allowMarketSell={selStorageKind === 'backpack' && storage.marketBackpackEnabled}
              view={storageView}
            />
          {/if}
        </CarvedSlab>
        {#if sel?.is_vehicle}
          <CarvedSlab sharp={true}>
            <VehicleParts containerId={storage.selectedId} />
          </CarvedSlab>
        {/if}
      </div>
    </div>
  {/if}
</div>

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }
  /* Submenu entry into the Workshop bench services. Understated pill, not a
     filled button - it is a secondary nav link, not a call to action. */
  /* The header's trailing controls row: the Workshop link and the view switch. */
  .head-controls { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-3); margin-top: var(--space-3); }
  .submenu-link {
    display: inline-flex; align-items: center; gap: var(--space-1);
    font-size: var(--text-xs); text-transform: uppercase; letter-spacing: .1em;
    color: var(--text-muted); text-decoration: none;
    border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-1) var(--space-3);
    transition: border-color var(--motion-fast) var(--ease-out), color var(--motion-fast) var(--ease-out);
  }
  .submenu-link .glyph { color: var(--accent); font-size: 9px; }
  .submenu-link:hover { border-color: var(--accent); color: var(--accent-text); }

  .view-toggle {
    display: inline-flex; border: 1px solid var(--edge); border-radius: var(--radius-sm);
    overflow: hidden; background: var(--metal-0);
  }
  .view-btn {
    background: transparent; border: 0; cursor: pointer; color: var(--text-muted);
    padding: var(--space-1) var(--space-3);
    font-size: var(--text-xs); text-transform: uppercase; letter-spacing: .1em;
    transition: color var(--motion-fast) var(--ease-out), background var(--motion-fast) var(--ease-out);
  }
  .view-btn + .view-btn { border-left: 1px solid var(--edge); }
  .view-btn:hover { color: var(--text); }
  .view-btn.on { color: var(--accent-bright); background: color-mix(in srgb, var(--accent) 12%, transparent); }

  /* Reserve the row so an arriving notice never shoves the panels down. */
  .notice-slot { min-height: 1.9rem; margin-top: var(--space-3); }
  .grid {
    margin-top: var(--space-4);
    display: grid; gap: var(--space-4);
    grid-template-columns: minmax(0, 1fr) minmax(0, 0.9fr) minmax(0, 1.2fr);
    align-items: start;
  }
  .col { display: flex; flex-direction: column; gap: var(--space-4); min-width: 0; }

  .panel-kicker { margin: 0 0 var(--space-3); color: var(--accent); text-transform: uppercase; letter-spacing: .18em; font-size: var(--text-xs); }
  .sub-count { color: var(--text-muted); letter-spacing: normal; }
  .access {
    display: inline-flex; margin-left: var(--space-2); padding: 1px 5px;
    border: 1px solid var(--edge); border-radius: 3px; color: var(--text-muted);
    font-size: var(--text-xs); letter-spacing: .05em;
  }
  .access.writable { color: var(--accent-text); border-color: color-mix(in srgb, var(--accent) 48%, var(--edge)); }

  /* Stagger-rise the panels on enter (opacity/transform only). */
  .col > :global(*) { opacity: 0; transform: translateY(14px); animation: rise .5s var(--ease-out) forwards; }
  .col.left > :global(*:nth-child(1)) { animation-delay: .04s; }
  .col.left > :global(*:nth-child(2)) { animation-delay: .10s; }
  .col.left > :global(*:nth-child(3)) { animation-delay: .16s; }
  .col.mid > :global(*) { animation-delay: .08s; }
  .col.right > :global(*:nth-child(1)) { animation-delay: .12s; }
  .col.right > :global(*:nth-child(2)) { animation-delay: .18s; }
  @keyframes rise { to { opacity: 1; transform: none; } }

  @media (max-width: 900px) {
    .grid { grid-template-columns: 1fr; }
  }
  @media (prefers-reduced-motion: reduce) {
    .col > :global(*) { opacity: 1; transform: none; animation: none; }
  }
</style>
