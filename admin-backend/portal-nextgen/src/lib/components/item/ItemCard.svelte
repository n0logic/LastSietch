<script>
  // In-game-style item inspection card: header (category / name / quality
  // diamond), rarity-tinted art panel with an augment-slot glyph row,
  // durability bar, description, base stats, and installed augments.
  //
  // Every section below is INDEPENDENTLY optional and renders nothing when
  // absent, rather than a fixed template with blanks - confirmed against
  // five-plus real in-game variants (weapon, armor, gathering tool, utility
  // tool, misc/fuel): a raw resource shows description only, no diamond, no
  // durability, no stats; an armor piece can run nine stat rows deep.
  //
  // The bottom-left "NV" marker seen on every in-game card is item VOLUME
  // (confirmed against the backpack's own "V 237/397" capacity readout),
  // NOT tier as first assumed. We have no per-item volume figure anywhere
  // in our data (only a container's aggregate max_item_volume), so it is
  // deliberately omitted rather than guessed.
  //
  // Pure display: no fetch of the live item itself (the caller already has
  // it), only a lookup into the static gear-stats.json catalogue for
  // description/stats/rarity. Normalizes the two shapes items arrive in
  // today - storage (`_v2_item()`: quality/cur_dur/max_dur nested under
  // `durability`) and Character equipped (same fields top-level). Both
  // already agree on `template`, `name`, `icon`, `augments`.
  import { iconUrl } from '$lib/icons.js';
  import { gearStatsFor } from './gearStats.js';
  import { groupRolls } from '$lib/augments.svelte.js';

  let { item } = $props();

  let templateId = $derived(item?.template || item?.template_id || '');
  let name = $derived(item?.name || templateId || 'Item');
  let icon = $derived(iconUrl(item?.icon));
  let quality = $derived.by(() => {
    const q = item?.quality ?? item?.durability?.quality;
    return q == null || q === '' ? null : Number(q);
  });
  let durCur = $derived(item?.durability?.cur ?? item?.cur_dur ?? '');
  let durMax = $derived(item?.durability?.max ?? item?.max_dur ?? '');
  let hasDurability = $derived(durCur !== '' && durMax !== '' && Number(durMax) > 0);
  let durPct = $derived(hasDurability ? Math.round((Number(durCur) / Number(durMax)) * 100) : 0);
  let augments = $derived(Array.isArray(item?.augments) ? item.augments : []);

  // Static catalogue lookup, keyed off the live item's template id. Guarded
  // against a stale response landing after the caller swapped `item` mid-flight
  // (hovering fast across a grid).
  let gear = $state(null);
  let gearLoaded = $state(false);
  $effect(() => {
    const tid = templateId;
    gearLoaded = false;
    gear = null;
    if (!tid) { gearLoaded = true; return; }
    gearStatsFor(tid).then((g) => {
      if (tid !== templateId) return;
      gear = g;
      gearLoaded = true;
    });
  });

  // Unique = --ls-melange, CONFIRMED: sampled the actual pixel colour off two
  // reference screenshots (Bulwark Leggings, Power Harness), both a muted
  // slate-violet (#65528f dominant) - close enough in hue to our melange
  // family that reusing the existing token (rather than a one-off hex) reads
  // as the same colour. Memento has no reference screenshot tonight; the
  // gold-ish placeholder below is a guess, kept off --accent deliberately so
  // a Memento item never looks like the portal's own chrome.
  //
  // The game ALSO tints the art panel by item CATEGORY independent of
  // rarity (blue for "Utility Tools", orange for "Hydration Tools", neutral
  // for "Raw Resources"/"Misc") - confirmed across 7+ screenshots tonight.
  // We do not reproduce that: storage items carry no category/subcategory
  // field (only Character's equipped payload gets one, server-side), so
  // guessing a type->colour map from a handful of screenshots would be
  // fabricating a system we cannot actually key off live data. Rarity is
  // the one axis we have real data for.
  const RARITY_COLOR = {
    Common: 'var(--text-muted)',
    Unique: 'var(--ls-melange-hi)',
    Memento: 'var(--ls-yellow)',
  };
  const RARITY_TINT = {
    Common: 'var(--edge)',
    Unique: 'var(--ls-melange)',
    Memento: 'var(--ls-yellow)',
  };
  // Category line. `gear.type` is the precise catalogue type ("Assault Rifle")
  // but gear-stats.json covers 858 of several thousand template ids, so most
  // items had no header line at all. `item.category` is the server's coarse
  // market_categories.classify() slug, resolved for EVERY template, and now
  // sent on both the storage and equipped payloads - so it backstops the
  // catalogue rather than replacing it. "other" is classify()'s catch-all, not
  // a real category, so it renders nothing rather than the word "OTHER".
  const CATEGORY_LABEL = {
    garments: 'Garments', weapons: 'Weapons', tools: 'Tools',
    resources: 'Resources', vehicles: 'Vehicles', augmentations: 'Augments',
    building: 'Building',
  };
  let categoryLine = $derived(
    gear?.type || CATEGORY_LABEL[item?.category] || null
  );

  let rarity = $derived(gear?.rarity || null);
  let rarityColor = $derived(RARITY_COLOR[rarity] || 'var(--text-muted)');
  let rarityTint = $derived(RARITY_TINT[rarity] || 'var(--edge)');

  // Augment SLOT CAP by coarse item type: a documented game rule (clothing
  // <=2, weapons <=3 - see scripts/dune-augment.py), confirmed against two
  // in-game armor screenshots (both showed exactly 2 slot glyphs). Not a
  // live-read field, so this is used ONLY to draw the slot-capacity glyph
  // row and never to gate anything. Tools/unmapped types show installed
  // augments only, no guessed capacity.
  const SLOT_CAP = { weapon: 3, armor: 2 };
  let slotCap = $derived(gear?.type ? (SLOT_CAP[gear.type] ?? null) : null);
  let emptySlots = $derived(slotCap != null ? Math.max(0, slotCap - augments.length) : 0);

  function readableAug(a) {
    const s = a.label || a.name || 'Augment';
    return s.includes('_') ? s.replace(/_/g, ' ') : s;
  }
  function pct(roll) {
    return Math.round(Math.max(0, Math.min(1, Number(roll) || 0)) * 100);
  }

  // Splits the description on the FIRST case-insensitive occurrence of the
  // item's own rarity word, so it can render highlighted inline the way the
  // in-game card does. Only fires when the word actually appears in the
  // text - most descriptions do not name their own rarity, and this never
  // fabricates a highlight.
  //
  // The in-game card ALSO highlights ORIGIN words this way ("Fremen" in
  // orange, "Old Imperial" in blue) - a whole system we do not reproduce:
  // there is no origin/faction field in our data, only `rarity`, so we would
  // be guessing both which words to scan for and what colour to give them.
  // Only the one word we actually have ground truth for gets highlighted.
  let descParts = $derived.by(() => {
    const text = gear?.description || '';
    if (!text || text === 'WIP' || !rarity) return text ? [{ text, hi: false }] : [];
    const idx = text.toLowerCase().indexOf(rarity.toLowerCase());
    if (idx === -1) return [{ text, hi: false }];
    return [
      { text: text.slice(0, idx), hi: false },
      { text: text.slice(idx, idx + rarity.length), hi: true },
      { text: text.slice(idx + rarity.length), hi: false },
    ].filter((p) => p.text !== '');
  });

  let statEntries = $derived.by(() => {
    const stats = gear?.stats || {};
    const units = gear?.units || {};
    return Object.entries(stats).map(([k, v]) => ({
      label: k, value: v, unit: units[k] || '',
      // The in-game panel prefixes a POSITIVE percentage modifier with "+"
      // (negative ones already carry their own "-"); plain magnitudes like
      // Armor Value never get a sign. `Number()` on a non-numeric stat
      // (none observed yet, but the live per-item panel does show word
      // values like "Extreme") safely yields NaN, so this no-ops for those.
      sign: units[k] === '%' && Number(v) > 0 ? '+' : '',
    }));
  });
</script>

<div class="card" style="--rarity: {rarityColor}; --rarity-tint: {rarityTint};">
  <header class="head">
    <div class="head-text">
      {#if categoryLine}<p class="cat mono">{categoryLine.toUpperCase()}</p>{/if}
      <h3 class="name">{name}</h3>
    </div>
    {#if quality != null}
      <div class="grade" title="Tier {quality}">
        <span class="grade-num mono">{quality}</span>
      </div>
    {/if}
  </header>

  <div class="art">
    <img src={icon} alt="" aria-hidden="true" loading="lazy" />
    {#if augments.length > 0 || emptySlots > 0}
      <div class="art-foot" aria-label="{augments.length} of {augments.length + emptySlots} augment slots used">
        {#each augments as a, i (i)}<span class="slot filled" aria-hidden="true"></span>{/each}
        {#each Array(emptySlots) as _, i (i)}<span class="slot" aria-hidden="true"></span>{/each}
      </div>
    {/if}
  </div>

  {#if rarity}
    <p class="rarity-line">
      <span class="rarity-dot" aria-hidden="true"></span>
      <span class="rarity-word">{rarity}</span>
    </p>
  {/if}

  {#if hasDurability}
    <div class="dur">
      <span class="dur-glyph" aria-hidden="true">&#9881;</span>
      <div class="dur-bar" role="meter" aria-valuenow={durPct} aria-valuemin="0" aria-valuemax="100" aria-label="Durability">
        <span class="dur-fill" style="width:{durPct}%"></span>
      </div>
      <span class="dur-txt mono">{durCur} ({durMax})</span>
    </div>
  {/if}

  {#if !gearLoaded}
    <p class="hint">Loading item data&hellip;</p>
  {:else if !gear}
    <p class="hint">No catalogued stats for this item yet.</p>
  {:else}
    {#if descParts.length > 0}
      <p class="desc">
        {#each descParts as p, i (i)}{#if p.hi}<span class="desc-hi">{p.text}</span>{:else}{p.text}{/if}{/each}
      </p>
    {/if}

    {#if statEntries.length > 0}
      <div class="stats">
        <p class="stats-note mono">Base values, unscaled by item quality</p>
        <ul class="stat-list">
          {#each statEntries as s (s.label)}
            <li><span class="stat-label">{s.label}</span><span class="stat-val mono">{s.sign}{s.value}{s.unit}</span></li>
          {/each}
        </ul>
      </div>
    {/if}
  {/if}

  {#if augments.length > 0}
    <div class="augs-block">
      <p class="augs-kicker mono">Installed augments</p>
      <ul class="augs" role="list">
        {#each augments as a, i (i)}
          {@const rolls = Array.isArray(a.rolls) ? a.rolls : []}
          <li class="aug">
            <div class="aug-head">
              <span class="glyph" aria-hidden="true">&#9670;</span>
              <span class="aug-label">{readableAug(a)}</span>
              {#if a.grade != null}<span class="aug-grade mono" title="Augment grade {a.grade} of 5">G{a.grade}</span>{/if}
            </div>
            {#if rolls.length > 0}
              <div class="rolls">
                {#each groupRolls(rolls) as g, ri (ri)}
                  <span class="roll mono" class:perfect={g.value >= 1}
                        title="{g.count} roll{g.count === 1 ? '' : 's'} at {pct(g.value)}%">
                    {#if g.value >= 1}<span class="tag">Perfect</span>{/if}
                    <span class="num">{pct(g.value)}%</span>
                    {#if g.count > 1}<span class="mult">&times;{g.count}</span>{/if}
                  </span>
                {/each}
              </div>
            {/if}
            {#if Array.isArray(a.effects) && a.effects.length > 0}
              <ul class="effects">
                {#each a.effects as e, ei (ei)}<li>{e}</li>{/each}
              </ul>
            {/if}
          </li>
        {/each}
      </ul>
    </div>
  {/if}
</div>

<style>
  .card {
    display: flex; flex-direction: column; gap: var(--space-3);
    width: min(320px, 86vw); max-height: min(80vh, 640px); overflow-y: auto;
    font-family: var(--font-sans); color: var(--text);
    background: var(--panel); border: 1px solid var(--edge); border-radius: var(--radius-md);
    padding: var(--space-4); box-shadow: var(--shadow-overlay);
  }

  .head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-2); }
  .head-text { min-width: 0; }
  .cat { margin: 0 0 2px; font-size: var(--text-xs); color: var(--accent); text-transform: uppercase; letter-spacing: .18em; }
  .name {
    margin: 0; font-family: var(--font-display); font-size: var(--text-lg); line-height: 1.15;
    color: var(--accent-text); text-transform: uppercase; letter-spacing: .01em; overflow-wrap: break-word;
  }
  /* Chamfered diamond badge, tinted by rarity - one of the three redundant
     rarity cues alongside the word below and the colour dot next to it. */
  .grade {
    flex: 0 0 auto; width: 30px; height: 30px; margin-top: 2px; display: grid; place-items: center;
    background: var(--rarity-tint); transform: rotate(45deg);
    border: 1px solid color-mix(in srgb, var(--rarity) 55%, var(--edge));
    border-radius: 4px;
  }
  .grade-num { transform: rotate(-45deg); font-size: var(--text-sm); font-weight: 700; color: var(--bg-deep); }

  .art {
    position: relative; aspect-ratio: 4 / 3; border-radius: var(--radius-sm); overflow: hidden;
    background:
      radial-gradient(120% 100% at 50% 10%, color-mix(in srgb, var(--rarity-tint) 38%, transparent) 0%, transparent 62%),
      linear-gradient(180deg, color-mix(in srgb, var(--rarity-tint) 20%, var(--metal-1)), var(--metal-0));
    border: 1px solid color-mix(in srgb, var(--rarity) 32%, var(--edge));
    display: grid; place-items: center;
  }
  .art img { width: 60%; height: 60%; object-fit: contain; filter: drop-shadow(0 6px 10px rgba(0, 0, 0, .5)); }
  .art-foot { position: absolute; right: var(--space-2); bottom: var(--space-2); display: flex; gap: 5px; }
  /* One ringed dot per augment slot (capacity), brighter for a slot that
     actually has an augment installed. Colour sampled directly off two
     reference screenshots (a muted green-teal, #315a4a dominant) rather than
     guessed - it is closer to our own --ls-green than to Ibad blue, and
     using it keeps the Ibad reservation for live data intact. Circular to
     match the reference; earlier drafts used a rotated-diamond shape before
     the screenshots came in and were wrong. */
  .slot { width: 10px; height: 10px; border-radius: 50%; border: 1px solid var(--ls-green); background: color-mix(in srgb, var(--ls-green) 55%, transparent); }
  .slot.filled { background: var(--ls-green); box-shadow: 0 0 4px color-mix(in srgb, var(--ls-green) 55%, transparent); }

  .rarity-line { margin: 0; display: flex; align-items: center; gap: var(--space-1); }
  .rarity-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--rarity); flex: 0 0 auto; }
  .rarity-word { font-size: var(--text-xs); color: var(--rarity); text-transform: uppercase; letter-spacing: .12em; font-weight: 700; }

  .dur { display: flex; align-items: center; gap: var(--space-2); }
  .dur-glyph { color: var(--accent); font-size: var(--text-sm); flex: 0 0 auto; }
  .dur-bar { flex: 1; min-width: 0; height: 6px; border-radius: 3px; background: color-mix(in srgb, var(--edge) 60%, transparent); overflow: hidden; }
  .dur-fill { display: block; height: 100%; background: var(--accent); }
  .dur-txt { font-size: var(--text-xs); color: var(--text-muted); flex: 0 0 auto; white-space: nowrap; }

  .hint { margin: 0; font-size: var(--text-xs); color: var(--text-muted); }
  .desc { margin: 0; font-size: var(--text-sm); color: var(--text-muted); line-height: 1.4; }
  .desc-hi { color: var(--rarity); font-weight: 700; }

  .stats-note { margin: 0 0 4px; font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .06em; }
  /* Flat single-column label/value list, one row per stat - matches every
     in-game variant observed (armor, tools); it is never a fixed 2-column
     grid or bars, since we do not know the true max/scale a bar would need. */
  .stat-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; }
  .stat-list li { display: flex; justify-content: space-between; gap: var(--space-3); font-size: var(--text-sm); border-bottom: 1px solid color-mix(in srgb, var(--edge) 60%, transparent); padding: 4px 0; }
  .stat-label { color: var(--text-muted); }
  .stat-val { color: var(--text); }

  .augs-kicker { margin: 0 0 4px; font-size: var(--text-xs); color: var(--accent); text-transform: uppercase; letter-spacing: .12em; }
  .augs { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
  .aug { background: color-mix(in srgb, var(--accent) 7%, var(--metal-0)); border: 1px solid var(--edge); border-radius: var(--radius-sm); padding: 4px var(--space-2); }
  .aug-head { display: flex; align-items: center; gap: var(--space-1); flex-wrap: wrap; }
  .glyph { color: var(--accent); font-size: 9px; flex: 0 0 auto; }
  .aug-label { font-size: var(--text-xs); color: var(--text); flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .aug-grade { font-size: var(--text-xs); color: var(--accent-text); flex: 0 0 auto; }
  .rolls { display: flex; gap: 3px; flex-wrap: wrap; margin-top: 3px; }
  .roll { display: inline-flex; align-items: center; gap: 3px; font-size: var(--text-xs); color: var(--text-muted); background: var(--metal-1); border: 1px solid var(--edge); border-radius: 2px; padding: 0 4px; line-height: 1.5; }
  /* Perfect (1.0) roll: never hue-only - paired with the "Perfect" text tag
     and the 100% numeral. */
  .roll.perfect { color: var(--bg-deep); background: var(--ls-melange); border-color: var(--ls-melange); font-weight: 700; }
  .roll .tag { text-transform: uppercase; letter-spacing: .04em; }
  .roll .mult { opacity: .75; font-weight: 400; }
  .roll.perfect .mult { opacity: .65; }
  .effects { list-style: none; margin: 3px 0 0; padding: 0 0 0 var(--space-3); display: flex; flex-direction: column; gap: 1px; }
  .effects li { font-size: var(--text-xs); color: var(--text-muted); }
</style>
