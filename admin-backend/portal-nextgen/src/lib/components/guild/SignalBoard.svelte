<script>
  // Filterable recruiting directory: live guild census (Ibad) merged with the
  // amber recruiting rows. Filters are pure client-side over the loaded set. Empty
  // and error are honest sealed states, never a fabricated row.
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import CarvedGuildSlab from './CarvedGuildSlab.svelte';
  import EliHint from './EliHint.svelte';

  // status: 'loading' | 'ready' | 'error'
  let { guilds = [], status = 'loading', canRequest = false } = $props();

  let list = $derived(Array.isArray(guilds) ? guilds : []);

  let query = $state('');
  let recruitingOnly = $state(true);
  let npOnly = $state(false);
  let playstyle = $state('');
  let timezone = $state('');

  function isRecruiting(g) { return g?.recruiting?.open === true || g?.recruiting?.open === 1; }
  function isNp(g) { return g?.recruiting?.new_player_friendly === true || g?.recruiting?.new_player_friendly === 1; }

  // Filter option lists derived from the recruiting rows present in the data.
  let playstyles = $derived([...new Set(list.map((g) => g?.recruiting?.playstyle).filter(Boolean))].sort());
  let timezones = $derived([...new Set(list.map((g) => g?.recruiting?.timezone).filter(Boolean))].sort());

  let filtered = $derived(
    list.filter((g) => {
      if (recruitingOnly && !isRecruiting(g)) return false;
      if (npOnly && !isNp(g)) return false;
      if (playstyle && g?.recruiting?.playstyle !== playstyle) return false;
      if (timezone && g?.recruiting?.timezone !== timezone) return false;
      if (query.trim()) {
        const q = query.trim().toLowerCase();
        const hay = `${g?.name || ''} ${g?.faction || ''} ${g?.recruiting?.message || ''}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    })
  );

  // Recruiting guilds first, then by size (the directory has no online census),
  // then name; stable across polls.
  let sorted = $derived(
    [...filtered].sort((a, b) => {
      const ar = isRecruiting(a) ? 0 : 1, br = isRecruiting(b) ? 0 : 1;
      if (ar !== br) return ar - br;
      const am = Number(b?.member_count) || 0, bm = Number(a?.member_count) || 0;
      if (am !== bm) return am - bm;
      return (a?.name || '').localeCompare(b?.name || '');
    })
  );
</script>

<CarvedSlab elevation={2}>
  <div class="head">
    <p class="kicker mono">Signal Board | recruiting directory</p>
    {#if status === 'ready'}<span class="count mono">{sorted.length} of {list.length}</span>{/if}
  </div>
  <EliHint text="Guilds looking for new members. Browse and ask to join." />

  {#if status === 'loading'}
    <p class="skeleton">loading</p>
  {:else if status === 'error'}
    <p class="sealed">The directory could not be reached. Try again shortly.</p>
  {:else if list.length === 0}
    <p class="sealed">No sietches are listed yet.</p>
  {:else}
    <div class="filters">
      <input class="search" type="search" bind:value={query} placeholder="Search sietch, faction, blurb" aria-label="Search guilds" />
      <label class="chip"><input type="checkbox" bind:checked={recruitingOnly} /> Recruiting only</label>
      <label class="chip"><input type="checkbox" bind:checked={npOnly} /> New-player friendly</label>
      {#if playstyles.length}
        <select bind:value={playstyle} aria-label="Filter by playstyle">
          <option value="">Any playstyle</option>
          {#each playstyles as p}<option value={p}>{p}</option>{/each}
        </select>
      {/if}
      {#if timezones.length}
        <select bind:value={timezone} aria-label="Filter by timezone">
          <option value="">Any timezone</option>
          {#each timezones as t}<option value={t}>{t}</option>{/each}
        </select>
      {/if}
    </div>

    {#if sorted.length === 0}
      <p class="sealed">No sietches match those filters.</p>
    {:else}
      <div class="grid">
        {#each sorted as g (g.guild_id)}
          <CarvedGuildSlab guild={g} {canRequest} />
        {/each}
      </div>
    {/if}
  {/if}
</CarvedSlab>

<style>
  .head { display: flex; align-items: baseline; justify-content: space-between; gap: var(--space-3); margin-bottom: var(--space-3); }
  .kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .24em; font-size: var(--text-xs); }
  .count { font-size: var(--text-xs); color: var(--text-muted); }
  .filters { display: flex; flex-wrap: wrap; gap: var(--space-2); align-items: center; margin-bottom: var(--space-4); }
  .search {
    flex: 1 1 12rem; min-width: 10rem;
    font-family: var(--font-sans); font-size: var(--text-sm); color: var(--text);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-3); box-shadow: inset 0 1px 0 var(--metal-hi);
  }
  .search:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .chip {
    display: inline-flex; align-items: center; gap: var(--space-2);
    font-size: var(--text-xs); color: var(--text-muted); letter-spacing: .04em;
    padding: var(--space-1) var(--space-2); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); cursor: pointer; user-select: none;
  }
  .chip input { accent-color: var(--accent); }
  select {
    font-family: var(--font-mono); font-size: var(--text-xs); color: var(--text);
    background: var(--bg-elevated); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-1) var(--space-2); cursor: pointer;
  }
  select:hover { border-color: var(--accent); }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 20rem), 1fr)); gap: var(--space-3); }
  .sealed { color: var(--text-muted); font-size: var(--text-sm); margin: 0; line-height: 1.45; }
</style>
