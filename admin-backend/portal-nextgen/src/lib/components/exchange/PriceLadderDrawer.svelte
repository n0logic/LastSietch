<script>
  // The signature surface: a spice-glass vertical price ladder for one template,
  // sliding in from the right when a ListingRow is opened. Rungs are sorted cheapest
  // first; the single cheapest ask GLOWS amber. A Bot-Floor rule (the reused
  // BotFloorRule block) shows what the CHOAM bot will pay per grade, and every rung
  // priced at/under its grade cap is tagged "flip". Selecting a buyable rung opens
  // the inline BuyPanel, which previews the Solari bank-delta and fires the optimistic
  // buy (the drain cue lives in the header stat bar). BuyPanel + BotFloorRule are kept
  // inline here so the drawer stays one cohesive instrument.
  //
  // Chrome, focus trap, inert background, scrim and Escape come from the shared Modal
  // primitive. The right-edge drawer geometry is the ONLY thing kept local: Modal
  // centres its panel, and `size` is its class hook, so `size="drawer"` names a size
  // Modal does not style and the rules below own the placement outright.
  //
  // Design law: all chrome amber. The only Ibad in the drawer is the sparkline tip
  // when the latest history point is a fresh live low; buy previews stay amber.
  import { exchange, closeItem, buy } from '$lib/exchange.svelte.js';
  import { uuidv4 } from '$lib/api.js';
  import { iconUrl } from '$lib/icons.js';
  import Modal from '$lib/components/Modal.svelte';
  import Sparkline from './Sparkline.svelte';
  import DurabilityMeter from '$lib/components/storage/DurabilityMeter.svelte';
  import WatchAlert from '$lib/components/storage/WatchAlert.svelte';

  let sel = $derived(exchange.selected);
  let open = $derived(sel.tpl != null);

  // Ladder sorted cheapest-first; the min price is the glow rung.
  let rungs = $derived([...(sel.ladder || [])].sort((a, b) => (Number(a.price) || 0) - (Number(b.price) || 0)));
  let minPrice = $derived(rungs.length ? Number(rungs[0].price) || 0 : 0);

  // grade -> bot cap, for the flip tags + the floor rule.
  let capByGrade = $derived.by(() => {
    const m = new Map();
    for (const t of sel.bot_tiers || []) m.set(Number(t.grade), Number(t.cap) || 0);
    return m;
  });
  function capFor(q) { return capByGrade.get(Number(q)) ?? 0; }
  function isFlip(r) { const c = capFor(r.quality); return c > 0 && (Number(r.price) || 0) <= c; }

  let history = $derived(sel.history);
  let calibrating = $derived(!history || history.calibrating);
  // The sparkline goes live (Ibad tip) only when the latest point equals the 7d low
  // (a real live low, a value moving now); otherwise amber.
  let lastPoint = $derived.by(() => {
    if (calibrating || !history?.points?.length) return null;
    const v = Number(history.points[history.points.length - 1]?.min_price);
    return Number.isFinite(v) ? v : null;
  });
  let sparkLive = $derived(lastPoint != null && history?.low_7d != null && lastPoint <= Number(history.low_7d));
  // Screen-reader label carries the numerals (latest min vs 7-day low) when live.
  let sparkLabel = $derived.by(() => {
    if (calibrating || lastPoint == null) return 'Price history, calibrating';
    const parts = ['Price history', `latest ${lastPoint.toLocaleString()} Solari`];
    if (history?.low_7d != null) parts.push(`7-day low ${Number(history.low_7d).toLocaleString()}`);
    return parts.join(', ');
  });

  // Buy selection (inline BuyPanel).
  let pick = $state(null);       // the selected rung
  let count = $state('1');
  let busy = $state(false);
  let watch = $state(false);
  // Idempotency key for the current buy attempt. Minted once when a rung is picked,
  // reused on retry (so an ambiguous timeout cannot double-charge), reset on a
  // settled success. See buy() in the store.
  let buyUuid = null;

  // Reset the picker when the drawer's template changes.
  let lastTpl = null;
  $effect(() => {
    if (sel.tpl !== lastTpl) { lastTpl = sel.tpl; pick = null; count = '1'; buyUuid = null; }
  });

  let pickMax = $derived(pick ? Math.max(1, Number(pick.qty) || 1) : 1);
  let countNum = $derived(Math.max(1, Math.min(Math.floor(Number(count)) || 1, pickMax)));
  let unit = $derived(pick ? Number(pick.price) || 0 : 0);
  let cost = $derived(unit * countNum);
  let bankAfter = $derived(Math.max(0, (exchange.bank.solari || 0) - cost));
  let tooPoor = $derived(cost > (exchange.bank.solari || 0));

  function choose(r) { if (r?.buyable) { pick = r; count = '1'; buyUuid = uuidv4(); } }

  async function confirm() {
    if (busy || !pick || tooPoor) return;
    if (!buyUuid) buyUuid = uuidv4();
    busy = true;
    const ok = await buy(pick, countNum, buyUuid); // same key on retry (dedupe-safe)
    busy = false;
    if (ok) { pick = null; count = '1'; buyUuid = null; }
  }
</script>

{#if open}
  <Modal size="drawer" title="CHOAM price ladder" onClose={closeItem}>
    <header class="dhead">
      <img class="dicon" src={iconUrl(sel.icon)} alt="" aria-hidden="true" />
      <div class="dtitle">
        <h2>{sel.name || 'Item'}</h2>
      </div>
    </header>
    <div class="watch-controls">
      <button type="button" aria-expanded={watch} onclick={() => (watch = !watch)}>Watch this item's price</button>
      {#if watch}<WatchAlert item={{ template: sel.tpl, name: sel.name }} onDone={() => (watch = false)} />{/if}
    </div>

    <div class="trend">
      <Sparkline points={history?.points ?? []} live={sparkLive} width={220} height={44} label={sparkLabel} />
      <div class="trend-meta mono">
        {#if calibrating}
          <span class="cal">Calibrating: gathering price data</span>
        {:else if history?.low_7d != null}
          <span class="lo">7d low <strong>{Number(history.low_7d).toLocaleString()}</strong></span>
        {/if}
      </div>
    </div>

    <!-- BotFloorRule: what the bot will pay per grade (the flip floor). -->
    {#if (sel.bot_tiers || []).length}
      <div class="floor">
        <p class="floor-k mono">CHOAM bot buys up to</p>
        <div class="floor-tiers">
          {#each sel.bot_tiers as t (t.grade)}
            <span class="tier mono">G{t.grade}<span class="tcap">{Number(t.cap).toLocaleString()}</span></span>
          {/each}
        </div>
      </div>
    {/if}

    <div class="ladder">
      {#if sel.status === 'loading'}
        <p class="hint mono">reading ladder&hellip;</p>
      {:else if sel.status === 'error'}
        <p class="hint">Could not read this listing.</p>
      {:else if rungs.length === 0}
        <p class="hint">No open listings for this item right now.</p>
      {:else}
        <ul class="rungs">
          {#each rungs as r (r.order_id)}
            {@const cheapest = (Number(r.price) || 0) === minPrice}
            <li>
              <button
                class="rung" class:cheapest class:picked={pick?.order_id === r.order_id} class:disabled={!r.buyable}
                type="button" onclick={() => choose(r)} disabled={!r.buyable}
                aria-label="Buy {r.qty} at {Number(r.price).toLocaleString()} each"
              >
                <span class="rprice mono">{Number(r.price).toLocaleString()}</span>
                <span class="rqty mono">&times;{Number(r.qty).toLocaleString()}</span>
                {#if r.quality != null}
                  <span class="rgrade">
                    <span class="gnum mono">G{r.quality}</span>
                    <DurabilityMeter durability={{ current: Number(r.quality), max: 6 }} size="ring" label="Grade" />
                  </span>
                {/if}
                <span class="rtags">
                  {#if r.is_npc}<span class="tag npc">NPC</span>{/if}
                  {#if isFlip(r)}<span class="tag flip">flip</span>{/if}
                </span>
              </button>
            </li>
          {/each}
        </ul>
      {/if}
    </div>

    <!-- BuyPanel: preview the Solari bank-delta, then optimistic-buy. Online-safe. -->
    {#if pick}
      <div class="buypanel">
        <p class="bp-head mono">Buy {pick.is_npc ? 'from CHOAM' : 'from a player'}</p>
        <div class="bp-row">
          <label class="bp-fld"><span>Count</span>
            <input type="number" min="1" max={pickMax} step="1" inputmode="numeric" bind:value={count} aria-label="Count to buy" />
          </label>
          <div class="bp-cost">
            <span class="k mono">Total</span>
            <span class="v mono">{cost.toLocaleString()}</span>
          </div>
        </div>
        <div class="bp-delta mono" class:poor={tooPoor}>
          <span class="k">Solari after</span>
          <span class="v">{bankAfter.toLocaleString()}</span>
        </div>
        {#if tooPoor}<p class="bp-warn mono" role="alert">Not enough Solari on account.</p>{/if}
        <div class="bp-actions">
          <button class="btn primary" type="button" onclick={confirm} disabled={busy || tooPoor}>{busy ? 'Buying' : `Buy ${countNum}`}</button>
          <button class="btn" type="button" onclick={() => (pick = null)}>Back</button>
        </div>
      </div>
    {/if}
  </Modal>
{/if}

<style>
  .watch-controls { margin: 16px 0; }
  .watch-controls > button { min-height: 44px; padding: 8px 12px; border: 1px solid var(--edge); border-radius: 4px; background: var(--bg-elevated); color: var(--accent-text); font: inherit; font-size: 14px; cursor: pointer; }
  /* Right-edge drawer geometry for the panel Modal renders. `size="drawer"` is a
     size Modal itself does not style, so nothing here fights a shipped rule. */
  :global(.modal.size-drawer) {
    top: 0; right: 0; left: auto; bottom: 0;
    transform: none; width: min(440px, 94vw); height: 100dvh; max-height: none;
    display: flex; flex-direction: column;
    background: linear-gradient(180deg, var(--metal-1), var(--metal-0));
    border: 0; border-left: 1px solid var(--edge-hi); border-radius: 0;
    box-shadow: -30px 0 60px -30px var(--shadow-cast);
    animation: slide var(--motion-mid) var(--ease-out);
  }
  :global(.modal.size-drawer .mbody) {
    flex: 1 1 auto; min-height: 0; gap: var(--space-4);
    padding: var(--space-3) var(--space-5) var(--space-5);
  }
  @keyframes slide { from { transform: translateX(24px); opacity: .4; } to { transform: none; opacity: 1; } }

  .dhead { display: flex; align-items: center; gap: var(--space-3); }
  .dicon { width: 44px; height: 44px; object-fit: contain; filter: drop-shadow(0 0 5px var(--accent-glow)); }
  .dtitle { flex: 1; min-width: 0; }
  .dtitle h2 { font-size: var(--text-xl); text-transform: uppercase; margin: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  .trend { display: flex; flex-direction: column; gap: var(--space-1); padding: var(--space-3); border: 1px solid var(--edge); border-radius: var(--radius-sm); background: color-mix(in srgb, var(--accent) 5%, var(--metal-0)); }
  .trend-meta { font-size: var(--text-xs); color: var(--text-muted); }
  .trend-meta .lo strong { color: var(--accent-text); }
  .trend-meta .cal { color: color-mix(in srgb, var(--text-muted) 85%, transparent); }

  .floor { display: flex; flex-direction: column; gap: var(--space-2); }
  .floor-k { margin: 0; font-size: var(--text-xs); color: var(--accent); text-transform: uppercase; letter-spacing: .12em; }
  .floor-tiers { display: flex; flex-wrap: wrap; gap: var(--space-1); }
  .tier { font-size: var(--text-xs); display: inline-flex; align-items: baseline; gap: 4px; padding: 2px 6px; border: 1px solid var(--edge); border-radius: var(--radius-sm); color: var(--text-muted); }
  .tier .tcap { color: var(--accent-text); font-variant-numeric: tabular-nums; }

  .ladder { flex: 0 0 auto; }
  .hint { margin: var(--space-2) 0; font-size: var(--text-sm); color: var(--text-muted); }
  .rungs { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-1); }
  .rung {
    width: 100%; display: grid; align-items: center; gap: var(--space-2);
    grid-template-columns: 1fr auto auto auto; text-align: left;
    padding: var(--space-2) var(--space-3);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm); cursor: pointer;
    transition: border-color var(--motion-fast) var(--ease-out), transform var(--motion-fast) var(--ease-out);
  }
  .rung:hover:not(.disabled) { border-color: var(--accent); transform: translateX(-2px); }
  /* The single cheapest ask glows amber (chrome highlight, the spice-glass top). */
  .rung.cheapest {
    border-color: color-mix(in srgb, var(--accent) 55%, var(--edge));
    background: color-mix(in srgb, var(--accent) 10%, var(--metal-0));
    box-shadow: 0 0 12px -2px var(--accent-glow), inset 0 1px 0 var(--metal-hi);
  }
  .rung.picked { border-color: var(--accent-bright); box-shadow: 0 0 0 1px var(--accent-glow); }
  .rung.disabled { opacity: .5; cursor: not-allowed; }
  .rprice { font-size: var(--text-base); font-weight: 700; color: var(--accent-text); font-variant-numeric: tabular-nums; }
  .rqty { font-size: var(--text-xs); color: var(--text-muted); }
  .rgrade { display: inline-flex; flex-direction: column; gap: 2px; min-width: 42px; }
  .gnum { font-size: 10px; color: var(--text-muted); }
  .rtags { display: inline-flex; gap: 4px; }
  .tag { font-size: 9px; text-transform: uppercase; letter-spacing: .08em; padding: 1px 4px; border-radius: 2px; border: 1px solid var(--edge); color: var(--text-muted); }
  .tag.flip { color: var(--bg-deep); background: var(--accent); border-color: var(--accent); }

  .buypanel { margin-top: auto; display: flex; flex-direction: column; gap: var(--space-2); padding: var(--space-3); border: 1px solid var(--edge-hi); border-radius: var(--radius-sm); background: var(--metal-1); box-shadow: inset 0 1px 0 var(--metal-hi); }
  .bp-head { margin: 0; font-size: var(--text-xs); color: var(--accent); text-transform: uppercase; letter-spacing: .1em; }
  .bp-row { display: flex; align-items: flex-end; gap: var(--space-3); }
  .bp-fld { display: flex; flex-direction: column; gap: var(--space-1); font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .06em; }
  .bp-fld input {
    width: 6rem; font-family: var(--font-mono); font-variant-numeric: tabular-nums; font-size: var(--text-sm); color: var(--text);
    background: var(--metal-0); border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-1) var(--space-2); text-transform: none; letter-spacing: normal;
  }
  .bp-fld input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .bp-cost { margin-left: auto; display: flex; flex-direction: column; align-items: flex-end; gap: 2px; }
  .bp-cost .k, .bp-delta .k { font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .08em; }
  .bp-cost .v { font-size: var(--text-lg); font-weight: 700; color: var(--accent-text); font-variant-numeric: tabular-nums; }
  .bp-delta { display: flex; align-items: baseline; justify-content: space-between; padding-top: var(--space-1); border-top: 1px solid var(--edge); }
  .bp-delta .v { font-size: var(--text-sm); color: var(--accent-text); font-variant-numeric: tabular-nums; }
  .bp-delta.poor .v { color: var(--ls-red); }
  .bp-warn { margin: 0; font-size: var(--text-xs); color: var(--ls-red); }
  .bp-actions { display: flex; gap: var(--space-2); }
  .btn { flex: 1 1 0; font-family: var(--font-mono); font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase; padding: var(--space-2) var(--space-3); border-radius: var(--radius-sm); color: var(--text); background: var(--metal-0); border: 1px solid var(--edge); cursor: pointer; }
  .btn.primary { color: var(--bg-deep); background: var(--accent); border-color: var(--accent); box-shadow: 0 0 10px var(--accent-glow); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }

  @media (prefers-reduced-motion: reduce) {
    :global(.modal.size-drawer) { animation: none; }
    .rung:hover:not(.disabled) { transform: none; }
  }
</style>
