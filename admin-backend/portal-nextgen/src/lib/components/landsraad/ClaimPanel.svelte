<script>
  // Claim every house reward from the portal instead of visiting each representative.
  // Two steps on purpose: "Review claim" asks the server for the REAL plan (it runs the
  // claim and rolls it back), then "Claim now" commits that plan. Claiming only works
  // while the character is offline; the preview says so instead of letting the player
  // press a button that will refuse. After a claim the receipt stays on screen and the
  // page reloads its rewards, which can trail by up to a minute while the mirror syncs.
  import { api } from '$lib/api.js';
  import { landsraad, loadAll } from '$lib/landsraad.svelte.js';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';

  let { summary = null } = $props();

  let phase = $state('idle');   // idle | previewing | review | claiming | done
  let plan = $state(null);      // preview response {online, receipt}
  let receipt = $state(null);   // applied receipt
  let error = $state('');

  let hasRewards = $derived(Number(summary?.total_lines) > 0 || Number(summary?.total_solari) > 0);

  function fmt(n) { return (Number(n) || 0).toLocaleString(); }

  async function review() {
    error = ''; phase = 'previewing';
    try {
      plan = await api.landsraad.claim('preview');
      phase = 'review';
    } catch (e) {
      error = e?.data?.message || 'The claim could not be prepared. Try again in a moment.';
      phase = 'idle';
    }
  }

  async function confirm() {
    error = ''; phase = 'claiming';
    try {
      const r = await api.landsraad.claim('apply');
      receipt = r?.receipt || null;
      phase = 'done';
      loadAll();
    } catch (e) {
      // No envelope (network drop, timeout) means we cannot say whether it landed.
      error = e?.data?.message
        || 'We could not confirm the claim. Wait a minute, then check your rewards before trying again.';
      phase = 'review';
      if (!e?.data || e.data.error === 'unconfirmed') loadAll();
    }
  }

  function cancel() { phase = 'idle'; plan = null; error = ''; }
</script>

{#snippet lines(title, rows, note)}
  {#if rows?.length}
    {@const sameWhy = note && new Set(rows.map((x) => x.reason)).size === 1}
    <div class="grp">
      <p class="grp-h mono">{title}</p>
      <ul>
        {#each rows as r (r.name + (r.reason || ''))}
          <li>
            <span class="nm">{r.name}</span>
            <span class="qty mono">x{fmt(r.qty)}</span>
            {#if r.reason && !sameWhy}<span class="why">{r.reason}</span>{/if}
            {#if r.already}<span class="why">already unlocked</span>{/if}
          </li>
        {/each}
      </ul>
      {#if note}<p class="note">{note}</p>{/if}
    </div>
  {/if}
{/snippet}

{#snippet receiptView(rc)}
  {#if rc.solari}
    <div class="grp"><p class="grp-h mono">Solari to your backpack</p><p class="solari mono">{fmt(rc.solari)}</p></div>
  {/if}
  {@render lines('To your backpack', rc.backpack)}
  {@render lines('To the CHOAM Exchange', rc.exchange, rc.notes?.exchange)}
  {@render lines('Swatches unlocked', rc.unlocked, rc.notes?.unlocked)}
  {@render lines('Staying at the house representative', rc.at_rep, rc.notes?.at_rep)}
{/snippet}

<!-- Owns its slab so it can vanish when there is nothing to claim, yet keep the
     receipt on screen after the rewards reload to zero. -->
{#if landsraad.claimEnabled && (hasRewards || phase === 'done' || error)}
  <CarvedSlab>
  <div class="claim-panel">
    <div class="head">
      <p class="panel-kicker mono">Claim from the portal</p>
    </div>

    {#if phase === 'idle' || phase === 'previewing'}
      <p class="lead">Collect every house reward at once instead of travelling to each representative. Your character must be logged out.</p>
      <button class="claim" type="button" onclick={review} disabled={phase === 'previewing'}>
        {phase === 'previewing' ? 'Preparing…' : 'Review claim'}
      </button>
    {:else if phase === 'review' || phase === 'claiming'}
      {#if plan?.online}
        <p class="warn">Your character is in the game right now. Log out completely, wait a few minutes, then review again. This is what would be delivered:</p>
      {:else if plan?.receipt?.empty && !plan?.receipt?.at_rep?.length}
        <p class="lead">Nothing to claim for the character you are playing. Rewards earned on another character are claimed while that character is selected.</p>
      {:else if plan?.receipt?.empty}
        <p class="lead">Nothing here can be claimed from the portal right now.</p>
      {:else}
        <p class="lead">This is exactly what will be delivered. Stay logged out until it finishes.</p>
      {/if}
      {#if plan?.receipt}{@render receiptView(plan.receipt)}{/if}
      <div class="actions">
        {#if plan?.online}
          <button class="claim" type="button" onclick={review}>Review again</button>
        {:else if !plan?.receipt?.empty}
          <button class="claim" type="button" onclick={confirm} disabled={phase === 'claiming'}>
            {phase === 'claiming' ? 'Claiming…' : 'Claim now'}
          </button>
        {/if}
        <button class="ghost" type="button" onclick={cancel} disabled={phase === 'claiming'}>Cancel</button>
      </div>
    {:else if phase === 'done'}
      <p class="ok">Claimed. Log in to find your rewards. Cielago will also DM you this summary when you are back online.</p>
      {#if receipt}{@render receiptView(receipt)}{/if}
    {/if}

    {#if error}<p class="err" role="alert">{error}</p>{/if}
  </div>
  </CarvedSlab>
{/if}

<style>
  .head { margin-bottom: var(--space-2); }
  .panel-kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .18em; font-size: var(--text-xs); }
  .lead, .warn, .ok, .err, .note { font-size: var(--text-sm); margin: var(--space-2) 0; max-width: 70ch; }
  .lead, .note { color: var(--text-muted); }
  .warn { color: var(--ls-red); }
  .ok { color: var(--ls-green); }
  .err { color: var(--ls-red); }
  .grp { margin-top: var(--space-3); }
  .grp-h { margin: 0 0 var(--space-1); font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .1em; }
  ul { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 2px var(--space-3); }
  li { display: flex; gap: var(--space-2); align-items: baseline; min-width: 0; font-size: var(--text-sm); }
  .nm { color: var(--text); min-width: 0; overflow-wrap: anywhere; }
  .qty { color: var(--accent-text); font-variant-numeric: tabular-nums; }
  .why { color: var(--text-muted); font-size: var(--text-xs); }
  .solari { margin: 0; font-size: 17px; font-weight: 700; color: var(--text); }
  .actions { display: flex; gap: var(--space-3); margin-top: var(--space-4); flex-wrap: wrap; }
  .claim {
    font-family: var(--font-display);
    letter-spacing: .06em; font-size: var(--text-base);
    color: var(--bg-deep); background: var(--accent); border: 1px solid var(--accent);
    border-radius: var(--radius-sm); padding: var(--space-2) var(--space-5);
    box-shadow: 0 0 12px var(--accent-glow); cursor: pointer;
    transition: filter var(--motion-fast) var(--ease-out);
  }
  .claim:hover:not(:disabled) { filter: brightness(1.12); }
  .claim:disabled { opacity: .55; cursor: progress; }
  .ghost {
    font-size: var(--text-sm); color: var(--text-muted); background: none;
    border: 1px solid var(--edge); border-radius: var(--radius-sm); padding: var(--space-2) var(--space-4); cursor: pointer;
  }
</style>
