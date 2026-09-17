<script>
  // Public player directory: a searchable list of players who have opted in to
  // being found. char_name is the ONLY identifier handled (never account_id); the
  // server resolves the account when a DM is sent. Search is debounced (~250ms)
  // and stale responses are discarded by sequence. Each row can open an inline DM
  // composer locked to that player. Neutral/amber chrome: this is admin.db
  // metadata, not live game telemetry.
  import { onMount } from 'svelte';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import EliHint from './EliHint.svelte';
  import MessageComposer from './MessageComposer.svelte';
  import { api } from '$lib/api.js';

  let players = $state([]);
  let status = $state('loading'); // 'loading' | 'ready' | 'error'
  let query = $state('');
  let activeQuery = $state(''); // the query the current results reflect
  let openFor = $state(null);   // char_name whose composer is open
  let seq = 0;
  let timer = null;

  async function run(q) {
    const mine = ++seq;
    status = 'loading';
    try {
      const r = await api.players.list(q);
      if (mine !== seq) return; // a newer search superseded this one
      players = Array.isArray(r?.players) ? r.players : [];
      activeQuery = q;
      status = 'ready';
    } catch (e) {
      if (mine !== seq) return;
      players = []; activeQuery = q; status = 'error';
    }
  }

  function onInput() {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => run(query.trim()), 250);
  }

  function toggle(name) {
    openFor = openFor === name ? null : name;
  }

  onMount(() => run(''));
</script>

<CarvedSlab elevation={2}>
  <div class="head">
    <p class="kicker mono">Player directory | who you can reach</p>
    {#if status === 'ready'}<span class="count mono">{players.length} listed</span>{/if}
  </div>
  <EliHint text="Everyone who can be messaged. Uncheck yourself in visibility to hide." />

  <input
    class="search"
    type="search"
    bind:value={query}
    oninput={onInput}
    placeholder="Search by character name"
    aria-label="Search players by character name"
  />

  {#if status === 'loading'}
    <p class="skeleton">loading</p>
  {:else if status === 'error'}
    <p class="sealed">The directory could not be reached. Try again shortly.</p>
  {:else if players.length === 0}
    {#if activeQuery}
      <p class="sealed">No matches for that name.</p>
    {:else}
      <p class="sealed">No one is listed yet.</p>
    {/if}
  {:else}
    <ul class="rows">
      {#each players as p (p.char_name)}
        <li class="row">
          <div class="line">
            <span class="name">{p.char_name}</span>
            {#if p.blurb}<span class="blurb">{p.blurb}</span>{/if}
            <button class="btn" type="button" onclick={() => toggle(p.char_name)}>
              {openFor === p.char_name ? 'Close' : 'Message'}
            </button>
          </div>
          {#if openFor === p.char_name}
            <div class="dm">
              <MessageComposer recipientCharName={p.char_name} locked={true} onSent={() => (openFor = null)} />
            </div>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}
</CarvedSlab>

<style>
  .head { display: flex; align-items: baseline; justify-content: space-between; gap: var(--space-3); margin-bottom: var(--space-3); }
  .kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .24em; font-size: var(--text-xs); }
  .count { font-size: var(--text-xs); color: var(--text-muted); }
  .search {
    width: 100%; box-sizing: border-box; margin-bottom: var(--space-4);
    font-family: var(--font-sans); font-size: var(--text-sm); color: var(--text);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-2) var(--space-3); box-shadow: inset 0 1px 0 var(--metal-hi);
  }
  .search:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .rows { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-2); }
  .row {
    display: flex; flex-direction: column; gap: var(--space-2);
    padding: var(--space-3) var(--space-4);
    background: var(--metal-0); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); box-shadow: inset 0 1px 0 var(--metal-hi);
  }
  .line { display: flex; align-items: baseline; flex-wrap: wrap; gap: var(--space-2) var(--space-3); }
  .name { font-size: var(--text-sm); color: var(--text); }
  .blurb { font-size: var(--text-sm); color: var(--text-muted); line-height: 1.4; min-width: 0; word-break: break-word; }
  .btn {
    margin-left: auto;
    font-family: var(--font-mono); font-size: var(--text-xs);
    letter-spacing: .1em; text-transform: uppercase;
    padding: var(--space-1) var(--space-3);
    background: var(--bg-elevated); color: var(--text);
    border: 1px solid var(--border); border-radius: var(--radius-sm); cursor: pointer;
    transition: border-color var(--motion-fast) var(--ease-out);
  }
  .btn:hover { border-color: var(--accent); }
  .dm { padding-top: var(--space-3); border-top: 1px solid var(--edge); }
  .sealed { color: var(--text-muted); font-size: var(--text-sm); margin: 0; line-height: 1.45; }
</style>
