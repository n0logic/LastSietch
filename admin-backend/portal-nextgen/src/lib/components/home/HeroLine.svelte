<script>
  // The hero, set as type. Station nameplate, then one line that says where the
  // player is standing and what the sand is doing there. The three "Now" facts
  // (map, online count, next storm) fold in here rather than into a pill of
  // their own: the pill collided with the phone tab bar, and a sentence reads
  // faster than three chips anyway.
  //
  // "You are standing in <map>" is only written when the character is ONLINE and
  // the roster placed them on a board we can name. Offline, or somewhere with
  // no board of its own, the line names the instance being read instead,
  // because a last-known map is not a position and a raw map id is not a place.
  import LiveCountdown from '$lib/components/LiveCountdown.svelte';
  import ServerNameplate from '$lib/components/ServerNameplate.svelte';
  import { mapKeyFor } from '$lib/homeVerdict.js';

  let {
    status = null,
    coriolis = null,
    charName = null,
    charMap = null,
    charOnline = null,
    instLabel = null,
    mapLabel = null,
    onlinePlayers = null,
    stormActive = false,
    stormNextEtaUtc = null,
  } = $props();

  // The roster's map name falls through to the game's own id for anything
  // instanced, so an ecolab run printed "You are standing in CB_EcoLab_2". The
  // line is written only for a map that resolves to a board we read, which is
  // also the only case where it agrees with the reading underneath it.
  let placed = $derived(charOnline === true && !!charMap && mapKeyFor(charMap) != null);
  let where = $derived(placed
    ? `You are standing in ${charMap}.`
    : `Reading ${mapLabel}${instLabel ? ` | ${instLabel}` : ''}.`);
</script>

<div class="hero-line">
  <ServerNameplate {status} {coriolis} />
  <p class="where">
    <span class="where-main">{where}</span>
    {#if charName && charOnline === false}
      <span class="where-note mono">{charName} is offline</span>
    {/if}
  </p>
  <p class="facts mono">
    {#if onlinePlayers != null}<span>{onlinePlayers} online</span>{/if}
    {#if stormActive}
      <span class="warn">storm sweeping now</span>
    {:else if stormNextEtaUtc}
      <span>next storm <LiveCountdown target={stormNextEtaUtc} /></span>
    {/if}
  </p>
</div>

<style>
  .hero-line { display: flex; flex-direction: column; gap: var(--space-2); }
  .where { margin: 0; color: var(--text); font-size: var(--text-lg); line-height: 1.35; }
  .where-main { font-family: var(--font-display); }
  .where-note {
    color: var(--text-muted); font-size: var(--text-xs);
    letter-spacing: .12em; text-transform: uppercase; margin-left: var(--space-2);
  }
  .facts {
    margin: 0; display: flex; flex-wrap: wrap; gap: var(--space-3);
    color: var(--text-muted); font-size: var(--text-xs);
    letter-spacing: .12em; text-transform: uppercase;
    font-variant-numeric: tabular-nums;
  }
  .facts .warn { color: var(--ls-yellow); }
</style>
