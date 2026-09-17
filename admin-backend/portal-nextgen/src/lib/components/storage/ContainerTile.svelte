<script>
  // One owned-container tile in the middle rail. PNG dune-icon by default (the GLB
  // hero renders only for the SELECTED tile, in ContainerRail). Shows name/type, a
  // Hagga/DD location badge, an item-count/slot caption, a capacity fill bar, and a
  // "cached" marker when the row is a mirror snapshot. A fresh (non-cached) read
  // earns a LiveDot. The tile is a MOVE drop target: dropping an item relocates it
  // into this container (server picks first-empty).
  import LiveDot from '$lib/components/LiveDot.svelte';
  import { storage, moveItem } from '$lib/storage.svelte.js';
  import { containerIconUrl, CONTAINER_FALLBACK } from '$lib/icons.js';

  let { container, selected = false, onSelect } = $props();

  let isDD = $derived(container?.is_deep_desert === true || /deep\s*desert/i.test(container?.location || ''));
  let storageKind = $derived(container?.is_bank ? 'bank' : (container?.is_backpack ? 'backpack' : ''));
  let isPawnStorage = $derived(container?.is_pawn_storage === true && !!storageKind);
  let mic = $derived(Number(container?.mic) || 0);
  let count = $derived(Number(container?.item_count) || 0);
  let fillFrac = $derived(mic > 0 ? Math.max(0, Math.min(1, count / mic)) : 0);
  // container.icon is a container_icons basename served from /admin/static/img/
  // containers/ (NOT the dune-icons library); resolve it there. A null basename or
  // a 404 (see onerror) falls back to the generic storage-container glyph.
  let icon = $derived(containerIconUrl(container?.icon));
  let dragOver = $state(false);

  // The two-per-row grid tile is too narrow to spell out name AND type when
  // they say the same thing (a nameless container falls back to its type, see
  // `displayName` below) - only show the type caption when it adds information.
  let displayName = $derived(container?.name || container?.type || 'Container');
  let showType = $derived(!!(container?.name && container?.type && container.name !== container.type));

  function canAccept(e) {
    return isPawnStorage && storage.pawnMoveEnabled && storage.offlineOk && !isDD &&
      e.dataTransfer && e.dataTransfer.types &&
      Array.from(e.dataTransfer.types).includes('application/x-ls-item');
  }
  function onDragOver(e) { if (!canAccept(e)) return; e.preventDefault(); e.dataTransfer.dropEffect = 'move'; dragOver = true; }
  function onDragLeave() { dragOver = false; }
  async function onDrop(e) {
    dragOver = false;
    if (isDD || !isPawnStorage) return;
    const raw = e.dataTransfer.getData('application/x-ls-item') || e.dataTransfer.getData('text/plain');
    if (!raw) return;
    e.preventDefault();
    let p; try { p = JSON.parse(raw); } catch { return; }
    if (!p || p.src_container_id === container.id || p.src_storage === storageKind) return;
    await moveItem(p.item_id, container.id, p.src_container_id, p.template);
  }
</script>

<button
  class="tile"
  class:selected
  class:pawn-storage={isPawnStorage}
  class:drag-over={dragOver}
  type="button"
  aria-pressed={selected}
  onclick={() => onSelect?.(container.id)}
  ondragover={onDragOver}
  ondragleave={onDragLeave}
  ondrop={onDrop}
>
  <div class="head">
    <img class="icon" src={icon} alt="" aria-hidden="true" loading="lazy"
         onerror={(e) => { if (e.target.src !== location.origin + CONTAINER_FALLBACK) e.target.src = CONTAINER_FALLBACK; }} />
    <!-- LiveDot only on an explicitly-fresh (non-cached) read; unknown freshness
         (cached === null) shows no dot, honoring the Ibad live-data reservation. -->
    {#if container?.cached === false}<LiveDot tone="live" />{/if}
  </div>
  <div class="body">
    <!-- Two-line clamp: at the narrow grid width a full-width single-line
         ellipsis loses too much of a long name ("Ranked Gear and Augme...").
         The full name is still reachable via the title tooltip. -->
    <span class="name" title={container?.name}>{displayName}</span>
    {#if showType}<span class="type">{container?.type}</span>{/if}
    <div class="meta">
      {#if isPawnStorage}
        <span class="badge writable">Offline transfer</span>
      {:else}
        <span class="badge" class:dd={isDD}>{isDD ? 'Deep Desert' : 'Read-only'}</span>
      {/if}
      {#if container?.cached}<span class="cached mono">cached</span>{/if}
    </div>
    <div class="cap">
      {#if mic > 0}
        <div class="bar" role="meter" aria-valuenow={count} aria-valuemin="0" aria-valuemax={mic} aria-label="Capacity">
          <span class="fill" style="width:{Math.round(fillFrac * 100)}%"></span>
        </div>
        <span class="cap-txt mono">{count}/{mic}</span>
      {:else}
        <span class="cap-txt mono">{count} item{count === 1 ? '' : 's'}{mic === -1 ? ' (no limit)' : ''}</span>
      {/if}
    </div>
  </div>
</button>

<style>
  .tile {
    display: flex; flex-direction: column; gap: 3px; text-align: left; width: 100%; min-width: 0;
    padding: var(--space-2); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    background: var(--metal-0); box-shadow: inset 0 1px 0 var(--metal-hi); cursor: pointer;
    transition: border-color var(--motion-fast) var(--ease-out), background var(--motion-fast) var(--ease-out), transform var(--motion-fast) var(--ease-out);
  }
  .tile:hover { border-color: var(--edge-hi); }
  .tile.selected { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 9%, var(--metal-0)); }
  .tile.pawn-storage:not(.selected) { border-color: color-mix(in srgb, var(--accent) 32%, var(--edge)); }
  .tile.drag-over { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 14%, transparent); transform: translateY(-1px); }
  .head { display: flex; align-items: center; justify-content: space-between; }
  .icon { width: 28px; height: 28px; object-fit: contain; flex: 0 0 auto; opacity: .92; }
  .body { min-width: 0; display: flex; flex-direction: column; gap: 3px; }
  /* Reserve two lines so a short name and a long name still align their
     meta/capacity rows across a row of tiles, instead of a jagged grid. */
  .name {
    font-size: var(--text-xs); font-weight: 600; color: var(--text); line-height: 1.25;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
    min-height: calc(1.25em * 2); max-height: calc(1.25em * 2); overflow: hidden; word-break: break-word;
  }
  .type { font-size: var(--text-xs); color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .meta { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
  .badge { font-size: var(--text-xs); padding: 0 4px; border-radius: 3px; border: 1px solid var(--edge); color: var(--text-muted); }
  .badge.dd { color: var(--ls-orange); border-color: color-mix(in srgb, var(--ls-orange) 45%, var(--edge)); }
  .badge.writable { color: var(--accent-text); border-color: color-mix(in srgb, var(--accent) 48%, var(--edge)); }
  .cached { font-size: var(--text-xs); color: color-mix(in srgb, var(--text-muted) 70%, transparent); }
  .cap { display: flex; align-items: center; gap: var(--space-1); }
  .bar { flex: 1; min-width: 0; height: 4px; border-radius: 2px; background: color-mix(in srgb, var(--edge) 60%, transparent); overflow: hidden; }
  .fill { display: block; height: 100%; background: var(--accent); border-radius: 2px; transition: width var(--motion-mid) var(--ease-out); }
  .cap-txt { font-size: var(--text-xs); color: var(--text-muted); white-space: nowrap; flex: 0 0 auto; }
  @media (prefers-reduced-motion: reduce) { .fill { transition: none; } .tile.drag-over { transform: none; } }
</style>
