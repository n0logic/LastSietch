<script>
  // Home instrument: banked Solari. Compact by default (one number + the Solari
  // glyph the rest of the app uses), expands in place.
  //
  // Banked only. Pocket coins and the wallet delta have no source this wave, so
  // the panel says nothing about them. A null balance is SEALED, never a zero:
  // "0 Solari" is a statement about the player's money we cannot make.
  // The figure reads AMBER (owned currency, not a live-streaming value).
  import { base } from '$app/paths';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';

  let { solari = null } = $props();

  const SOLARI_ICON = '/admin/static/img/dune-icons/T_UI_IconResourceSolarisCoin_D.png';

  let open = $state(false);
  let amount = $derived(
    typeof solari === 'number' && Number.isFinite(solari) ? Math.floor(solari) : null
  );
  // The figure never wraps: it steps down a size as the banked total grows
  // (separators included, so 64,236,107 is ten characters).
  let shown = $derived(amount === null ? '' : amount.toLocaleString());
  let longFigure = $derived(shown.length >= 10);
  let hugeFigure = $derived(shown.length >= 13);
</script>

{#if amount === null}
  <SealedPanel slab status="empty" action="none" emptyText="The bank could not be read." />
{:else}
  <CarvedSlab sharp>
    <button class="face" type="button" aria-expanded={open} onclick={() => (open = !open)}>
      <span class="eyebrow mono">Wallet</span>
      <span class="val mono" class:is-long={longFigure} class:is-huge={hugeFigure}>{shown}</span>
      <img class="glyph" src={SOLARI_ICON} alt="" aria-hidden="true" loading="lazy" />
      <span class="caret" aria-hidden="true"></span>
    </button>
    {#if open}
      <div class="body">
        <p class="line">Banked at the CHOAM Exchange. Coins you are carrying are not counted here.</p>
        <a class="cta mono" href="{base}/storage">Open the bank</a>
      </div>
    {/if}
  </CarvedSlab>
{/if}

<style>
  .face {
    width: 100%; min-width: 0; display: grid; grid-template-columns: minmax(0, 1fr) auto;
    gap: 2px var(--space-3);
    align-items: center; text-align: left; background: none; border: 0; padding: 0;
    color: inherit; cursor: pointer;
  }
  .eyebrow {
    grid-column: 1; font-size: var(--text-xs); text-transform: uppercase;
    letter-spacing: .18em; color: var(--accent);
  }
  .val {
    grid-column: 1; font-size: var(--text-2xl); font-weight: 700; line-height: 1;
    color: var(--accent-text); white-space: nowrap; font-variant-numeric: tabular-nums;
  }
  .val.is-long { font-size: var(--text-xl); }
  .val.is-huge { font-size: var(--text-lg); }
  .glyph {
    grid-row: 1 / 3; grid-column: 2; align-self: center;
    width: 34px; height: 34px; object-fit: contain; opacity: .9;
  }
  .face:has(.val.is-long) .glyph { width: 28px; height: 28px; }
  .caret {
    grid-column: 2; justify-self: end; width: 8px; height: 8px;
    border-right: 1.5px solid var(--text-muted); border-bottom: 1.5px solid var(--text-muted);
    transform: rotate(45deg); margin-top: var(--space-2);
    transition: transform var(--motion-fast) var(--ease-out);
  }
  .face[aria-expanded='true'] .caret { transform: rotate(225deg); }
  .face:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }

  .body {
    margin-top: var(--space-3); padding-top: var(--space-3);
    border-top: 1px solid var(--edge); display: flex; flex-direction: column; gap: var(--space-2);
  }
  .line { margin: 0; font-size: var(--text-sm); color: var(--text-muted); line-height: 1.45; }
  .cta {
    align-self: flex-start; text-decoration: none; color: var(--accent-text);
    font-size: var(--text-xs); letter-spacing: .14em; text-transform: uppercase;
  }
  .cta:hover { color: var(--accent-bright); }

  /* Phone: the instruments row is two columns wide, which leaves a seven
     figure Solari number nowhere to go. The glyph drops, the caret moves up
     beside the eyebrow, and the figure takes the full card width one size
     down rather than breaking mid-number. */
  @media (max-width: 520px) {
    .glyph { display: none; }
    .val { grid-column: 1 / -1; font-size: var(--text-lg); }
    .caret { grid-row: 1; margin-top: 0; }
  }

  @media (prefers-reduced-motion: reduce) {
    .caret { transition: none; }
  }
</style>
