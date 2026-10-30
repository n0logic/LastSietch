<script>
  // Seven Nights of the Sietch (Halloween 2026). Tabs: Tonight, Token Stall, Your
  // Hoard and The Seven Nights. The nav entry is
  // gated on the `sietch` feature; the page itself stays reachable by URL and says
  // honestly when the doors are shut (flags.earning false).
  import { onMount, untrack } from 'svelte';
  import { base } from '$app/paths';
  import { useAuthGate } from '$lib/authGate.svelte.js';
  import { sietch, loadState, phaseAt } from '$lib/sietch.svelte.js';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import SietchHero from '$lib/components/sietch/SietchHero.svelte';
  import TonightPanel from '$lib/components/sietch/TonightPanel.svelte';
  import HoardPanel from '$lib/components/sietch/HoardPanel.svelte';
  import NightsPanel from '$lib/components/sietch/NightsPanel.svelte';
  import StallPanel from '$lib/components/sietch/StallPanel.svelte';

  const gate = useAuthGate();
  const TABS = [
    { key: 'tonight', label: 'Tonight' },
    { key: 'stall', label: 'Token Stall' },
    { key: 'hoard', label: 'Your Hoard' },
    { key: 'nights', label: 'The Seven Nights' },
  ];
  let tab = $state('tonight');

  onMount(() => {
    const h = location.hash.slice(1);
    if (TABS.some((t) => t.key === h)) tab = h;
  });
  function pick(key) {
    tab = key;
    try { history.replaceState(history.state, '', `#${key}`); } catch (e) {}
  }
  function onKey(e) {
    const i = TABS.findIndex((t) => t.key === tab);
    let next = -1;
    if (e.key === 'ArrowRight') next = (i + 1) % TABS.length;
    else if (e.key === 'ArrowLeft') next = (i - 1 + TABS.length) % TABS.length;
    if (next < 0) return;
    e.preventDefault();
    pick(TABS[next].key);
    document.getElementById(`sn-tab-${TABS[next].key}`)?.focus();
  }

  let loaded = false;
  $effect(() => {
    const status = gate.status;
    if (status === 'authed' && !loaded) { loaded = true; untrack(loadState); }
    else if (status === 'anon') { loaded = false; }
  });
  // The night rolls over at 00:00 UTC while the page is open: refetch each minute
  // so the countdown, the activity and the caps move with it.
  $effect(() => {
    if (gate.status !== 'authed') return;
    const id = setInterval(() => { if (!document.hidden) loadState(); }, 60000);
    return () => clearInterval(id);
  });

  let now = $state(Date.now());
  $effect(() => {
    const id = setInterval(() => { now = Date.now(); }, 30000);
    return () => clearInterval(id);
  });
  let data = $derived(sietch.data);
  let where = $derived(phaseAt(now + sietch.clockOffset, data?.night));
</script>

<svelte:head>
  <title>Seven Nights | Last Sietch</title>
</svelte:head>

<div class="page">
  <SietchHero phase={where.phase} target={where.target} night={data?.night} calendar={data?.calendar || []} />

  {#if gate.loading}
    <p class="skeleton">loading</p>
  {:else if gate.anon || sietch.status === 'anon'}
    <SealedPanel status="empty" action="login" emptyText="Sign in to Knock at the doors, earn LS Tokens and see your Hoard." />
  {:else if sietch.status === 'unlinked'}
    <SealedPanel status="empty" emptyText="Link a game character to join the Seven Nights: Knock at the doors, earn LS Tokens and keep a Hoard." />
    <p class="link-cta"><a href={`${base}/start`}>Link a character</a></p>
  {:else if sietch.status === 'error'}
    <SealedPanel status="error" errorText="The Sietch could not be reached right now. Try again in a moment." />
  {:else if sietch.status !== 'ready'}
    <p class="skeleton">loading</p>
  {:else}
    <div class="tabs" role="tablist" aria-label="Seven Nights" tabindex="-1" onkeydown={onKey}>
      {#each TABS as t (t.key)}
        <button
          id="sn-tab-{t.key}" role="tab" aria-selected={tab === t.key} aria-controls="sn-panel"
          tabindex={tab === t.key ? 0 : -1} class:on={tab === t.key} onclick={() => pick(t.key)}
        >{t.label}</button>
      {/each}
    </div>
    <div id="sn-panel" role="tabpanel" aria-labelledby="sn-tab-{tab}">
      {#if tab === 'tonight'}
        <TonightPanel {data} phase={where.phase} />
      {:else if tab === 'stall'}
        <StallPanel {data} now={now + sietch.clockOffset} />
      {:else if tab === 'hoard'}
        <HoardPanel {data} />
      {:else if tab === 'nights'}
        <NightsPanel {data} />
      {/if}
    </div>
  {/if}
</div>

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }
  .tabs {
    display: flex; gap: var(--space-1); margin-bottom: var(--space-4);
    border-bottom: 1px solid var(--edge); overflow-x: auto; scrollbar-width: none;
  }
  .link-cta { text-align: center; margin: var(--space-4) 0 0; }
  .link-cta a {
    display: inline-block; text-decoration: none; color: var(--bg-deep); background: var(--accent);
    border-radius: var(--radius-sm); padding: var(--space-2) var(--space-4); font-family: var(--font-display); letter-spacing: .06em;
  }
  .tabs::-webkit-scrollbar { display: none; }
  .tabs button {
    flex: 0 0 auto; min-height: 44px; padding: var(--space-2) var(--space-4);
    background: transparent; border: 0; border-bottom: 2px solid transparent; cursor: pointer;
    font-family: var(--font-display); font-size: var(--text-lg); letter-spacing: .06em; text-transform: uppercase;
    color: var(--text-muted);
    transition: color var(--motion-fast) var(--ease-out), border-color var(--motion-fast) var(--ease-out);
  }
  .tabs button:hover { color: var(--text); }
  .tabs button.on { color: var(--text); border-bottom-color: var(--ls-melange-soft); text-shadow: 0 0 14px var(--ls-melange-glow); }
</style>
