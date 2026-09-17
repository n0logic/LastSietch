<script>
  // Installed augment display for one gear item. Most items carry none (only
  // 279 of the whole DB do), so this renders nothing when `augments` is empty -
  // the parent (EquippedList) decides whether a bare item gets a "no augments"
  // note. `rolls` length VARIES per augment (1-7 normalised stat rolls, 1.0 =
  // perfect); each renders as its own percentage pip so nothing is lost, and a
  // perfect roll is marked with a text tag, never color alone. Effects text
  // sits behind a tap/click disclosure rather than hover - hover doesn't exist
  // on the phones this portal is mostly used on - so 2-3 augments on one weapon
  // never turn the row into a wall of text.
  import { groupRolls } from '$lib/augments.svelte.js';

  let { augments = [] } = $props();

  let list = $derived(Array.isArray(augments) ? augments : []);

  // `label` may already be a curated string ("Scattergun Rampage-Enhancement")
  // or a fall-back raw template id ("T6_Augment_Scattergun6"); the latter is
  // the only case with underscores, so this is enough to make it readable
  // without a translation table.
  function readable(raw) {
    const s = raw || 'Augment';
    return s.includes('_') ? s.replace(/_/g, ' ') : s;
  }
  function pct(roll) {
    return Math.round(Math.max(0, Math.min(1, Number(roll) || 0)) * 100);
  }
</script>

{#if list.length > 0}
  <ul class="augs" role="list">
    {#each list as a, i (i)}
      {@const label = readable(a.label || a.name)}
      {@const rolls = Array.isArray(a.rolls) ? a.rolls : []}
      {@const effects = Array.isArray(a.effects) ? a.effects : []}
      <li class="aug">
        {#if effects.length > 0}
          <details class="chip">
            <summary>
              <span class="glyph" aria-hidden="true">&#9670;</span>
              <span class="label">{label}</span>
              {#if a.grade != null}<span class="grade mono" title="Augment grade {a.grade} of 5">G{a.grade}</span>{/if}
              <span class="rolls">
                {#each groupRolls(rolls) as g, ri (ri)}
                  <span class="roll mono" class:perfect={g.value >= 1}
                        title="{g.count} roll{g.count === 1 ? '' : 's'} at {pct(g.value)}%">
                    {#if g.value >= 1}<span class="tag">Perfect</span>{/if}
                    <span class="num">{pct(g.value)}%</span>
                    {#if g.count > 1}<span class="mult">&times;{g.count}</span>{/if}
                  </span>
                {/each}
              </span>
              <span class="chev" aria-hidden="true">&#8250;</span>
            </summary>
            <ul class="effects" role="list">
              {#each effects as e, ei (ei)}
                <!-- `resolved` is the stat's ACTUAL value at this item's roll,
                     computed server-side and present only when the augment has a
                     single effect. Multi-effect augments never carry one: the
                     game's roll order is not the catalogue's display order, so a
                     per-stat number there would be confidently wrong. Those keep
                     showing the range, which is true at any order. -->
                <li>{a.resolved || e}</li>
              {/each}
            </ul>
          </details>
        {:else}
          <div class="chip static">
            <span class="glyph" aria-hidden="true">&#9670;</span>
            <span class="label">{label}</span>
            {#if a.grade != null}<span class="grade mono" title="Augment grade {a.grade} of 5">G{a.grade}</span>{/if}
            <span class="rolls">
              {#each groupRolls(rolls) as g, ri (ri)}
                <span class="roll mono" class:perfect={g.value >= 1}
                      title="{g.count} roll{g.count === 1 ? '' : 's'} at {pct(g.value)}%">
                  {#if g.value >= 1}<span class="tag">Perfect</span>{/if}
                  <span class="num">{pct(g.value)}%</span>
                  {#if g.count > 1}<span class="mult">&times;{g.count}</span>{/if}
                </span>
              {/each}
            </span>
          </div>
        {/if}
      </li>
    {/each}
  </ul>
{/if}

<style>
  .augs { list-style: none; margin: var(--space-1) 0 0; padding: 0; display: flex; flex-direction: column; gap: 3px; }
  .aug { min-width: 0; }

  .chip {
    background: color-mix(in srgb, var(--accent) 7%, var(--metal-0));
    border: 1px solid var(--edge); border-radius: var(--radius-sm);
  }
  .chip.static { padding: 3px var(--space-2); display: flex; align-items: center; gap: var(--space-1); flex-wrap: wrap; }

  /* The chevron is taken OUT of the flex flow (absolute, pinned to the first
     line) rather than left as a trailing flex item. As a sibling it competes
     for room on the wrapped rolls line, and a 6-7 roll augment pushes it onto
     a line of its own where a bare ">" reads as a stray glyph. */
  .chip summary {
    position: relative;
    display: flex; align-items: center; gap: var(--space-1); flex-wrap: wrap;
    padding: 3px calc(var(--space-2) + 10px) 3px var(--space-2);
    cursor: pointer; list-style: none;
  }
  .chip summary::-webkit-details-marker { display: none; }

  .glyph { color: var(--accent); font-size: 9px; flex: 0 0 auto; }
  /* Does NOT grow: growing pushed the grade and the roll chips out to the far
     right edge, leaving a wide dead span between an augment's name and its own
     rolls on a desktop-width card. Shrink is kept so a long name still
     ellipsizes on a phone instead of forcing the chips to wrap early. */
  .label {
    font-size: var(--text-xs); color: var(--text); min-width: 0;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 0 1 auto;
  }
  .grade { font-size: var(--text-xs); color: var(--accent-text); flex: 0 0 auto; }

  /* MUST stay shrinkable. With `flex: 0 0 auto` this box is pinned to its
     max-content width, so a weapon carrying 5+ rolls (Lmg1 has 6, Spitdart 7)
     overflowed the card instead of wrapping. Shrink-to-fit lets the inner
     flex-wrap do its job. No `min-width: 0` on purpose: the automatic
     min-content floor is one whole chip, which is exactly the floor we want -
     zeroing it would just move the overflow inside this box. */
  .rolls { display: flex; gap: 3px; flex-wrap: wrap; flex: 0 1 auto; }
  .roll {
    display: inline-flex; align-items: center; gap: 3px; white-space: nowrap;
    font-size: var(--text-xs); color: var(--text-muted);
    background: var(--metal-1); border: 1px solid var(--edge);
    border-radius: 2px; padding: 0 4px; line-height: 1.5;
  }
  /* Perfect (1.0) roll: never hue-only - the fill color is always paired with
     the "Perfect" text tag and the 100% numeral. */
  .roll.perfect {
    color: var(--bg-deep); background: var(--ls-melange); border-color: var(--ls-melange);
    font-weight: 700;
  }
  .roll .tag { text-transform: uppercase; letter-spacing: .04em; }
  /* Run count, e.g. "PERFECT 100% x6". Slightly dimmed so the value still reads
     first; on a perfect chip the fill is dark-on-light, so dim differently. */
  .roll .mult { opacity: .75; font-weight: 400; }
  .roll.perfect .mult { opacity: .65; }

  .chev {
    position: absolute; right: var(--space-2); top: 3px;
    color: var(--text-muted); font-size: var(--text-sm); line-height: 1.5;
    transition: transform var(--motion-fast) var(--ease-out);
  }
  details[open] > summary .chev { transform: rotate(90deg); }

  .effects {
    list-style: none; margin: 0; padding: 0 var(--space-2) var(--space-2) var(--space-5);
    display: flex; flex-direction: column; gap: 2px;
  }
  .effects li { font-size: var(--text-xs); color: var(--text-muted); }

  @media (prefers-reduced-motion: reduce) {
    .chev { transition: none; }
  }
</style>
