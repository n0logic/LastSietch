<script>
  // The event's route hero: a moonlit Arrakis night drawn in CSS, with the event
  // plate laid over it once the art lands. The CSS night IS the fallback, so a
  // missing plate (or data saver) leaves a finished hero, never a broken image.
  // One <h1> per document: this replaces PageHeader on /sietch.
  import LiveCountdown from '$lib/components/LiveCountdown.svelte';
  import { detectQuality } from '$lib/quality.js';
  import { COPY } from './copy.js';

  let { phase = 'before', target = null, night = null, calendar = [] } = $props();

  // Bumped whenever a plate is re-cut (static asset rule: new ?v= every change).
  const ART_DIR = '/img/sietch-nights/';
  const ART_V = '20261024a';
  const saveData = detectQuality().saveData;
  // The CSS moons and dunes stand down only once the plate has actually painted,
  // so a slow or missing plate never leaves an empty sky.
  let artReady = $state(false);
  let plateImg = $state(null);
  // A cached plate can finish before hydration wires onload; read it directly too.
  $effect(() => { if (plateImg?.complete && plateImg.naturalWidth > 0) artReady = true; });

  let label = $derived(phase === 'before' ? 'The doors open in' : phase === 'night' && night ? (night?.n === 7 ? 'The last night ends in' : 'Tonight ends in') : '');
  let lanterns = $derived(COPY.nights.map((c) => {
    const row = calendar.find((r) => r.n === c.n) || {};
    return { n: c.n, title: c.title, state: row.state || 'upcoming', earned: Number(row.earned) || 0 };
  }));
</script>

<header class="hero" class:art={artReady}>
  {#if !saveData}
    <picture class="plate" aria-hidden="true">
      <source media="(max-width: 560px)" srcset={`${ART_DIR}hero-mobile-1080x1350.webp?v=${ART_V}`} />
      <img src={`${ART_DIR}hero-2400x800.webp?v=${ART_V}`} alt="" bind:this={plateImg} decoding="async" fetchpriority="high" onload={() => { artReady = true; }} />
    </picture>
  {/if}
  <div class="sky" aria-hidden="true">
    <span class="moon big"></span>
    <span class="moon small"></span>
    <svg class="dunes" viewBox="0 0 1200 160" preserveAspectRatio="none">
      <path class="far" d="M0 110 C 160 70, 300 90, 420 80 S 700 40, 860 70 S 1100 60, 1200 80 V160 H0 Z" />
      <path class="near" d="M0 140 C 200 110, 380 130, 540 115 S 860 100, 1000 125 S 1150 120, 1200 118 V160 H0 Z" />
    </svg>
  </div>
  <div class="copy">
    <p class="kicker mono">Last Sietch | Halloween event</p>
    <h1>{COPY.event.name}</h1>
    <p class="tag">{COPY.event.tagline}</p>
    <div class="status">
      {#if phase === 'night' && night}
        <span class="chip mono">Night {night.n} of 7</span>
      {:else if phase === 'after'}
        <span class="chip mono">The nights are over</span>
      {:else if phase === 'night'}
        <span class="chip mono">The doors are shut</span>
      {:else}
        <span class="chip mono">Begins {COPY.event.starts_text}</span>
      {/if}
      {#if target && label}
        <span class="cd"><LiveCountdown {target} prefix={label} /></span>
      {/if}
    </div>
  </div>
  <ol class="lanterns" aria-label="The seven nights">
    {#each lanterns as l (l.n)}
      <li class="lantern {l.state}" class:lit={l.earned > 0} title={`Night ${l.n}: ${l.title}`}>
        <span class="flame" aria-hidden="true"></span>
        <span class="n mono">{l.n}</span>
        <span class="sr">Night {l.n}, {l.title}, {l.state === 'past' ? 'past' : l.state === 'tonight' ? 'tonight' : 'still to come'}{l.earned > 0 ? `, ${l.earned} LS Tokens earned` : ''}</span>
      </li>
    {/each}
  </ol>
</header>

<style>
  .hero {
    --sn-sky-0: #05060d; --sn-sky-1: #0e1128; --sn-sky-2: #221a3d;
    --sn-moon: #ece6d3; --sn-moon-glow: rgba(214, 214, 255, .28);
    --sn-dune-far: #17122a; --sn-dune-near: #0b0916;
    position: relative; overflow: hidden; isolation: isolate;
    border: 1px solid color-mix(in srgb, var(--ls-melange-soft) 35%, var(--edge));
    border-radius: var(--radius-md);
    margin-bottom: var(--space-5);
    min-height: 260px;
    padding: var(--space-6) var(--space-5) var(--space-5);
    color: #efe8f7;
    background: var(--sn-sky-0);
  }
  .sky {
    position: absolute; inset: 0; z-index: -1;
    background:
      radial-gradient(1px 1px at 12% 22%, #fff 50%, transparent 51%),
      radial-gradient(1px 1px at 28% 12%, #dcd6ff 50%, transparent 51%),
      radial-gradient(1.5px 1.5px at 44% 30%, #fff 50%, transparent 51%),
      radial-gradient(1px 1px at 58% 8%, #fff 50%, transparent 51%),
      radial-gradient(1px 1px at 66% 26%, #cfc8ff 50%, transparent 51%),
      radial-gradient(1.5px 1.5px at 8% 44%, #fff 50%, transparent 51%),
      radial-gradient(1px 1px at 36% 48%, #fff 50%, transparent 51%),
      radial-gradient(1px 1px at 52% 52%, #e8e2ff 50%, transparent 51%),
      radial-gradient(ellipse at 80% 10%, color-mix(in srgb, var(--ls-melange-soft) 30%, transparent), transparent 55%),
      linear-gradient(180deg, var(--sn-sky-0) 0%, var(--sn-sky-1) 55%, var(--sn-sky-2) 100%);
  }
  .plate { position: absolute; inset: 0; z-index: -1; opacity: 0; transition: opacity var(--motion-slow) var(--ease-out); }
  .plate img { width: 100%; height: 100%; object-fit: cover; object-position: 70% 50%; display: block; }
  .hero.art .plate { opacity: 1; }
  /* Over the plate the CSS sky becomes a scrim: dark on the copy side only. */
  .hero.art .sky { z-index: 0; background: linear-gradient(90deg, rgba(5, 6, 13, .82) 0%, rgba(5, 6, 13, .45) 45%, transparent 75%), linear-gradient(0deg, rgba(5, 6, 13, .7), transparent 40%); }
  .hero.art .moon, .hero.art .dunes { display: none; }
  .moon { position: absolute; border-radius: 50%; background: radial-gradient(circle at 35% 35%, #fffaf0, var(--sn-moon) 55%, #c9c1ad 100%); }
  .moon.big { width: 120px; height: 120px; top: 22px; right: 9%; box-shadow: 0 0 60px 18px var(--sn-moon-glow); animation: drift 18s var(--ease-in-out) infinite alternate; }
  /* The crescent is a disc with the sky's own colour bitten out of it. */
  .moon.small { width: 34px; height: 34px; top: 128px; right: calc(9% + 112px); box-shadow: inset -9px -3px 0 0 var(--sn-moon); background: transparent; opacity: .9; }
  .dunes { position: absolute; left: 0; right: 0; bottom: 0; width: 100%; height: 42%; }
  .dunes .far { fill: var(--sn-dune-far); }
  .dunes .near { fill: var(--sn-dune-near); }
  @keyframes drift { from { transform: translateY(0); } to { transform: translateY(6px); } }

  .copy { position: relative; z-index: 1; max-width: 60ch; }
  .kicker { color: var(--ls-melange-hi); text-transform: uppercase; letter-spacing: .34em; font-size: var(--text-xs); margin: 0 0 var(--space-2); }
  h1 {
    font-size: clamp(2rem, 5.2vw, 3.4rem); line-height: 1.02; text-transform: uppercase; margin: 0 0 var(--space-2);
    text-shadow: 0 2px 18px rgba(0, 0, 0, .7), 0 0 30px color-mix(in srgb, var(--ls-melange) 35%, transparent);
  }
  .tag { margin: 0 0 var(--space-4); color: #cdc3de; }
  .status { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-3); }
  .chip {
    font-size: var(--text-xs); letter-spacing: .14em; text-transform: uppercase;
    padding: 4px 10px; border-radius: 999px;
    border: 1px solid color-mix(in srgb, var(--ls-melange-hi) 55%, transparent);
    color: var(--ls-melange-pale); background: rgba(20, 12, 40, .55);
  }
  /* LiveCountdown's muted prefix sits on a night sky here, not on a panel. */
  .cd :global(.cd-prefix) { color: #cdc3de; }

  .lanterns { position: relative; z-index: 1; list-style: none; margin: var(--space-6) 0 0; padding: 0; display: flex; gap: var(--space-3); flex-wrap: wrap; }
  .lantern { position: relative; width: 34px; height: 46px; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; }
  .flame {
    width: 18px; height: 26px; border-radius: 50% 50% 45% 45% / 60% 60% 40% 40%;
    background: rgba(255, 255, 255, .06); border: 1px solid rgba(220, 210, 255, .25);
  }
  .lantern.tonight .flame { border-color: var(--ls-melange-hi); box-shadow: 0 0 12px var(--ls-melange-glow); }
  .lantern.lit .flame {
    background: radial-gradient(circle at 50% 65%, #fff1c9, #f5a23a 45%, #a4441f 100%);
    border-color: #f5a23a; box-shadow: 0 0 14px rgba(245, 162, 58, .55);
    animation: flicker 3.2s var(--ease-in-out) infinite;
  }
  .lantern.past:not(.lit) .flame { opacity: .45; }
  .n { font-size: 10px; color: #b9aed0; margin-top: 3px; }
  @keyframes flicker { 0%, 100% { opacity: 1; } 45% { opacity: .82; } 55% { opacity: .95; } }
  .sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }

  @media (max-width: 560px) {
    .hero { padding: var(--space-5) var(--space-4) var(--space-4); min-height: 300px; }
    .moon.big { width: 56px; height: 56px; top: 14px; right: 14px; box-shadow: 0 0 34px 10px var(--sn-moon-glow); }
    .moon.small { width: 18px; height: 18px; top: 66px; right: 66px; box-shadow: inset -5px -2px 0 0 var(--sn-moon); }
    .hero:not(.art) .kicker, .hero:not(.art) h1 { padding-right: 64px; }
    .hero.art { min-height: 380px; }
    .hero.art .sky { background: linear-gradient(0deg, rgba(5, 6, 13, .9) 0%, rgba(5, 6, 13, .55) 55%, rgba(5, 6, 13, .2) 100%); }
    .plate img { object-position: 50% 30%; }
    .lanterns { gap: var(--space-2); }
  }
  @media (prefers-reduced-motion: reduce) {
    .moon.big, .lantern.lit .flame { animation: none; }
    .plate { transition: none; }
  }
</style>
