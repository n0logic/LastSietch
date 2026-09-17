<script>
  // CHOAM buyer weekly-budget tracker. One carved slab holding a grid of capped
  // scopes (bulk-resource categories + capped item overrides); gear is uncapped
  // and never appears here. Data comes from exchange.botLimits (fed by
  // lastsietch-market-bot each tick).
  //
  // The two meters do NOT mean the same thing, and rendering them identically as
  // used/cap made the SELL side look broken (Components read 94,580/10,000 — 9.5x
  // "over cap" — and players reasonably read that as a bug):
  //
  //   BUY  = a HARD GATE.       At cap the bot stops buying. Full bar = a wall.
  //   SOLD = units actually SOLD to players (fulfilled orders, never open
  //          listings). Its cap is a RESTOCK TRIGGER: at cap the bot stops
  //          replenishing the shelf, but existing stock is not withdrawn, so
  //          players keep buying and the number legitimately exceeds the cap.
  //
  // So BUY shows used/cap and can warn; SOLD shows the sold count with the
  // restock threshold beside it, and going past it is labelled "restocking
  // paused" rather than styled as an alarm.
  import { exchange } from '$lib/exchange.svelte.js';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import { iconUrl } from '$lib/icons.js';

  // Live clock for the countdown; ticks each minute (cheap, reduced-motion safe).
  let now = $state(Date.now());
  $effect(() => {
    const id = setInterval(() => { now = Date.now(); }, 60000);
    return () => clearInterval(id);
  });

  // Only scopes that actually carry a cap on either side. Categories first
  // (is_item false), then capped item overrides.
  let scopes = $derived(
    (exchange.botLimits.scopes || [])
      .filter((s) => (s.buy_cap > 0) || (s.sell_cap > 0))
      .slice()
      .sort((a, b) => (a.is_item === b.is_item) ? 0 : (a.is_item ? 1 : -1))
  );

  function pct(used, cap) {
    if (!cap || cap <= 0) return 0;
    return Math.max(0, Math.min(100, Math.round((Number(used) / cap) * 100)));
  }
  function freesIn(ts) {
    if (!ts) return '';
    const ms = new Date(ts).getTime() - now;
    if (ms <= 0) return 'now';
    const h = Math.floor(ms / 3600000);
    const d = Math.floor(h / 24);
    const rh = h % 24;
    if (d > 0) return `${d}d ${rh}h`;
    if (rh > 0) return `${rh}h`;
    return `${Math.max(1, Math.floor(ms / 60000))}m`;
  }
  function fmt(n) { return (Number(n) || 0).toLocaleString(); }
</script>

{#if scopes.length}
  <CarvedSlab sharp={true} elevation={2}>
    <div class="botlimits">
      <div class="head">
        <p class="kicker mono">CHOAM buyer &middot; weekly limits</p>
        <p class="hint mono">bulk resources cap per rolling 7 days &middot; gear is uncapped</p>
        <p class="legend mono">
          <b>buy</b> = what the bot will still buy from you (a hard limit) &middot;
          <b>sold</b> = units players already bought from it; past its restock mark
          the shelf just stops being refilled, so it can read over
        </p>
      </div>
      <div class="grid">
        {#each scopes as s (s.scope)}
          <div class="scope">
            <div class="lbl">
              {#if s.icon}<img class="ico" src={iconUrl(s.icon)} alt="" aria-hidden="true" loading="lazy" />{/if}
              <span class="name mono">{s.label}</span>
            </div>
            {#if s.buy_cap > 0}
              <div class="meter" class:full={s.buy_used >= s.buy_cap}>
                <span class="mk mono">BUY</span>
                <div class="gauge" aria-hidden="true"><span class="gfill" style="width:{pct(s.buy_used, s.buy_cap)}%"></span></div>
                <span class="val mono">{fmt(s.buy_used)}/{fmt(s.buy_cap)}</span>
                {#if s.buy_reset_ts}<span class="reset mono">frees in {freesIn(s.buy_reset_ts)}</span>{/if}
              </div>
            {/if}
            {#if s.sell_cap > 0}
              <div class="meter sold" class:paused={s.sell_used >= s.sell_cap}>
                <span class="mk mono">SOLD</span>
                <div class="gauge" aria-hidden="true"><span class="gfill" style="width:{pct(s.sell_used, s.sell_cap)}%"></span></div>
                <span class="val mono">{fmt(s.sell_used)}<span class="of">&nbsp;· restock at {fmt(s.sell_cap)}</span></span>
                {#if s.sell_used >= s.sell_cap}
                  <span class="reset mono">
                    restocking paused{#if s.sell_reset_ts} &middot; resumes in {freesIn(s.sell_reset_ts)}{/if}
                  </span>
                {:else if s.sell_reset_ts}
                  <span class="reset mono">restocks in {freesIn(s.sell_reset_ts)}</span>
                {/if}
              </div>
            {/if}
          </div>
        {/each}
      </div>
    </div>
  </CarvedSlab>
{/if}

<style>
  .botlimits { padding: .5rem .35rem; }
  .head { display: flex; justify-content: space-between; align-items: baseline; gap: 1rem; flex-wrap: wrap; margin-bottom: .55rem; }
  .kicker { font-size: .72rem; letter-spacing: .14em; text-transform: uppercase; color: #e8b465; opacity: .92; }
  .hint { font-size: .64rem; color: #9a8f7a; opacity: .75; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: .5rem .9rem; }
  .scope { border: 1px solid rgba(232, 180, 101, .14); border-radius: 4px; padding: .4rem .55rem; background: rgba(0, 0, 0, .18); }
  .lbl { display: flex; align-items: center; gap: .4rem; margin-bottom: .32rem; }
  .ico { width: 18px; height: 18px; object-fit: contain; }
  .name { font-size: .8rem; color: #e7ddc9; }
  .meter { display: grid; grid-template-columns: 32px 1fr auto; align-items: center; gap: .4rem; margin: .18rem 0; }
  .mk { font-size: .56rem; letter-spacing: .1em; color: #8f866f; }
  .gauge { height: 6px; border-radius: 3px; background: rgba(232, 180, 101, .12); overflow: hidden; }
  .gfill { display: block; height: 100%; background: linear-gradient(90deg, #c8933f, #e8b465); }
  .meter.full .gfill { background: linear-gradient(90deg, #a34b2f, #d46a3f); }
  .val { font-size: .68rem; color: #cdbf9f; white-space: nowrap; }
  .of { color: #8f866f; font-size: .62rem; }
  .reset { grid-column: 2 / 4; font-size: .62rem; color: #b98a4a; opacity: .88; }
  .meter.full .reset { color: #e08a5a; }
  /* SOLD is not a wall: past the restock mark the shelf simply stops being
     refilled, so it reads calm (muted) rather than alarm-red like a hit BUY cap. */
  .meter.sold .gfill { background: linear-gradient(90deg, #6f7f5a, #9fb37c); }
  .meter.sold.paused .gfill { background: linear-gradient(90deg, #5d6550, #808a6c); }
  .meter.sold.paused .reset { color: #9aa383; }
  .legend { flex-basis: 100%; font-size: .6rem; line-height: 1.5; color: #8f866f;
            opacity: .8; margin-top: .15rem; }
  .legend b { color: #cdbf9f; font-weight: 600; }
</style>
