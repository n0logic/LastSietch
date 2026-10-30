<script>
  // The Token Stall. The catalog, limits, ownership and the open/closed state are
  // all the server's: a secret row exists here only once /state sends it, and the
  // Buy button is a request the order transaction may still refuse.
  import { base } from '$app/paths';
  import { auth } from '$lib/auth.svelte.js';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import Notice from '$lib/components/Notice.svelte';
  import { detectQuality } from '$lib/quality.js';
  import StallBuyDialog from './StallBuyDialog.svelte';
  import { sietch, orderById } from '$lib/sietch.svelte.js';
  import { COPY } from './copy.js';

  let { data, now = Date.now() } = $props();

  const ART = '/img/sietch-nights/';
  const ART_V = '20261027b';
  const saveData = detectQuality().saveData;
  const failed = $state({});

  let open = $derived(data?.flags?.shop === true);
  let before = $derived(now < Date.parse(COPY.stall.opens_at));
  let linked = $derived((auth.linked || []).length > 0);
  let catalog = $derived(Array.isArray(data?.catalog) ? data.catalog : []);
  let main = $derived(catalog.filter((c) => !c.secret));
  let secret = $derived(catalog.filter((c) => c.secret));
  let balance = $derived(Number(data?.balance) || 0);
  let buying = $state(null);
  let result = $state(null);

  function portalLane(c) { return c.lane === 'prefs' || c.lane === 'role'; }
  function tile(c) {
    if (saveData) return null;
    if (failed[c.sku]) return `${ART}shop-empty-${portalLane(c) ? 'portal' : 'game'}-512.webp?v=${ART_V}`;
    return `${ART}shop-${c.sku}-512.webp?v=${ART_V}`;
  }
  const OPENS = new Intl.DateTimeFormat('en-US', { weekday: 'short', hour: 'numeric', timeZone: 'America/New_York' });
  function why(c) {
    if (c.owned && (c.limit_left ?? 0) <= 0) return 'Owned';
    // Nightly drops: a row that is listed but not yet on sale says when it opens.
    if (c.opens_at && now < Date.parse(c.opens_at)) return `Opens ${OPENS.format(new Date(c.opens_at))} ET`;
    if (c.disabled) return 'Not available';
    if ((c.limit_left ?? 1) <= 0) return 'Limit reached';
    if (balance < c.price) return `Need ${c.price - balance} more`;
    return '';
  }
  function done(r) {
    buying = null;
    result = r;
  }
  // The purchase notice follows the order's live status from /state, so a bank or
  // wallet delivery reads "on its way" and then "delivered" (or refunded) in place.
  let placed = $derived(result?.ok && result.order ? (orderById(result.order.id) || result.order) : null);
  let waitedOut = $derived(!!placed && sietch.tracking.id === placed.id && sietch.tracking.timedOut);
  function orderNotice(o) {
    if (o.status === 'refunded') return { tone: 'warn', text: 'That delivery did not go through, and your LS Tokens were refunded.' };
    if (o.status === 'failed') return { tone: 'error', text: 'That delivery failed. Staff will look at it; Your Hoard shows its status.' };
    if (o.lane === 'prefs') return { tone: 'ok', text: 'Bought. It is yours now.' };
    if (o.lane === 'role') return { tone: 'ok', text: 'Bought. The role appears on your Discord account within a few minutes.' };
    if (o.status === 'delivered') {
      if (o.lane === 'spec') return { tone: 'ok', text: 'Delivered. Log in to see the XP on the track.' };
      if (o.lane === 'charxp') return { tone: 'ok', text: 'Delivered. Log in to see the XP on your level.' };
      return { tone: 'ok', text: o.lane === 'wallet' ? 'Delivered. The Solari are in your wallet; relog if you do not see them.' : `Delivered. ${COPY.stall.bank_note}` };
    }
    if (o.lane === 'spec') return { tone: 'info', text: COPY.stall.spec_note };
    if (o.lane === 'charxp') return { tone: 'info', text: COPY.stall.charxp_note };
    if (waitedOut) return { tone: 'info', text: o.lane === 'wallet' ? 'On its way. Relog in a few minutes to see the Solari.' : 'On its way, check your bank after a zone change.' };
    return { tone: 'info', text: 'Bought. Delivering now.' };
  }
</script>

<div class="stall">
  <figure class="banner" aria-hidden="true">
    {#if !saveData}<img src={`${ART}token-stall-header-2400x600.webp?v=${ART_V}`} alt="" loading="lazy" decoding="async" width="2400" height="600" />{/if}
    <figcaption>
      <span class="kicker mono">{COPY.stall.name}</span>
      <span class="bal"><span class="mono">{balance}</span> {COPY.event.currency}</span>
    </figcaption>
  </figure>

  {#if !open || before}
    <CarvedSlab>
      <p class="lede">{before ? COPY.stall.before_open : COPY.stall.closed}</p>
      {#if before}<p class="fine">Opens {COPY.stall.opens_text}. Closes {COPY.stall.closes_text}.</p>{/if}
    </CarvedSlab>
  {:else}
    {#if !linked}
      <CarvedSlab hot>
        <p class="kicker mono">Before you buy</p>
        <p class="lede">{COPY.stall.link_prompt}</p>
        <ol class="steps">
          <li>Sign in with Discord. You are here already.</li>
          <li>Find your character by its in-game name.</li>
          <li>Answer three quick questions to prove it is yours.</li>
        </ol>
        <a class="cta" href={`${base}/start`}>Link a character</a>
      </CarvedSlab>
    {/if}
    {#if placed}
      {@const n = orderNotice(placed)}
      <Notice tone={n.tone} text={n.text} />
    {:else if result?.ok}
      <Notice tone="ok" text="Bought. Your Hoard shows its status." />
    {:else if result?.error}
      <Notice tone="error" text={result.message} />
    {/if}

    {#each [{ key: 'main', rows: main, title: '' }, { key: 'secret', rows: secret, title: 'The back room' }] as group (group.key)}
      {#if group.rows.length}
        {#if group.title}<h3 class="group">{group.title}</h3>{/if}
        <ul class="grid">
          {#each group.rows as c (c.sku)}
            {@const blocked = why(c)}
            <li class="item" class:owned={c.owned} class:secret={c.secret}>
              {#if tile(c)}
                <img class="tile" src={tile(c)} alt="" loading="lazy" decoding="async" width="512" height="512" onerror={() => { failed[c.sku] = true; }} />
              {/if}
              <div class="body">
                <h4>{c.name}</h4>
                {#if c.blurb}<p class="blurb">{c.blurb}</p>{/if}
                {#if c.contains?.length}
                  <ul class="contains">{#each c.contains as part (part)}<li>{part}</li>{/each}</ul>
                {:else if c.applies_to?.length && c.lane === 'bank'}
                  <p class="fits mono">Fits: {c.applies_to.join(', ')}</p>
                {/if}
                <p class="meta mono">
                  <span class="price">{c.price} {c.price === 1 ? 'LS Token' : COPY.event.currency}</span>
                  {#if (c.limit_left ?? 0) > 0}<span>{c.limit_left} left for you</span>{/if}
                  {#if c.lane === 'bank'}<span>CHOAM bank</span>{:else if c.lane === 'wallet'}<span>Wallet</span>{:else if c.lane === 'spec'}<span>Specialization</span>{:else if c.lane === 'charxp'}<span>Character</span>{:else if c.lane === 'role'}<span>Discord</span>{:else}<span>Portal</span>{/if}
                </p>
                <button class="buy" disabled={!linked || !!blocked} onclick={() => { result = null; buying = c; }}>
                  {blocked || 'Buy'}
                </button>
              </div>
            </li>
          {/each}
        </ul>
      {/if}
    {/each}
    <p class="fine">{COPY.event.carryover_note} The Stall closes {COPY.stall.closes_text}.</p>
  {/if}
</div>

{#if buying}
  <StallBuyDialog item={buying} {balance} onClose={() => { buying = null; }} onDone={done} />
{/if}

<style>
  .stall { display: flex; flex-direction: column; gap: var(--space-4); }
  .banner { position: relative; margin: 0; border-radius: var(--radius-md); overflow: hidden; border: 1px solid var(--edge); min-height: 120px; background: linear-gradient(180deg, #0e1128, #221a3d); }
  .banner img { display: block; width: 100%; height: auto; aspect-ratio: 4 / 1; object-fit: cover; }
  .banner figcaption {
    position: absolute; left: 0; right: 0; bottom: 0; display: flex; justify-content: space-between; align-items: end;
    padding: var(--space-4) var(--space-5); background: linear-gradient(0deg, rgba(5, 6, 13, .85), transparent);
    color: #efe8f7;
  }
  .banner .kicker { color: var(--ls-melange-hi); margin: 0; }
  .bal { font-size: var(--text-sm); color: #cdc3de; }
  .bal .mono { font-size: var(--text-2xl); font-weight: 700; color: var(--ls-amber-bright); }
  .lede { margin: 0; }
  .fine { font-size: var(--text-sm); color: var(--text-muted); margin: var(--space-2) 0 0; }
  .steps { margin: var(--space-3) 0; padding-left: 1.3rem; color: var(--text-muted); }
  .cta {
    display: inline-block; text-decoration: none; color: var(--bg-deep); background: var(--accent);
    border-radius: var(--radius-sm); padding: var(--space-2) var(--space-4); font-family: var(--font-display); letter-spacing: .06em;
  }
  .group { font-size: var(--text-xl); text-transform: uppercase; margin: var(--space-3) 0 0; color: var(--ls-melange-hi); }
  :global([data-theme='day']) .group { color: #6a2fa0; }
  .grid { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: var(--space-4); }
  .item {
    display: flex; flex-direction: column; border: 1px solid var(--edge); border-radius: var(--radius-md); overflow: hidden;
    background: var(--panel); box-shadow: inset 0 1px 0 var(--metal-hi);
  }
  .item.secret { border-color: color-mix(in srgb, var(--ls-melange-soft) 60%, var(--edge)); box-shadow: 0 0 18px -6px var(--ls-melange-glow); }
  .tile { display: block; width: 100%; height: auto; aspect-ratio: 1; object-fit: cover; }
  .body { display: flex; flex-direction: column; gap: var(--space-2); padding: var(--space-3) var(--space-4) var(--space-4); flex: 1; }
  h4 { margin: 0; font-family: var(--font-display); font-size: var(--text-lg); text-transform: uppercase; }
  .blurb { margin: 0; font-size: var(--text-sm); color: var(--text-muted); }
  .contains { margin: 0; padding-left: 1.1rem; font-size: var(--text-sm); color: var(--text-muted); }
  .fits { margin: 0; font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .06em; }
  .meta { margin: auto 0 0; display: flex; flex-wrap: wrap; gap: var(--space-2); font-size: var(--text-xs); color: var(--text-muted); }
  .price { color: var(--ls-amber-bright); font-weight: 600; }
  :global([data-theme='day']) .price { color: #8a5613; }
  .buy {
    min-height: 44px; border-radius: var(--radius-sm); cursor: pointer;
    border: 1px solid color-mix(in srgb, var(--ls-melange-hi) 60%, transparent);
    background: linear-gradient(180deg, #3a2463, #1b1033); color: #f3ecff;
    font-family: var(--font-display); font-size: var(--text-base); letter-spacing: .08em; text-transform: uppercase;
  }
  .buy:hover:not(:disabled) { box-shadow: 0 0 16px var(--ls-melange-glow); }
  .buy:disabled { cursor: not-allowed; background: var(--metal-0); color: var(--text-muted); border-color: var(--edge); }
  .item.owned .buy:disabled { color: var(--ls-green); }
</style>
