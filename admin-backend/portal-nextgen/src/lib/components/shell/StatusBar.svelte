<script>
  // One row that answers "who am I and what do I have": the entity header (initials
  // tile, presence dot, status pill) and four readings. Props only.
  //
  // A null reading is absent, never zero. The bank reads AMBER (owned currency, not a
  // streaming value, same rule as the Wallet instrument); Ibad blue is reserved for
  // figures that update live (players online). Map NAME only, never a position.
  import { base } from '$app/paths';
  import { initials, mapNameOnly } from '$lib/shellHome.js';

  let { signals = {}, loginUrl = '' } = $props();
  const SOLARI_ICON = '/admin/static/img/dune-icons/T_UI_IconResourceSolarisCoin_D.png';

  let s = $derived(signals || {});
  let bank = $derived(typeof s.bankSolari === 'number' && Number.isFinite(s.bankSolari) ? Math.floor(s.bankSolari).toLocaleString() : null);
  let mapName = $derived(mapNameOnly(s.charMap));
  let presence = $derived(s.charOnline === true ? `Online${mapName ? ` · ${mapName}` : ''}` : s.charOnline === false ? 'Offline' : null);
  let rewardReady = $derived(s.rewardsEnabled !== false && ((s.claimableTotal || 0) > 0 || s.weeklyClaimable === true || s.monthlyClaimable === true));
</script>

{#if s.linked}
  <section class="status" aria-label="Your status">
    <a class="who" href={`${base}/character`}>
      <span class="tile" aria-hidden="true">{initials(s.charName)}<span class="dot" class:on={s.charOnline === true} class:off={s.charOnline === false}></span></span>
      <span class="id">
        <span class="name">{s.charName || 'Your character'}</span>
        {#if presence}<span class="pill" class:ok={s.charOnline === true}>{presence}</span>{/if}
      </span>
    </a>
    <a class="cell" href={`${base}/storage`}>
      <span class="k mono">Bank</span>
      {#if bank !== null}
        <span class="v"><span class="coin"><img src={SOLARI_ICON} alt="" width="22" height="22" /></span>{bank}</span>
        <span class="n">Solari</span>
      {:else}
        <span class="n">Could not be read</span>
      {/if}
    </a>
    <a class="cell" href={`${base}/exchange`}>
      <span class="k mono">Orders</span>
      {#if s.marketOpen != null || s.karumOpen != null}
        <span class="v">{(s.marketOpen || 0) + (s.karumOpen || 0)} <small>open</small></span>
        <span class="n">{(s.marketFilled || 0) > 0 ? `${s.marketFilled} filled today` : 'None filled today'}</span>
      {:else}
        <span class="n">Could not be read</span>
      {/if}
    </a>
    <a class="cell" href={`${base}/rewards`}>
      <span class="k mono">Rewards</span>
      {#if s.claimableTotal == null && s.rewardsEnabled == null}
        <span class="n">Could not be read</span>
      {:else if rewardReady}
        <span class="v ready">Ready</span><span class="n">Claim today</span>
      {:else}
        <span class="v quiet">All claimed</span><span class="n">Back tomorrow</span>
      {/if}
    </a>
    <a class="cell" href={`${base}/guilds`}>
      <span class="k mono">Sietch</span>
      {#if s.guildRead && s.guildName}
        <span class="v name2">{s.guildName}</span>
        <span class="n">{#if s.guildOnline != null}<span class="live mono">{s.guildOnline}</span> online{:else}Open roster{/if}</span>
      {:else if s.guildRead}
        <span class="v quiet">No sietch yet</span><span class="n">Find one to join</span>
      {:else}
        <span class="n">Could not be read</span>
      {/if}
    </a>
  </section>
{:else}
  <section class="status signed-out" aria-label="Your status">
    <p>Connect Discord to see your bank, orders, rewards and sietch here. The weekend and the desert above are public.</p>
    <a class="primary" href={loginUrl} data-sveltekit-reload>Connect Discord</a>
  </section>
{/if}

<style>
  .status {
    margin: var(--space-4) 0 0;
    display: grid; grid-template-columns: 1.5fr repeat(4, minmax(0, 1fr));
    background: var(--panel); border: 1px solid var(--edge); border-radius: 6px; box-shadow: var(--shadow-card);
  }
  .status > a { display: flex; flex-direction: column; justify-content: center; gap: 4px; min-height: 84px; padding: 14px 18px; text-decoration: none; border-left: 1px solid var(--border-subtle); min-width: 0; }
  .status > a:first-child { border-left: 0; flex-direction: row; align-items: center; gap: 14px; }
  .status > a:hover { background: color-mix(in srgb, var(--accent) 7%, transparent); }
  .tile { position: relative; display: grid; place-items: center; width: 44px; height: 44px; border-radius: 5px; flex: none; border: 1px solid var(--accent-soft); background: linear-gradient(160deg, #3a2c17, #1b140a); font: 700 17px var(--font-display); letter-spacing: .04em; color: var(--accent-text); }
  .tile .dot { position: absolute; right: -4px; bottom: -4px; width: 12px; height: 12px; border-radius: 50%; border: 2px solid var(--bg); background: var(--yellow, #e8c14a); }
  .tile .dot.on { background: var(--ls-green, #5fcf6b); }
  .tile .dot.off { background: #6b6253; }
  .id { display: grid; gap: 4px; min-width: 0; }
  .name { font: 700 21px/1.05 var(--font-display); letter-spacing: .04em; overflow-wrap: anywhere; }
  .pill { justify-self: start; padding: 1px 9px; border: 1px solid var(--edge-hi); border-radius: 99px; font: 500 11px var(--font-mono); letter-spacing: .08em; text-transform: uppercase; color: var(--text-muted); }
  .pill.ok { color: var(--ls-green, #5fcf6b); border-color: color-mix(in srgb, var(--ls-green, #5fcf6b) 45%, transparent); }
  .k { font-size: var(--text-xs); letter-spacing: .16em; text-transform: uppercase; color: var(--text-muted); }
  .v { display: flex; align-items: center; gap: 8px; font: 500 22px/1 var(--font-mono); color: var(--accent-text); font-variant-numeric: tabular-nums; }
  .v small { font: 400 13px var(--font-sans); color: var(--text-muted); }
  .v.ready { font: 700 24px var(--font-display); letter-spacing: .05em; text-transform: uppercase; }
  .v.quiet, .v.name2 { font: 700 20px var(--font-display); letter-spacing: .04em; color: var(--text); overflow-wrap: anywhere; }
  .coin { display: grid; place-items: center; width: 24px; height: 24px; border-radius: 50%; background: #d8c6a0; }
  .n { font-size: var(--text-sm); color: var(--text-muted); }
  .live { color: var(--ls-ibad); }
  .signed-out { grid-template-columns: 1fr auto; align-items: center; gap: var(--space-4); padding: 16px 20px; }
  .signed-out p { margin: 0; color: var(--text-muted); }
  .primary { display: inline-flex; align-items: center; min-height: 44px; padding: 0 20px; border-radius: 4px; background: linear-gradient(180deg, var(--accent-bright), var(--accent)); color: #1a0f02; text-decoration: none; font: 700 15px var(--font-display); letter-spacing: .12em; text-transform: uppercase; }
  @media (max-width: 900px) {
    .status { margin: var(--space-3) 0 0; grid-template-columns: 1fr 1fr; }
    .status > a { border-left: 0; border-top: 1px solid var(--border-subtle); min-height: 76px; padding: 12px 14px; }
    .status > a:first-child { grid-column: 1 / -1; border-top: 0; }
    .signed-out { grid-template-columns: 1fr; }
  }
</style>
