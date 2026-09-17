<script>
  // Installed-parts durability for the selected vehicle. Installed parts are NOT the
  // items in the cargo grid -- they live in dune.vehicle_modules and are read live.
  // `health` = current durability vs factory; `integrity` = the (decayed) max cap vs
  // factory, so integrity < 100% is permanent decay the Refurbish Vehicle button undoes.
  import { onDestroy } from 'svelte';
  import { api } from '$lib/api.js';
  import { storage } from '$lib/storage.svelte.js';

  let { containerId } = $props();

  let parts = $state([]);
  let status = $state('idle');   // 'idle' | 'loading' | 'ok' | 'empty' | 'error'
  let cd = $state({ enabled: true, cap: 0, uses_left: 0, remaining: 0 });
  let now = $state(Date.now());
  const tick = setInterval(() => { now = Date.now(); }, 1000);
  onDestroy(() => clearInterval(tick));
  let cdUntil = $state(0);       // epoch ms the cooldown clears (from the fetched remaining)
  let cdLeft = $derived(Math.max(0, Math.ceil((cdUntil - now) / 1000)));

  function fmtCd(s) {
    const m = Math.floor(s / 60), sec = s % 60;
    return m > 0 ? `${m}m ${String(sec).padStart(2, '0')}s` : `${sec}s`;
  }

  async function load(id) {
    if (id == null) { parts = []; status = 'idle'; return; }
    status = 'loading';
    try {
      const r = await api.storage.vehicleParts(id);
      parts = Array.isArray(r?.parts) ? r.parts : [];
      cd = {
        enabled: r?.refurbish_enabled !== false,
        cap: r?.refurbish_cap ?? 0,
        uses_left: r?.refurbish_uses_left ?? 0,
        remaining: r?.refurbish_cooldown_remaining ?? 0,
      };
      cdUntil = cd.remaining > 0 ? Date.now() + cd.remaining * 1000 : 0;
      status = parts.length ? 'ok' : 'empty';
    } catch {
      parts = []; status = 'error';
    }
  }

  // Re-fetch whenever the selected vehicle changes OR a refurbish bumps the token.
  $effect(() => { storage.partsRefreshToken; load(containerId); });

  function tone(pct) {
    if (pct == null) return 'na';
    if (pct >= 90) return 'good';
    if (pct >= 60) return 'warn';
    return 'bad';
  }
</script>

<div class="parts">
  <p class="kicker mono">Installed parts &middot; durability</p>

  {#if cd.enabled && cd.cap > 0}
    <p class="cd mono">
      Refurbish: {#if cdLeft > 0}on cooldown, next in {fmtCd(cdLeft)}{:else}{cd.uses_left} of {cd.cap} use{cd.cap === 1 ? '' : 's'} left{/if}
      <span class="cd-note">&middot; this vehicle only</span>
    </p>
  {/if}

  {#if status === 'loading'}
    <p class="hint">Reading parts…</p>
  {:else if status === 'error'}
    <p class="hint bad">Could not read this vehicle's parts. Try again in a moment.</p>
  {:else if status === 'empty'}
    <p class="hint">No durability-bearing parts found on this vehicle.</p>
  {:else if status === 'ok'}
    <ul class="list">
      {#each parts as p (p.template_id)}
        <li class="row">
          <span class="name" title={p.template_id}>{p.name}</span>
          <span class="bars">
            <span class="bar" title="Condition (current durability)">
              <span class="fill" data-tone={tone(p.health_pct)} style="width:{p.health_pct ?? 0}%"></span>
            </span>
            <span class="pct" data-tone={tone(p.health_pct)}>{p.health_pct ?? '—'}%</span>
          </span>
          {#if p.integrity_pct != null && p.integrity_pct < 100}
            <span class="decay" title="Maximum durability lost to wear — Refurbish Vehicle restores it">
              max {p.integrity_pct}%
            </span>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .parts { display: flex; flex-direction: column; gap: var(--space-2); }
  .kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .24em; font-size: var(--text-xs); }
  .cd { margin: 0; font-size: var(--text-xs); color: var(--accent-text, var(--accent)); }
  .cd-note { color: var(--text-dim, var(--text)); opacity: .7; }
  .hint { margin: 0; font-size: var(--text-xs); color: var(--text-dim, var(--text)); }
  .hint.bad { color: var(--ls-red); }
  .list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-1); }
  .row { display: grid; grid-template-columns: 1fr auto auto; align-items: center; gap: var(--space-2); font-size: var(--text-xs); }
  .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); }
  .bars { display: inline-flex; align-items: center; gap: var(--space-2); }
  .bar { display: inline-block; width: 5rem; height: 6px; border-radius: 3px; background: var(--metal-1); border: 1px solid var(--edge); overflow: hidden; }
  .fill { display: block; height: 100%; }
  .fill[data-tone='good'] { background: var(--ls-green); }
  .fill[data-tone='warn'] { background: var(--accent); }
  .fill[data-tone='bad'] { background: var(--ls-red); }
  .fill[data-tone='na'] { background: var(--edge); }
  .pct { font-family: var(--font-mono); min-width: 2.6rem; text-align: right; }
  .pct[data-tone='good'] { color: var(--ls-green); }
  .pct[data-tone='warn'] { color: var(--accent-text, var(--accent)); }
  .pct[data-tone='bad'] { color: var(--ls-red); }
  .pct[data-tone='na'] { color: var(--text-dim, var(--text)); }
  .decay { font-family: var(--font-mono); color: var(--accent-text, var(--accent)); font-size: calc(var(--text-xs) * 0.92); }
</style>
