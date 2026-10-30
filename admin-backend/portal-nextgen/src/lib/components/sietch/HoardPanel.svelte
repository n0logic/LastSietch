<script>
  // Your Hoard: the balance, where it came from, what you bought, and the portal
  // cosmetics you own. Ownership is the server's (delivered orders); the pickers
  // list only what the catalog marks owned, and the prefs write is validated again
  // server-side.
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import Notice from '$lib/components/Notice.svelte';
  import { sietch, setSietchPref } from '$lib/sietch.svelte.js';
  import { COPY } from './copy.js';

  let { data } = $props();

  let ledger = $derived(Array.isArray(data?.ledger) ? data.ledger : []);
  let orders = $derived(Array.isArray(data?.orders) ? data.orders : []);
  let catalog = $derived(Array.isArray(data?.catalog) ? data.catalog : []);
  let flairs = $derived(catalog.filter((c) => c.owned && c.kind === 'flair' && c.pref_key));
  let mapdots = $derived(catalog.filter((c) => c.owned && c.kind === 'mapdot' && c.pref_key));
  let prefs = $derived(data?.prefs || {});

  const SOURCES = {
    visit: 'Visited the portal', knock: 'Knocked at a door', landsraad: 'Landsraad offering', offering: 'Landsraad offering',
    activity: "Tonight's activity", moon: 'Full moon: all seven nights', streak: 'Full moon: all seven nights',
  };
  function what(row) {
    if (row.kind === 'order') return 'Token Stall purchase';
    if (row.kind === 'refund') return 'Refund';
    if (row.kind === 'admin_adjust') return row.reason || 'Awarded by staff';
    return SOURCES[row.source] || 'Earned';
  }
  function nightOf(key) {
    const c = COPY.nights.find((x) => x.key === key);
    return c ? `Night ${c.n}` : '';
  }
  const STATUS = {
    queued: 'Waiting to deliver', delivering: 'Delivering', delivered: 'Delivered',
    reconciling: 'Confirming delivery', failed: 'Could not deliver', refunded: 'Refunded',
  };
  function when(t) {
    const ms = Date.parse(typeof t === 'string' && !/[zZ]|[+-]\d\d:?\d\d$/.test(t) ? t.replace(' ', 'T') + 'Z' : t);
    if (!Number.isFinite(ms)) return '';
    return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }).format(new Date(ms));
  }
</script>

<div class="grid">
  <CarvedSlab>
    <p class="kicker mono">Your Hoard</p>
    <div class="balance">
      <span class="bal-num mono">{Number(data?.balance) || 0}</span>
      <span class="bal-lbl">{COPY.event.currency}</span>
    </div>
    <p class="fine">{Number(data?.earned_total) || 0} earned in all. {COPY.event.carryover_note}</p>

    <h3>Ledger</h3>
    {#if ledger.length === 0}
      <p class="hint">Nothing yet. Visit each night and Knock at the doors to fill your Hoard.</p>
    {:else}
      <ul class="ledger">
        {#each ledger as row (row.id)}
          <li>
            <span class="delta mono" class:neg={row.delta < 0}>{row.delta > 0 ? '+' : ''}{row.delta}</span>
            <span class="what">{what(row)}{#if row.night}<span class="night mono">{nightOf(row.night)}</span>{/if}</span>
            <span class="when mono">{when(row.created_at)}</span>
          </li>
        {/each}
      </ul>
    {/if}
  </CarvedSlab>

  <div class="side">
    <CarvedSlab>
      <p class="kicker mono">Your look</p>
      <fieldset>
        <legend>Badge</legend>
        {#if flairs.length === 0}
          <p class="hint">No badges yet. Find them at the Token Stall.</p>
        {:else}
          <label><input type="radio" name="sn-flair" checked={!prefs.flair} onchange={() => setSietchPref('flair', null)} /> None</label>
          {#each flairs as f (f.sku)}
            <label><input type="radio" name="sn-flair" checked={prefs.flair === f.pref_key} onchange={() => setSietchPref('flair', f.pref_key)} /> {f.name}</label>
          {/each}
        {/if}
      </fieldset>
      <fieldset>
        <legend>Map marker</legend>
        {#if mapdots.length === 0}
          <p class="hint">No map markers yet. Find them at the Token Stall.</p>
        {:else}
          <label><input type="radio" name="sn-mapdot" checked={!prefs.mapdot} onchange={() => setSietchPref('mapdot', null)} /> Default</label>
          {#each mapdots as m (m.sku)}
            <label><input type="radio" name="sn-mapdot" checked={prefs.mapdot === m.pref_key} onchange={() => setSietchPref('mapdot', m.pref_key)} /> {m.name}</label>
          {/each}
        {/if}
      </fieldset>
      {#if sietch.notice}<Notice tone={sietch.noticeTone} text={sietch.notice} />{/if}
    </CarvedSlab>

    <CarvedSlab>
      <p class="kicker mono">Stall orders</p>
      {#if orders.length === 0}
        <p class="hint">No orders yet.</p>
      {:else}
        <ul class="orders">
          {#each orders as o (o.id)}
            <li>
              <span class="what">{o.name || o.sku}</span>
              <span class="status mono" data-status={o.status}>{o.lane === 'spec' && (o.status === 'queued' || o.status === 'delivering') ? COPY.stall.spec_pending : o.lane === 'charxp' && (o.status === 'queued' || o.status === 'delivering') ? COPY.stall.charxp_pending : STATUS[o.status] || o.status}</span>
              <span class="price mono">{o.price}</span>
            </li>
          {/each}
        </ul>
        {#if orders.some((o) => o.lane === 'bank' && o.status === 'delivered')}<p class="fine">{COPY.stall.bank_note}</p>{/if}
      {/if}
    </CarvedSlab>
  </div>
</div>

<style>
  .grid { display: grid; grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr); gap: var(--space-4); }
  @media (max-width: 860px) { .grid { grid-template-columns: 1fr; } }
  .side { display: flex; flex-direction: column; gap: var(--space-4); }
  h3 { font-size: var(--text-lg); text-transform: uppercase; margin: var(--space-5) 0 var(--space-2); }
  .balance { display: flex; align-items: baseline; gap: var(--space-2); }
  .bal-num { font-size: var(--text-4xl); font-weight: 700; color: var(--ls-amber-bright); line-height: 1; text-shadow: 0 0 22px var(--ls-amber-glow); }
  :global([data-theme='day']) .bal-num { color: #8a5613; text-shadow: none; }
  .bal-lbl { color: var(--text-muted); }
  .fine { font-size: var(--text-sm); color: var(--text-muted); margin: var(--space-2) 0 0; }
  .hint { font-size: var(--text-sm); color: var(--text-muted); margin: 0; }
  .ledger, .orders { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; max-height: 26rem; overflow-y: auto; }
  .ledger li, .orders li { display: grid; grid-template-columns: 3.2rem minmax(0, 1fr) auto; gap: var(--space-2); align-items: baseline; padding: var(--space-2) 0; border-bottom: 1px solid var(--border-subtle); font-size: var(--text-sm); }
  .orders li { grid-template-columns: minmax(0, 1fr) auto 2.5rem; }
  .delta { color: var(--ls-green); font-weight: 600; }
  .delta.neg { color: var(--text-muted); }
  .night { color: var(--text-muted); font-size: var(--text-xs); margin-left: var(--space-2); }
  .when, .price { color: var(--text-muted); font-size: var(--text-xs); text-align: right; }
  .status { font-size: var(--text-xs); color: var(--text-muted); }
  .status[data-status='delivered'] { color: var(--ls-green); }
  .status[data-status='failed'] { color: var(--ls-red); }
  .status[data-status='reconciling'], .status[data-status='queued'], .status[data-status='delivering'] { color: var(--accent-text); }
  fieldset { border: 0; padding: 0; margin: 0 0 var(--space-4); display: flex; flex-direction: column; gap: var(--space-2); }
  legend { font-family: var(--font-display); text-transform: uppercase; letter-spacing: .06em; margin-bottom: var(--space-2); padding: 0; }
  label { display: flex; align-items: center; gap: var(--space-2); min-height: 32px; cursor: pointer; }
  input[type='radio'] { accent-color: var(--ls-melange-soft); width: 18px; height: 18px; }
</style>
