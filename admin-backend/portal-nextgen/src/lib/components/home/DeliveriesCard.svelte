<script>
  // Home panel: the last package the server sent this player, and how many of
  // them are still landing. PROPS IN ONLY, like every other panel in this
  // directory: the full list lives on the Mailbox page and owns the store, so
  // this card cannot disagree with it about a read it never made.
  //
  // A null count is a read that did not answer and it seals. It is NOT "no
  // packages": telling a player nothing was ever sent to them, when the truth is
  // that we could not look, is the one mistake this card can make that matters.
  // `count === 0` is the real empty and gets its own sentence.
  //
  // The state word is the package's own: delivered, partly delivered, or not
  // landed yet. The exchange leg of a welcome package stays unclaimed until the
  // player takes it off a terminal, so "partly delivered" is a normal resting
  // state here and the panel says where the rest is.
  import { base } from '$app/paths';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';

  // status: 'loading' | 'ready' | 'sealed' | 'none'  ('none' = nothing was sent)
  let { count = null, pendingCount = null, latest = null, status = 'loading' } = $props();

  // Section 1c's package states, in this card's words. Same three keys the
  // Mailbox panel's chips carry, so one package never reads as two things.
  const STATE_WORD = {
    delivered: 'delivered',
    partial: 'partly delivered',
    pending: 'not landed yet',
  };

  function stateWord(s) {
    return STATE_WORD[s] || 'state unknown';
  }

  // Relative words are Home's alone. The Mailbox list prints the real date,
  // because a player chasing a missing item needs the timestamp, not "4d ago".
  function ago(iso) {
    const t = new Date(iso).getTime();
    if (!Number.isFinite(t)) return '';
    const s = Math.floor((Date.now() - t) / 1000);
    if (s < 3600) return 'in the last hour';
    const h = Math.floor(s / 3600);
    if (h < 48) return `${h}h ago`;
    const d = Math.floor(h / 24);
    if (d < 60) return `${d}d ago`;
    return `${Math.floor(d / 30)} months ago`;
  }

  let pending = $derived(
    typeof pendingCount === 'number' && Number.isFinite(pendingCount) ? pendingCount : null
  );
  let tally = $derived(typeof count === 'number' && Number.isFinite(count) ? count : null);
</script>

<CarvedSlab elevation={2}>
  <div class="head">
    <p class="kicker mono">Deliveries</p>
    {#if status === 'ready' && tally !== null && tally > 0}
      <span class="tally mono">{tally} sent</span>
    {/if}
  </div>

  {#if status === 'loading'}
    <SealedPanel status="loading" loadingText="reading your deliveries" />
  {:else if status === 'sealed'}
    <SealedPanel status="empty" action="none" emptyText="What the server sent you could not be read." />
  {:else if status === 'none' || !latest}
    <SealedPanel status="empty" action="none" emptyText="Nothing has been sent to you yet." />
  {:else}
    <p class="latest">
      <b class="label">{latest.label}</b>
      <span class="meta mono">
        {#if ago(latest.granted_at)}<span class="when">{ago(latest.granted_at)}</span>{/if}
        <span class="state" data-state={latest.state}>{stateWord(latest.state)}</span>
      </span>
    </p>

    {#if pending !== null && pending > 0}
      <p class="pending">{pending} still landing</p>
    {/if}

    <a class="cta mono" href="{base}/mailbox#deliveries">See all deliveries</a>
  {/if}
</CarvedSlab>

<style>
  .head { display: flex; align-items: baseline; justify-content: space-between; gap: var(--space-3); margin-bottom: var(--space-3); }
  .kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .24em; font-size: var(--text-xs); }
  .tally { font-size: var(--text-xs); color: var(--text-muted); letter-spacing: .06em; white-space: nowrap; }

  .latest { margin: 0; display: flex; flex-direction: column; gap: 2px; }
  .label { font-size: var(--text-base); font-weight: 600; color: var(--text); }
  .meta { display: flex; align-items: baseline; gap: var(--space-3); flex-wrap: wrap; font-size: var(--text-xs); }
  .when { color: var(--text-muted); }
  /* The state word is CHROME, never Ibad: a package is a stored record, not a
     live reading off the sand. */
  .state { text-transform: uppercase; letter-spacing: .12em; color: var(--text-muted); }
  .state[data-state='delivered'] { color: var(--ls-green); }
  .state[data-state='partial'] { color: var(--accent-text); }

  .pending { margin: var(--space-2) 0 0; font-size: var(--text-sm); color: var(--text-muted); }

  .cta {
    display: inline-block; margin-top: var(--space-3); text-decoration: none;
    color: var(--accent-text); font-size: var(--text-xs);
    letter-spacing: .14em; text-transform: uppercase;
  }
  .cta:hover { color: var(--accent-bright); }
</style>
