<script>
  // Repair, all three tiers. Per-box (the SELECTED container) and per-gear share the
  // portal_repair cooldown bucket; "Refurbish Everything" is its own 24h bucket and
  // can be killed server side by REPAIR_ALL_ENABLED (a {error:'disabled'} response
  // locks that button honestly). All three are offline-gated; when the write surface
  // is locked they disable and the gate banner carries the reason. On a cooldown
  // refusal (429) the server message carries the MM:SS remaining, which we parse to
  // drive a live countdown on the shared / everything clocks.
  import { onDestroy } from 'svelte';
  import { api, uuidv4 } from '$lib/api.js';
  import { storage, selectedContainer } from '$lib/storage.svelte.js';
  import EliHint from '$lib/components/guild/EliHint.svelte';

  let sharedUntil = $state(0);       // box + gear clock (epoch ms)
  let everythingUntil = $state(0);   // 24h clock (epoch ms)
  let vehicleUntil = $state({});     // per-vehicle refurbish clocks, keyed by container id (epoch ms)
  let everythingEnabled = $state(true);
  let vehicleEnabled = $state(true);
  let now = $state(Date.now());
  let busy = $state('');   // '' | 'box' | 'gear' | 'everything' | 'vehicle'
  let notice = $state('');
  let noticeTone = $state('info');

  const tick = setInterval(() => { now = Date.now(); }, 1000);
  onDestroy(() => clearInterval(tick));

  let locked = $derived(!storage.offlineOk);
  let noBox = $derived(storage.selectedId == null);
  // The selected container is a vehicle -> offer the in-place refurbish button.
  let isVehicleSel = $derived(!!selectedContainer()?.is_vehicle);
  let sharedLeft = $derived(Math.max(0, Math.ceil((sharedUntil - now) / 1000)));
  let everythingLeft = $derived(Math.max(0, Math.ceil((everythingUntil - now) / 1000)));
  // Cooldown is per-vehicle, so the clock tracks the SELECTED vehicle's bucket.
  let vehicleLeft = $derived(Math.max(0, Math.ceil(((vehicleUntil[storage.selectedId] ?? 0) - now) / 1000)));

  function fmt(s) {
    if (s <= 0) return '';
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${String(sec).padStart(2, '0')}s`;
    return `${sec}s`;
  }

  // Pull "M:SS" / "MM:SS" out of the server cooldown copy -> seconds.
  function parseCooldown(msg) {
    const m = /(\d+):(\d{2})/.exec(msg || '');
    if (!m) return 0;
    return parseInt(m[1], 10) * 60 + parseInt(m[2], 10);
  }
  function seedCooldown(which, secs) {
    if (secs <= 0) return;
    const until = Date.now() + secs * 1000;
    if (which === 'everything') everythingUntil = until;
    else if (which === 'vehicle') vehicleUntil = { ...vehicleUntil, [storage.selectedId]: until };
    else sharedUntil = until;
  }
  function leftFor(which) {
    if (which === 'everything') return everythingLeft;
    if (which === 'vehicle') return vehicleLeft;
    return sharedLeft;
  }

  async function run(which, fn, body) {
    if (busy || locked) return;
    if (leftFor(which) > 0) return;
    busy = which; notice = '';
    try {
      const r = await fn({ ...(body || {}), uuid: uuidv4() });
      const n = r?.repaired_count;
      notice = r?.message || (typeof n === 'number' ? `Repaired ${n} item${n === 1 ? '' : 's'}.` : 'Repaired.');
      noticeTone = 'ok';
      // A refurbish writes module durability to the DB; nudge the vehicle parts panel
      // (which reads the DB) to re-fetch so the bars update without reselecting.
      if (which === 'vehicle' || which === 'everything') storage.partsRefreshToken++;
    } catch (e) {
      const token = e?.message || '';
      const serverMsg = e?.data?.message || '';
      if (token === 'disabled') {
        if (which === 'vehicle') vehicleEnabled = false; else everythingEnabled = false;
        notice = serverMsg || (which === 'vehicle'
          ? 'Vehicle Refurbish is turned off right now.'
          : 'Refurbish Everything is turned off right now.');
        noticeTone = 'warn';
      } else if (token === 'cooldown') {
        seedCooldown(which, parseCooldown(serverMsg));
        notice = serverMsg || 'Still on cooldown.';
        noticeTone = 'warn';
      } else if (token === 'player_online') {
        notice = serverMsg || 'Log out of the game to repair. Nothing changed.';
        noticeTone = 'warn';
      } else {
        notice = serverMsg || 'The repair did not run.';
        noticeTone = 'error';
      }
    } finally { busy = ''; }
  }
</script>

<div class="repair">
  <p class="kicker mono">Repair</p>
  <EliHint
    text="Fix durability on the gear you carry."
    detail="Gear repairs what you are wearing and carrying, on a short cooldown. Refurbish Everything covers your backpack, worn gear, hotbar and CHOAM bank in one pass, then rests for 24 hours; log back in to see the change. Items inside base containers cannot be repaired from the portal — move them to your backpack or CHOAM bank first, then run Refurbish Everything. Vehicles: select the vehicle and use Refurbish Vehicle to restore its mounted parts in place, no dismounting — or deconstruct the parts into your backpack or CHOAM bank and Refurbish Everything covers them too."
  />

  <div class="tiers">
    <!-- Repair box PULLED 2026-08-03. A container is held in the server's memory while
         the base is loaded, so the repair landed in Postgres and stayed invisible in-game
         until the next restart: the button reported success while nothing changed for the
         player. The route is gated server side by LASTSIETCH_REPAIR_BOX_ENABLED; restore that to
         1 and un-comment this button to bring the tier back. -->
    <button class="btn" type="button" disabled={locked || busy === 'gear' || sharedLeft > 0}
      onclick={() => run('gear', api.storage.repairGear)}>
      {busy === 'gear' ? 'Repairing' : sharedLeft > 0 ? `Gear (${fmt(sharedLeft)})` : 'Repair gear'}
    </button>
    {#if isVehicleSel && vehicleEnabled}
      <button class="btn wide" type="button"
        disabled={locked || busy === 'vehicle' || vehicleLeft > 0}
        onclick={() => run('vehicle', api.storage.repairVehicle, { inv_id: storage.selectedId })}
        title="Refurbish this vehicle's parts in place (restores max durability, no dismounting)">
        {busy === 'vehicle' ? 'Refurbishing'
          : vehicleLeft > 0 ? `Refurbish Vehicle (${fmt(vehicleLeft)})`
          : 'Refurbish Vehicle'}
      </button>
    {/if}
    <button class="btn wide" type="button"
      disabled={locked || busy === 'everything' || everythingLeft > 0 || !everythingEnabled}
      onclick={() => run('everything', api.storage.repairEverything)}>
      {#if !everythingEnabled}Refurbish Everything (off)
      {:else if busy === 'everything'}Refurbishing
      {:else if everythingLeft > 0}Refurbish Everything ({fmt(everythingLeft)})
      {:else}Refurbish Everything &middot; 24h{/if}
    </button>
  </div>

  {#if notice}
    <p class="notice" data-tone={noticeTone} role="status" aria-live="polite">{notice}</p>
  {/if}
</div>

<style>
  .repair { display: flex; flex-direction: column; gap: var(--space-2); }
  .kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .24em; font-size: var(--text-xs); }
  .tiers { display: flex; flex-wrap: wrap; gap: var(--space-2); }
  .btn {
    flex: 1 1 8rem; font-family: var(--font-mono); font-size: var(--text-xs);
    letter-spacing: .08em; text-transform: uppercase; padding: var(--space-2) var(--space-3);
    border-radius: var(--radius-sm); color: var(--text); background: var(--metal-1);
    border: 1px solid var(--edge); box-shadow: inset 0 1px 0 var(--metal-hi); cursor: pointer;
    transition: border-color var(--motion-fast) var(--ease-out);
  }
  .btn.wide { flex-basis: 100%; }
  .btn:hover:not(:disabled) { border-color: var(--accent); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
  .notice { margin: 0; font-size: var(--text-xs); }
  .notice[data-tone='ok'] { color: var(--ls-green); }
  .notice[data-tone='warn'] { color: var(--accent-text); }
  .notice[data-tone='error'] { color: var(--ls-red); }
</style>
