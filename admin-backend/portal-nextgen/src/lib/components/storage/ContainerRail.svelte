<script>
  // Middle panel: the owned-container list. PNG icon tiles by default; the SELECTED
  // container additionally gets a lazy three.js GLB hero (ContainerHero self-gates
  // on tier/webgl2/reduced-motion and renders nothing when the gate misses, so the
  // PNG list stays authoritative). Clicking a tile selects it and loads its grid.
  import ContainerTile from './ContainerTile.svelte';
  import ContainerHero from './ContainerHero.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import { selectContainer } from '$lib/storage.svelte.js';

  let { containers = [], selectedId, status = 'ready' } = $props();

  const sameId = (a, b) => a != null && b != null && String(a) === String(b);
  let selected = $derived(containers.find((c) => sameId(c.id, selectedId)) || null);
  // Backend may supply a per-container `glb`; else fall back to the medium CHOAM
  // storage container hero so the selected tile always has a 3D form on high tier.
  let heroGlb = $derived(
    selected?.glb || '/glb/pieces/SM_Env_Prop_Choam_StorageContainer_Medium_web.glb'
  );

  function onSelect(id) { selectContainer(id); }
</script>

<div class="rail">
  <div class="rail-head">
    <p class="kicker mono">Storage</p>
    <p class="scope">Bank and Backpack transfer while offline. World storage stays read-only.</p>
  </div>

  {#if selected}
    <ContainerHero glb={heroGlb} name={selected.name || ''} />
  {/if}

  {#if status === 'loading'}
    <div class="list">
      {#each Array(4) as _, i (i)}<div class="tile-skel skeleton"></div>{/each}
    </div>
  {:else if status === 'error'}
    <SealedPanel status="error" errorText="Could not read your containers." />
  {:else if containers.length === 0}
    <SealedPanel
      status="empty" action="none" art="empty-vault"
      emptyText="You own no storage containers yet."
    />
  {:else}
    <div class="list">
      {#each containers as c (c.id)}
        <ContainerTile container={c} selected={sameId(c.id, selectedId)} {onSelect} />
      {/each}
    </div>
  {/if}
</div>

<style>
  .rail { display: flex; flex-direction: column; gap: var(--space-3); }
  .rail-head { display: flex; flex-direction: column; gap: var(--space-1); }
  .kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .24em; font-size: var(--text-xs); }
  .scope { margin: 0; color: var(--text-muted); font-size: var(--text-xs); line-height: 1.45; }
  /* Two tiles per row wherever the rail has room for them (the rail itself
     narrows to ~120px/tile inside the 3-column desktop layout, and widens to
     ~150px+/tile once the page collapses to one column on tablet/phone).
     Fixed at 2 columns rather than auto-fill so a wide single-column page
     never balloons into a row of undersized tiles - only a genuinely narrow
     phone drops to one column. */
  .list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-2); }
  .tile-skel { height: 108px; border-radius: var(--radius-sm); }
  @media (max-width: 400px) {
    .list { grid-template-columns: 1fr; }
  }
</style>
