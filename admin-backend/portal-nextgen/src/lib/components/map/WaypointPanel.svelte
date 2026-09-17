<script>
  // Private waypoint list for the sidebar/sheet (authed chrome only; the route
  // never mounts this while anon). Purely presentational: the route owns the
  // waypoint array, optimistic CRUD and rollback; this panel emits intents.
  // Notes come from the player and render via text interpolation only.
  let {
    waypoints = [],
    coarse = false,
    error = '',
    onpan,
    onrename,
    ondelete,
  } = $props();

  const NOTE_MAX = 200; // mirrors the backend's _WP_NOTE_MAX cap

  let editingId = $state(null);
  let draft = $state('');

  function startEdit(wp) {
    editingId = wp.id;
    draft = wp.note || '';
  }
  function cancelEdit() {
    editingId = null;
    draft = '';
  }
  function commitEdit(wp) {
    if (editingId !== wp.id) return;
    const note = draft.trim().slice(0, NOTE_MAX);
    editingId = null;
    if (note !== (wp.note || '')) onrename?.(wp, note);
  }
  function onEditKeydown(e, wp) {
    if (e.key === 'Enter') { e.preventDefault(); commitEdit(wp); }
    if (e.key === 'Escape') { e.preventDefault(); cancelEdit(); }
  }

  function label(wp) {
    return wp.note || `Pin ${wp.id > 0 ? wp.id : ''}`.trim();
  }
</script>

<section class="wp" aria-label="Waypoints">
  <div class="head">
    <h2 class="title mono">Waypoints</h2>
    {#if waypoints.length}<span class="count mono">{waypoints.length}</span>{/if}
  </div>
  <p class="hint mono">
    {coarse ? 'Press and hold the board to drop a pin.' : 'Alt+click the board to drop a pin.'}
  </p>

  {#if error}
    <p class="error mono" role="alert">{error}</p>
  {/if}

  {#if waypoints.length}
    <ul class="list" role="list">
      {#each waypoints as wp (wp.id)}
        <li class="item" class:pending={wp.pending}>
          <span class="pin-glyph" aria-hidden="true">&#x25C6;</span>
          {#if editingId === wp.id}
            <!-- svelte-ignore a11y_autofocus -->
            <input
              class="edit mono"
              type="text"
              maxlength={NOTE_MAX}
              bind:value={draft}
              autofocus
              aria-label="Waypoint note"
              onkeydown={(e) => onEditKeydown(e, wp)}
              onblur={() => commitEdit(wp)}
            />
          {:else}
            <button class="note" title="Show on the board" onclick={() => onpan?.(wp)}>
              {label(wp)}
            </button>
            <button
              class="act mono"
              aria-label="Rename waypoint {label(wp)}"
              disabled={wp.pending}
              onclick={() => startEdit(wp)}
            >Edit</button>
          {/if}
          <button
            class="act del"
            aria-label="Delete waypoint {label(wp)}"
            disabled={wp.pending}
            onclick={() => ondelete?.(wp)}
          >&times;</button>
        </li>
      {/each}
    </ul>
  {:else}
    <p class="empty mono">No waypoints yet.</p>
  {/if}
</section>

<style>
  .wp {
    display: flex; flex-direction: column; gap: var(--space-2);
    border-top: 1px solid var(--border-subtle);
    padding-top: var(--space-3); margin-top: var(--space-3);
  }
  .head { display: flex; align-items: baseline; gap: var(--space-2); }
  .title {
    margin: 0; font-size: var(--text-xs); font-weight: 700;
    letter-spacing: .22em; text-transform: uppercase; color: var(--accent);
  }
  .count { color: var(--text-muted); font-size: var(--text-xs); }
  .hint { margin: 0; color: var(--text-muted); font-size: var(--text-xs); letter-spacing: .04em; }
  .error {
    margin: 0; color: var(--ls-red); font-size: var(--text-xs);
    letter-spacing: .04em;
  }

  .list {
    margin: 0; padding: 0; list-style: none;
    display: flex; flex-direction: column;
    max-height: 240px; overflow-y: auto; scrollbar-width: thin;
  }
  /* Etched label plates: each pin sits on a shallow inset metal strip. */
  .item {
    display: flex; align-items: center; gap: var(--space-2);
    padding: var(--space-1) var(--space-2); margin-top: var(--space-1);
    background: var(--metal-0);
    border: 1px solid var(--border-subtle); border-radius: var(--radius-sm);
    box-shadow: inset 0 1px 2px rgba(0, 0, 0, .35), inset 0 -1px 0 var(--metal-hi);
  }
  .item.pending { opacity: .55; }
  .pin-glyph { flex: none; color: var(--accent); font-size: var(--text-xs); }
  .note {
    flex: 1; min-width: 0; background: transparent; border: 0; cursor: pointer;
    color: var(--text); text-align: left; padding: var(--space-1) 0;
    font-family: var(--font-sans); font-size: var(--text-sm);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .note:hover { color: var(--accent-bright); }
  .edit {
    flex: 1; min-width: 0; background: var(--bg-elevated); color: var(--text);
    border: 1px solid var(--accent); border-radius: var(--radius-sm);
    padding: var(--space-1) var(--space-2); font-size: var(--text-sm);
  }
  .edit:focus { outline: none; }
  .act {
    flex: none; background: transparent; border: 0; cursor: pointer;
    color: var(--text-muted); font-size: var(--text-xs);
    letter-spacing: .1em; text-transform: uppercase; padding: var(--space-1);
    transition: color var(--motion-fast) var(--ease-out);
  }
  .act:hover:not(:disabled) { color: var(--text); }
  .act:disabled { cursor: default; opacity: .5; }
  .act.del { font-size: var(--text-base); line-height: 1; }
  .act.del:hover:not(:disabled) { color: var(--ls-red); }

  .empty { margin: 0; color: var(--text-muted); font-size: var(--text-xs); letter-spacing: .08em; }
</style>
