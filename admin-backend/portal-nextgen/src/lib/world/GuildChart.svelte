<script>
  // Guild chart: the flat, no-WebGL view of the Sand Table (spec section 8). Same
  // stones, same states, same cards. SVG in overmap cm (X east, Y south), the art
  // underlay placed by the blueprint transform only (overmap-art.json worldRectCm).
  import { worldUrl, stoneStatus, STATUS_TEXT, cardModel, cycle, stoneBands } from './data.js';
  import { base } from '$app/paths';
  import { mapInstances } from '$lib/mapInstances.svelte.js';
  import { untrackedBandKeys } from '$lib/mapInstances.js';
  import InstrumentCard from './InstrumentCard.svelte';

  let { world, status = null, bands = null, anchorId = null, selectedId = $bindable(null), onopen } = $props();
  let untracked = $derived(untrackedBandKeys(mapInstances.list));

  // Content bounds from the contours + stones, padded; the art rect is mostly empty sky.
  let box = $derived.by(() => {
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    for (const l of world.contours.lines) {
      for (let i = 0; i < l.length; i += 2) {
        if (l[i] < x0) x0 = l[i]; if (l[i] > x1) x1 = l[i];
        if (l[i + 1] < y0) y0 = l[i + 1]; if (l[i + 1] > y1) y1 = l[i + 1];
      }
    }
    for (const s of world.stones) {
      x0 = Math.min(x0, s.x); x1 = Math.max(x1, s.x); y0 = Math.min(y0, s.y); y1 = Math.max(y1, s.y);
    }
    const px = (x1 - x0) * 0.05, py = (y1 - y0) * 0.06;
    return { x: x0 - px, y: y0 - py, w: x1 - x0 + 2 * px, h: y1 - y0 + 2 * py };
  });
  let paths = $derived(world.contours.lines.map((l) => {
    let d = 'M' + l[0] + ' ' + l[1];
    for (let i = 2; i < l.length; i += 2) d += 'L' + l[i] + ' ' + l[i + 1];
    return d;
  }).join(''));

  let svgEl = $state(null);
  let cw = $state(900);
  let stageEl = $state(null);
  let anchor = $state(null);
  let bounds = $state({ w: 900, h: 600 });
  // one screen pixel in cm, so markers keep a constant on-screen size
  let u = $derived(box.w / Math.max(320, cw));
  const art = $derived(world.art.worldRectCm);

  let model = $derived.by(() => {
    const s = world.stones.find((x) => x.id === selectedId);
    return s ? cardModel(s, status, bands, untracked) : null;
  });

  function select(id, el) {
    selectedId = id;
    if (id && el && stageEl) {
      const r = el.getBoundingClientRect(), s = stageEl.getBoundingClientRect();
      anchor = { x: r.left - s.left + r.width / 2, y: r.top - s.top + r.height / 2 };
      bounds = { w: s.width, h: s.height };
    } else anchor = null;
  }
  function onKey(e) {
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown' || e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
      e.preventDefault();
      const id = cycle(world.stones, selectedId, e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : -1);
      select(id, stageEl && stageEl.querySelector(`[data-stone="${id}"]`));
      stageEl && stageEl.querySelector(`[data-stone="${id}"]`)?.focus();
    } else if (e.key === 'Enter' && selectedId) {
      const s = world.stones.find((x) => x.id === selectedId);
      if (s && s.live) { e.preventDefault(); onopen && onopen(s, null); }
    } else if (e.key === 'Escape') select(null, null);
  }
  function stoneClass(s) {
    const st = stoneStatus(s, status);
    return st ? 'st-' + st : 'st-none';
  }
  const r = (s) => (s.live ? 15 : s.kind === 'dungeon' ? 9 : 10) * (cw < 560 ? 0.6 : 1);
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<div class="stage" bind:this={stageEl} bind:clientWidth={cw} role="group" aria-label="Guild chart of Arrakis. Arrow keys move between locations, Enter opens a map, Escape clears." onkeydown={onKey}>
  <svg bind:this={svgEl} viewBox="{box.x} {box.y} {box.w} {box.h}" preserveAspectRatio="xMidYMid meet" role="presentation">
    <image href={worldUrl(base, 'overmap-art-2048.webp', world.stamp)} x={art.x0} y={art.y0} width={art.x1 - art.x0} height={art.y1 - art.y0} opacity=".55" preserveAspectRatio="none" />
    <path d={paths} class="contour" vector-effect="non-scaling-stroke" />
    {#each world.stones as s (s.id)}
      {@const rr = r(s) * u}
      <g class="stone {stoneClass(s)}" class:sel={selectedId === s.id} class:anchor={anchorId === s.id}
        transform="translate({s.x} {s.y})" data-stone={s.id} role="button" tabindex={selectedId === s.id || (!selectedId && s === world.stones[0]) ? 0 : -1}
        aria-label="{s.name}{stoneStatus(s, status) ? ', ' + STATUS_TEXT[stoneStatus(s, status)] : ''}"
        onpointerenter={(e) => select(s.id, e.currentTarget)} onpointerleave={() => select(null, null)}
        onfocus={(e) => select(s.id, e.currentTarget)}
        onclick={() => { if (s.live) onopen && onopen(s, null); }}
       >
        <circle r={rr * 2.4} class="hit" />
        <circle r={rr * 1.5} class="disc" />
        <circle r={rr * 1.5} class="ring" vector-effect="non-scaling-stroke" />
        {#if stoneStatus(s, status) === 'on-demand'}
          <path class="glyph" d="M{-rr * .55} 0H{rr * .55}M{rr * .1} {-rr * .45}L{rr * .6} 0L{rr * .1} {rr * .45}" vector-effect="non-scaling-stroke" />
        {:else}
          <circle r={rr * (s.live ? .62 : .42)} class="core" />
        {/if}
        {#if stoneStatus(s, status) === 'blocked'}<path class="bar" d="M{-rr * 1.5} 0H{rr * 1.5}" vector-effect="non-scaling-stroke" />{/if}
        {#each stoneBands(s, bands, untracked).filter((b) => b.band) as b, bi}
          {#each [0, 1, 2] as k}
            {@const n = s.facets.length || 1}
            {@const a = (-90 + (360 / n) * bi + (k - 1) * (n > 1 ? 22 : 40)) * Math.PI / 180}
            <circle cx={Math.cos(a) * rr * 2} cy={Math.sin(a) * rr * 2} r={rr * .24} class="mark" class:lit={k < b.band.marks} vector-effect="non-scaling-stroke" />
          {/each}
        {/each}
        {#if anchorId === s.id}<path class="you" d="M0 {-rr * 2.4}l{rr * .5} {-rr * .9}h{-rr}z" />{/if}
        {#if s.label && (s.label.style === 'gold3d' || s.live)}
          <text class="lab" y={-rr * 2.7} font-size={15 * u} text-anchor="middle">{s.name.toUpperCase()}</text>
        {/if}
      </g>
    {/each}
  </svg>
  <InstrumentCard {model} {anchor} {bounds} onopen={onopen} />
</div>

<style>
  .stage { position: relative; width: 100%; height: 100%; min-height: 380px; background: radial-gradient(ellipse at 50% 40%, #1b150e, var(--bg-deep, #0c0a08) 75%); overflow: hidden; border: 1px solid var(--edge); }
  svg { width: 100%; height: 100%; display: block; }
  .contour { fill: none; stroke: #e4b878; stroke-opacity: .5; stroke-width: 1; }
  .stone { cursor: default; outline: none; }
  .stone[data-stone] { transition: opacity .15s; }
  .hit { fill: transparent; }
  .disc { fill: #3a1f2c; stroke: none; }
  .ring { fill: none; stroke: #8c6a2f; stroke-width: 1.5; }
  .core { fill: #5b4a36; }
  .glyph { fill: none; stroke: #e0c9a0; stroke-width: 1.6; stroke-linecap: round; stroke-linejoin: round; }
  .bar { stroke: #d2693a; stroke-width: 3; }
  .st-up .ring { stroke: var(--accent-bright, #f5a23a); stroke-width: 2.5; filter: drop-shadow(0 0 5px var(--accent-glow, rgba(245,162,58,.6))); }
  .st-up .core { fill: var(--accent-bright, #f5a23a); animation: pulse 3.2s ease-in-out infinite; }
  .st-down .ring { stroke: #3a352e; }
  .st-down .core { fill: #26221d; }
  .st-down .disc { fill: #1d1218; }
  .st-on-demand .ring { stroke: #8c6a2f; stroke-dasharray: 3 3; }
  .st-on-demand .disc { opacity: .7; }
  .st-none .ring { stroke: #6a5530; stroke-width: 1; }
  .st-none .core { fill: #8a7350; }
  .sel .ring, .stone:focus-visible .ring { stroke: #fff2d4; stroke-width: 3; }
  .mark { fill: #14100b; stroke: #8c6a2f; stroke-width: 1; }
  .mark.lit { fill: var(--accent-bright, #f5a23a); stroke: var(--accent-bright, #f5a23a); }
  .you { fill: var(--ls-ibad, #3fb6c9); }
  .lab { fill: #f0c978; font-family: 'Saira Condensed', sans-serif; font-weight: 600; letter-spacing: .16em; paint-order: stroke; stroke: #0c0a08; stroke-width: 3px; stroke-linejoin: round; pointer-events: none; }
  @keyframes pulse { 50% { opacity: .55; } }
  @media (prefers-reduced-motion: reduce) { .st-up .core { animation: none; } }
</style>
