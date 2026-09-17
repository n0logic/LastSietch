<script>
  // Home panel: who from your guild is on the sand. (A sietch is the server
  // community, Habbanya or Kulon; a guild is a guild. The card says which.) One row per member, at most
  // the twelve the server already sorted (online first); the order is the
  // server's and is not re-sorted here, so the list does not shuffle between polls.
  //
  // Presence is BINARY. `online === true` is Online and wears the Ibad live dot;
  // everything else -- false, missing, a row the census could not resolve -- is
  // the literal word OFFLINE. There is no third, indeterminate state: a member
  // the census could not place is simply not online as far as we can say.
  //
  // A member with no map and no last-seen figure gets no caption at all rather
  // than an invented one.
  import { base } from '$app/paths';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import LiveDot from '$lib/components/LiveDot.svelte';

  // status: 'loading' | 'ready' | 'sealed' | 'none'  ('none' = not in a guild)
  let { guildName = null, members = [], status = 'loading' } = $props();

  const MAX_ROWS = 12;

  let list = $derived(Array.isArray(members) ? members.slice(0, MAX_ROWS) : []);
  let onlineCount = $derived(list.filter((m) => m?.online === true).length);

  function initials(name) {
    const parts = String(name || '').trim().split(/[\s_-]+/).filter(Boolean);
    if (!parts.length) return '??';
    return (parts[0][0] + (parts[1]?.[0] ?? '')).toUpperCase();
  }

  function agoLabel(s) {
    if (typeof s !== 'number' || !Number.isFinite(s) || s < 0) return '';
    if (s < 60) return 'last seen just now';
    const m = Math.floor(s / 60);
    if (m < 60) return `last seen ${m}m`;
    const h = Math.floor(m / 60);
    if (h < 48) return `last seen ${h}h`;
    return `last seen ${Math.floor(h / 24)}d`;
  }

  // Where they are if we know it, else how long ago they were last seen, else
  // nothing at all.
  function caption(m) {
    return m?.map || agoLabel(m?.last_seen_ago_s);
  }
</script>

<CarvedSlab elevation={2}>
  <div class="head">
    <p class="kicker mono">Guild{guildName ? ` | ${guildName}` : ''}</p>
    {#if status === 'ready' && list.length > 0}
      <span class="census mono">{onlineCount} of {list.length} online</span>
    {/if}
  </div>

  {#if status === 'loading'}
    <SealedPanel status="loading" loadingText="reading the roster" />
  {:else if status === 'none'}
    <SealedPanel
      status="empty" action="none"
      emptyText="You are not in a guild yet. The Guilds page lists the guilds recruiting."
    />
    <a class="cta mono" href="{base}/guilds">Find a guild</a>
  {:else if status === 'sealed'}
    <SealedPanel status="empty" action="none" emptyText="The guild roster could not be read." />
  {:else if list.length === 0}
    <SealedPanel status="empty" action="none" emptyText="No guild members are logged yet." />
  {:else}
    <ul class="roster" role="list">
      {#each list as m, i (m?.name ?? i)}
        <li class="row" class:online={m?.online === true}>
          <span class="pv" aria-hidden="true">{initials(m?.name)}</span>
          <span class="who">
            <b class="name">{m?.name || 'Unnamed survivor'}</b>
            {#if caption(m)}<small class="mono">{caption(m)}</small>{/if}
          </span>
          {#if m?.online === true}
            <span class="state live"><LiveDot tone="live" />Online</span>
          {:else}
            <span class="state mono">OFFLINE</span>
          {/if}
        </li>
      {/each}
    </ul>
    <a class="cta mono" href="{base}/guilds">Guild hall</a>
  {/if}
</CarvedSlab>

<style>
  .head { display: flex; align-items: baseline; justify-content: space-between; gap: var(--space-3); margin-bottom: var(--space-3); }
  .kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .24em; font-size: var(--text-xs); }
  .census { font-size: var(--text-xs); color: var(--text-muted); letter-spacing: .06em; white-space: nowrap; }

  .roster { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; }
  .row {
    display: grid; grid-template-columns: 30px minmax(0, 1fr) auto;
    align-items: center; gap: var(--space-3);
    padding: var(--space-2) 0; border-top: 1px solid var(--border-subtle);
  }
  .row:first-child { border-top: 0; }
  .pv {
    width: 30px; height: 30px; border-radius: 50%;
    display: grid; place-items: center;
    font-family: var(--font-display); font-weight: 700; font-size: var(--text-xs);
    color: var(--text-muted); background: var(--metal-0);
    border: 1px solid var(--edge); box-shadow: inset 0 1px 0 var(--metal-hi);
  }
  .row.online .pv { color: var(--text); border-color: var(--edge-hi); }
  .who { min-width: 0; display: flex; flex-direction: column; gap: 1px; }
  .name {
    font-weight: 500; font-size: var(--text-sm); color: var(--text-muted);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .row.online .name { color: var(--text); }
  .who small { font-size: var(--text-xs); color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  .state {
    display: inline-flex; align-items: center; gap: var(--space-2);
    font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase;
    color: var(--text-muted); white-space: nowrap;
  }
  /* Ibad blue marks a genuinely live value; OFFLINE stays muted chrome. */
  .state.live { color: var(--ls-ibad); }

  .cta {
    display: inline-block; margin-top: var(--space-3); text-decoration: none;
    color: var(--accent-text); font-size: var(--text-xs);
    letter-spacing: .14em; text-transform: uppercase;
  }
  .cta:hover { color: var(--accent-bright); }

  /* Phone: the initials disc is decoration, and it is the first thing to give up
     its column so the name and the state word both keep their room. */
  @media (max-width: 420px) {
    .row { grid-template-columns: minmax(0, 1fr) auto; gap: var(--space-2); }
    .pv { display: none; }
  }
</style>
