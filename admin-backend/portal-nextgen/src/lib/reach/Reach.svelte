<script>
  import { tick } from 'svelte';
  import { goto } from '$app/navigation';
  import { auth } from '$lib/auth.svelte.js';
  import { api } from '$lib/api.js';
  import { getPref, setPref } from '$lib/prefs.svelte.js';
  import Modal from '$lib/components/Modal.svelte';
  import { routeIndex, searchIndex, rememberPath } from './index.js';

  let { sections = [] } = $props();
  let open = $state(false);
  let query = $state('');
  let selected = $state(0);
  let input = $state(null);
  let extra = $state([]);
  let pending = $state(false);
  let unavailable = $state(false);
  let help = $state(false);
  let generation = 0;
  let owner = '';
  let routes = $derived(routeIndex(sections));
  let pins = $derived(getPref('reach_pins') || []);
  let recent = $derived(getPref('reach_recent') || []);
  let all = $derived([...routes, ...extra]);
  let results = $derived(query.trim() ? searchIndex(all, query).slice(0, 24) : []);
  let pinned = $derived(pins.map((href) => routes.find((row) => row.href === href)).filter(Boolean));
  let recents = $derived(recent.filter((href) => !pins.includes(href)).map((href) => routes.find((row) => row.href === href)).filter(Boolean).slice(0, 5));

  $effect(() => {
    const identity = auth.status === 'authed' ? auth.discordHandle : '';
    if (identity !== owner) {
      owner = identity;
      generation += 1;
      extra = [];
      pending = false;
      unavailable = false;
      open = false;
    }
  });

  async function show() {
    query = '';
    selected = 0;
    open = true;
    await tick();
    // Modal establishes its focus boundary first.
    await tick();
    input?.focus();
    if (auth.status !== 'authed' || extra.length || pending) return;
    pending = true;
    unavailable = false;
    const token = ++generation;
    const response = await Promise.allSettled([api.karum.catalog(), api.home.overview()]);
    if (token !== generation || auth.status !== 'authed') return;
    const [catalog, overview] = response;
    const items = catalog.status === 'fulfilled' ? catalog.value?.items || [] : [];
    const people = overview.status === 'fulfilled' ? overview.value?.guild?.members || [] : [];
    extra = [
      ...items.map((item) => ({ kind: 'Item', label: item.name || item.display_name || item.template_id,
        detail: 'Browse the Exchange', href: `/exchange?tpl=${encodeURIComponent(item.template_id)}` })),
      ...people.map((person) => ({ kind: 'Person', label: person.name,
        detail: `${person.online ? 'Online' : 'Offline'} · your sietch`, href: '/guilds', live: person.online === true })),
      { kind: 'Action', label: 'Claim rewards', detail: 'Review your available rewards', href: '/rewards' },
      { kind: 'Action', label: 'Send Solari', detail: 'Open your bank and transfers', href: '/storage' },
      { kind: 'Action', label: 'Check deliveries', detail: 'Packages and their delivery status', href: '/mailbox#deliveries' },
    ];
    unavailable = response.some((result) => result.status === 'rejected');
    pending = false;
  }

  function visit(row) {
    if (routes.some((route) => route.href === row.href)) {
      setPref('reach_recent', rememberPath(recent, row.href));
    }
    open = false;
    goto(row.href);
  }

  function pin(row) {
    setPref('reach_pins', pins.includes(row.href)
      ? pins.filter((href) => href !== row.href) : rememberPath(pins, row.href));
  }

  function onKey(event) {
    if (event.key === '/' && !event.ctrlKey && !event.metaKey && !event.altKey
        && !event.target?.closest?.('input, textarea, select, [contenteditable], [role="dialog"]')) {
      event.preventDefault();
      show();
    }
    if (!open || !query.trim() || event.target !== input) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      selected = Math.max(0, Math.min(results.length - 1, selected + (event.key === 'ArrowDown' ? 1 : -1)));
      document.getElementById(`reach-result-${selected}`)?.scrollIntoView({ block: 'nearest' });
    }
    if (event.key === 'Enter' && results[selected]) { event.preventDefault(); visit(results[selected]); }
  }
</script>

<svelte:window onkeydown={onKey} />

<button class="reach-trigger" type="button" onclick={show} aria-haspopup="dialog">
  <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/></svg>
  <span>Search <span class="reach-hint">maps, items and your sietch</span></span><kbd>/</kbd>
</button>

{#if open}
  <Modal title="The Reach" size="lg" onClose={() => (open = false)}>
    <label class="search-label" for="reach-input">Search the portal</label>
    <input id="reach-input" bind:this={input} bind:value={query} oninput={() => (selected = 0)}
      type="search" placeholder="Try a map, item, player or action" autocomplete="off"
      role="combobox" aria-expanded={query.trim().length > 0} aria-controls="reach-results"
      aria-autocomplete="list" aria-activedescendant={results[selected] ? `reach-result-${selected}` : undefined} />
    {#if query.trim()}
      <ul id="reach-results" class="results" role="listbox" aria-label="Search results">
        {#each results as row, i (`${row.kind}:${row.href}:${row.label}`)}
          <li role="option" id={`reach-result-${i}`} aria-selected={selected === i}>
            <button type="button" class:active={selected === i} onclick={() => visit(row)}>
              {#if row.plate}<img src={`/img/v3/loops/${row.plate}-poster.jpg`} alt="" />{:else}<span class="kind-icon" aria-hidden="true">{row.kind.slice(0, 1)}</span>{/if}
              <span class="result-copy"><strong>{row.label}</strong><small>{row.detail}</small></span>
              <span class="kind" class:live={row.live}>{row.kind}</span>
            </button>
          </li>
        {/each}
      </ul>
      {#if !results.length}<p class="quiet">No matches. Try another item name or a section such as Storage.</p>{/if}
    {:else}
      {#each [{ label: 'Pinned shortcuts', rows: pinned }, { label: 'Recent', rows: recents }, { label: 'Explore', rows: routes.filter((row) => !pins.includes(row.href)) }] as group}
        {#if group.rows.length || group.label === 'Pinned shortcuts'}
          <p class="group-label">{group.label}</p>
          {#if !group.rows.length}<p class="quiet">Pin a destination below to keep it close.</p>{/if}
          <div class="shortcuts">
            {#each group.rows as row (row.href)}
              <div class="shortcut">
                <button class="destination" type="button" onclick={() => visit(row)}>{row.label}<small>{row.detail}</small></button>
                <button class="pin" class:pinned={pins.includes(row.href)} type="button" onclick={() => pin(row)} aria-label={`${pins.includes(row.href) ? 'Unpin' : 'Pin'} ${row.label}`} aria-pressed={pins.includes(row.href)}>
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 3 6 0-1 6 4 4v2H6v-2l4-4zM12 15v6"/></svg>
                </button>
              </div>
            {/each}
          </div>
        {/if}
      {/each}
    {/if}
    {#if pending}<p class="quiet" role="status">Loading items and your sietch...</p>{/if}
    {#if unavailable}<p class="quiet" role="status">Some sources could not be read. Pages and maps are still searchable.</p>{/if}
    <button class="help-link" type="button" aria-expanded={help} onclick={() => (help = !help)}>How to use the Reach</button>
    {#if help}<p class="quiet">Press / from any page to search. Use the arrow keys and Enter to open a result. Pin pages and maps for next time. Search opens the relevant page so you can review any action there.</p>{/if}
  </Modal>
{/if}

<style>
  .reach-trigger { min-width: 0; flex: 1; max-width: 35rem; min-height: 44px; display: flex; align-items: center; gap: 12px; text-align: left; padding: 9px 14px; border: 1px solid var(--edge-hi); border-radius: 6px; color: var(--text); background: var(--bg-elevated); font: inherit; cursor: pointer; }
  .reach-trigger > span { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  svg { width: 20px; height: 20px; flex-shrink: 0; fill: none; stroke: currentColor; stroke-width: 1.6; stroke-linecap: round; }
  .reach-hint, kbd { color: var(--text-muted); font-size: 13px; }
  kbd { border: 1px solid var(--edge); padding: 0 5px; border-radius: 3px; }
  .search-label { display: block; margin-bottom: 8px; color: var(--text-muted); font-size: 13px; }
  input { width: 100%; min-height: 48px; padding: 10px 12px; font: inherit; color: var(--text); background: var(--bg-deep); border: 1px solid var(--edge-hi); border-radius: 4px; }
  .results { list-style: none; padding: 0; margin: 12px 0; }
  .results button { width: 100%; display: flex; align-items: center; gap: 12px; text-align: left; padding: 12px 8px; background: transparent; color: var(--text); border: 1px solid transparent; border-bottom-color: var(--edge); border-radius: 3px; cursor: pointer; font: inherit; }
  .results button.active, .results button:hover { background: var(--bg-elevated); border-color: var(--accent-soft); }
  .result-copy { flex: 1; min-width: 0; }
  strong { display: block; font-weight: 500; }
  small { display: block; font-size: 12px; color: var(--text-muted); }
  img, .kind-icon { width: 42px; height: 36px; border-radius: 3px; object-fit: cover; }
  .kind-icon { display: grid; place-items: center; background: var(--bg-elevated); color: var(--text-muted); }
  .kind { font-size: 12px; color: var(--text-muted); }
  .kind.live { color: var(--ls-ibad); }
  .group-label { margin: 22px 0 8px; font-size: 13px; color: var(--text-muted); }
  .shortcuts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; }
  .shortcut { display: flex; border: 1px solid var(--edge); border-radius: 4px; }
  .destination { flex: 1; min-width: 0; padding: 10px; text-align: left; background: transparent; color: var(--text); font: inherit; font-size: 14px; border: 0; cursor: pointer; }
  .pin { align-self: center; flex-shrink: 0; width: 44px; height: 44px; display: grid; place-items: center; background: none; color: var(--text-muted); border: 0; cursor: pointer; }
  .pin.pinned { color: var(--accent-text); }
  .quiet { font-size: 13px; color: var(--text-muted); line-height: 1.6; }
  .help-link { background: none; border: 0; padding: 12px 0; color: var(--accent-text); font: inherit; font-size: 13px; cursor: pointer; }
  @media (max-width: 600px) { .reach-hint { display: none; } .shortcuts { grid-template-columns: 1fr; } .kind { font-size: 11px; } }
</style>
