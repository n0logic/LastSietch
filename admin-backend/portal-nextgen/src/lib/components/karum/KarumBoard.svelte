<script>
  // The standing board: sort tabs, a text filter, a card grid, load-more. Forked
  // from bases/MarketGallery, which is a genuine per-listing browser over admin.db.
  //
  // Deliberately NOT forked from the Exchange module: that one is
  // template-AGGREGATED (it shows "IronBar, cheapest 40" across many orders), which
  // is the wrong semantics here. A Karum listing is ONE specific stack from ONE
  // named seller at ONE price, and collapsing them would hide exactly what a buyer
  // is choosing between.
  import { karum, loadBoard, loadMoreBoard } from '$lib/karum.svelte.js';
  import KarumCard from './KarumCard.svelte';
  import KarumWantedCard from './KarumWantedCard.svelte';
  import { GRADE_CHOICES, gradeLabel } from './grade.js';
  import { onDestroy } from 'svelte';
  import { getPref, setPref } from '$lib/prefs.svelte.js';

  let { canBuy = false, canFill = false, onBuy, onFill, onInspect } = $props();

  const SORTS = [
    { key: 'new', label: 'Newest' },
    { key: 'cheap', sell: 'Cheapest', wanted: 'Lowest offer' },
    { key: 'dear', sell: 'Priciest', wanted: 'Highest offer' },
  ];

  let rows = $derived(karum.board.rows);
  let qValue = $state(karum.board.q);
  let qTimer = null;
  let saved = $derived(getPref('market_searches') || []);
  let savedNotice = $state('');
  onDestroy(() => clearTimeout(qTimer));

  function saveSearch() {
    clearTimeout(qTimer);
    const entry = { q: qValue.trim().slice(0, 64), side: karum.board.side, sort: karum.board.sort, grade: karum.board.grade };
    const key = JSON.stringify(entry);
    setPref('market_searches', [entry, ...saved.filter((row) => JSON.stringify(row) !== key)].slice(0, 8));
    savedNotice = 'Search saved.';
    if (entry.q !== karum.board.q) loadBoard(entry);
  }

  function useSaved(entry) {
    clearTimeout(qTimer);
    qValue = entry.q;
    loadBoard({ ...entry, template: '' });
  }

  function setSort(sort) {
    if (sort === karum.board.sort) return;
    loadBoard({ sort });
  }
  function setSide(side) {
    if (side === karum.board.side) return;
    // grade is cleared inside loadBoard, so leaving the sell board can never carry a
    // filter the server refuses there.
    loadBoard({ side });
  }
  // The filter is the grade you HOLD, not the number on the row: picking G3 shows the
  // orders a G3 item could fill, which includes any-grade orders and "G2 or better".
  function setGrade(grade) {
    if (grade === karum.board.grade) return;
    loadBoard({ grade });
  }
  function onQInput(e) {
    qValue = e.target.value;
    if (qTimer) clearTimeout(qTimer);
    qTimer = setTimeout(() => loadBoard({ q: qValue.trim() }), 300);
  }
</script>

<div class="board">
  <div class="head">
    <div class="title">
      <p class="panel-kicker mono">The standing board</p>
      <div class="modes" role="tablist" aria-label="Trade direction">
        <button class:on={karum.board.side === 'sell'} role="tab"
                aria-selected={karum.board.side === 'sell'} type="button"
                onclick={() => setSide('sell')}>For sale</button>
        <button class:on={karum.board.side === 'wanted'} role="tab"
                aria-selected={karum.board.side === 'wanted'} type="button"
                onclick={() => setSide('wanted')}>Wanted</button>
      </div>
    </div>
    <div class="controls">
      <div class="tabs" role="tablist" aria-label="Sort listings">
        {#each SORTS as s (s.key)}
          <button
            class="tab" class:on={karum.board.sort === s.key}
            role="tab" aria-selected={karum.board.sort === s.key}
            type="button" onclick={() => setSort(s.key)}
          >{s.label || s[karum.board.side]}</button>
        {/each}
      </div>
      <input
        class="qf" type="search" placeholder="Filter by item"
        aria-label="Filter listings by item name"
        value={qValue} oninput={onQInput} maxlength="64"
      />
      <button class="save-search" type="button" onclick={saveSearch}>Save search</button>
      <button class="save-search" type="button" onclick={() => loadBoard()}>Refresh results</button>
    </div>
    {#if karum.board.side === 'wanted'}
      <div class="grades" role="group" aria-label="Filter by the grade you hold">
        <span class="grades-label mono">Fillable with</span>
        <button class="gchip" class:on={karum.board.grade == null} type="button"
                aria-pressed={karum.board.grade == null}
                onclick={() => setGrade(null)}>Any</button>
        {#each GRADE_CHOICES as choice (choice.value)}
          <button class="gchip" class:on={karum.board.grade === choice.value} type="button"
                  aria-pressed={karum.board.grade === choice.value}
                  onclick={() => setGrade(choice.value)}>{choice.label}</button>
        {/each}
      </div>
    {/if}
  </div>
  {#if saved.length}
    <div class="saved-searches" aria-label="Saved searches">
      {#each saved as entry, i (JSON.stringify(entry))}
        <span><button type="button" onclick={() => useSaved(entry)}>{entry.side === 'wanted' ? 'Wanted' : 'For sale'}: {entry.q || 'all items'}{entry.grade != null ? ` · ${gradeLabel(entry.grade)}` : ''}</button><button type="button" aria-label={`Remove saved search ${entry.q || 'all items'}`} onclick={() => setPref('market_searches', saved.filter((_, index) => index !== i))}>×</button></span>
      {/each}
    </div>
  {/if}
  {#if savedNotice}<p class="saved-note" role="status">{savedNotice}</p>{/if}

  {#if karum.board.status === 'loading' && rows.length === 0}
    <div class="grid">
      {#each Array(6) as _, i (i)}<div class="card-skel skeleton"></div>{/each}
    </div>
  {:else if karum.board.status === 'error'}
    <p class="hollow">The board could not be read right now. Try again shortly.</p>
  {:else if rows.length === 0}
    <p class="hollow">
      {karum.board.side === 'sell'
        ? 'Nothing is for sale right now. If you have something to spare, list it.'
        : karum.board.grade != null
          ? `No open request can be filled with a ${gradeLabel(karum.board.grade)} item. Clear the grade filter to see the rest.`
          : 'There are no open requests. Post what you are looking for.'}
    </p>
  {:else}
    <div class="grid">
      <!-- The card follows the ROW's shape, not the tab: a listing carries listing_id, a
           wanted order carries request_id. A row of the other kind is skipped rather than
           drawn with the wrong verb. -->
      {#each rows as row (row.listing_id ?? `r${row.request_id}`)}
        {#if row.listing_id != null && karum.board.side === 'sell'}
          <KarumCard listing={row} {canBuy} {onBuy} {onInspect} />
        {:else if row.request_id != null && karum.board.side === 'wanted'}
          <KarumWantedCard request={row} {canFill} {onFill} {onInspect} />
        {/if}
      {/each}
    </div>
    {#if karum.board.more}
      <button class="more-btn" type="button" onclick={loadMoreBoard}
              disabled={karum.board.status === 'loading'}>
        {karum.board.status === 'loading' ? 'Loading' : 'Load more'}
      </button>
    {/if}
  {/if}
</div>

<style>
  .saved-searches { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 18px; }
  .saved-searches span { display: flex; border: 1px solid var(--edge); border-radius: 4px; }
  .saved-searches button, .save-search { min-height: 40px; padding: 8px 10px; border: 0; background: var(--bg-elevated); color: var(--text-muted); font: inherit; font-size: 13px; cursor: pointer; }
  .save-search { border: 1px solid var(--edge); border-radius: 4px; }
  .saved-searches button:hover, .save-search:hover { color: var(--accent-text); }
  .saved-note { color: var(--text-muted); font-size: 12px; }
  .head { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); flex-wrap: wrap; margin-bottom: var(--space-4); }
  .title { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; }
  .panel-kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .18em; font-size: var(--text-xs); }
  .modes { display: inline-flex; border: 1px solid var(--edge); border-radius: var(--radius-sm); overflow: hidden; }
  .modes button { font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase; color: var(--text-muted); background: var(--bg-deep); border: 0; border-right: 1px solid var(--edge); padding: var(--space-1) var(--space-3); cursor: pointer; }
  .modes button:last-child { border-right: 0; }
  .modes button.on { color: var(--bg-deep); background: var(--accent); }
  .modes button:active { transform: translateY(1px); }
  .controls { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; }
  .tabs { display: inline-flex; gap: var(--space-1); }
  .tab {
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase;
    color: var(--text-muted); background: var(--metal-1); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-1) var(--space-3); cursor: pointer;
  }
  .tab.on { color: var(--accent-bright); border-color: var(--accent); }
  .qf {
    font-family: var(--font-sans); font-size: var(--text-sm); color: var(--text);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-1) var(--space-2); min-width: 10rem;
  }
  .qf:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .grades { display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-1); width: 100%; margin-top: var(--space-2); }
  .grades-label { margin-right: var(--space-1); font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: .08em; }
  .gchip {
    font-family: var(--font-mono); font-size: 10px; letter-spacing: .08em; text-transform: uppercase;
    padding: 3px var(--space-2); border-radius: var(--radius-sm); cursor: pointer;
    color: var(--text-muted); background: var(--metal-0); border: 1px solid var(--edge);
  }
  .gchip:hover { border-color: var(--edge-hi); color: var(--text); }
  .gchip.on { color: var(--bg-deep); background: var(--accent); border-color: var(--accent); }
  .gchip:active { transform: translateY(1px); }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: var(--space-3); }
  .card-skel { aspect-ratio: 16 / 13; border-radius: var(--radius-sm); }
  .hollow { color: var(--text-muted); font-size: var(--text-sm); margin: var(--space-4) 0; }
  .more-btn {
    margin: var(--space-4) auto 0; display: block;
    font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase;
    color: var(--text); background: var(--metal-1); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-5); cursor: pointer;
  }
  .more-btn:hover:not(:disabled) { border-color: var(--accent); }
  .more-btn:disabled { opacity: .5; cursor: not-allowed; }
  @media (max-width: 40rem) {
    .head, .title, .controls { align-items: stretch; }
    .head, .title { flex-direction: column; }
    .modes, .controls, .tabs { width: 100%; }
    .modes button, .tab { flex: 1 1 0; }
    .qf { width: 100%; box-sizing: border-box; }
  }
</style>
