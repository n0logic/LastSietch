<script>
  // Carved-slab panel: oxidized metal plate lit from above, with etched corner
  // brackets. `sharp` for live HUD instruments (sharp corners); `live` tints the
  // brackets Ibad blue (streaming data); `hot` lifts the edge to an accent glow.
  let { elevation = 1, live = false, sharp = false, hot = false, children } = $props();
</script>

<section class="slab carved elev-{elevation}" class:live class:sharp class:hot>
  {@render children?.()}
</section>

<style>
  .slab {
    position: relative;
    background: var(--panel);
    border: 1px solid var(--edge);
    border-radius: var(--radius-md);
    padding: var(--space-5);
    box-shadow:
      inset 0 1px 0 var(--metal-hi),
      inset 0 0 0 1px rgba(0, 0, 0, .45),
      0 18px 44px -28px var(--shadow-cast);
    transition:
      transform var(--motion-fast) var(--ease-out),
      border-color var(--motion-fast) var(--ease-out),
      box-shadow var(--motion-fast) var(--ease-out);
  }
  .slab.sharp { border-radius: var(--radius-sm); }

  /* Console panels respond to attention: a slight lift + brighter edge + a top
     sheen, brackets fully resolve. transform/opacity only (GPU-friendly). */
  .slab:hover {
    transform: none;
    border-color: var(--edge-hi);
    box-shadow:
      inset 0 1px 0 var(--metal-hi),
      inset 0 0 0 1px rgba(0, 0, 0, .45),
      0 24px 52px -26px var(--shadow-cast);
  }
  .slab:hover::before, .slab:hover::after { opacity: 1; }

  /* Etched L-brackets at opposing corners (the diegetic frame cue). */
  .slab::before,
  .slab::after {
    content: '';
    position: absolute;
    width: 13px; height: 13px;
    border: 1.5px solid var(--edge-hi);
    opacity: .7;
    pointer-events: none;
    transition: opacity var(--motion-fast) var(--ease-out);
  }
  .slab::before { top: 7px; left: 7px; border-right: 0; border-bottom: 0; }
  .slab::after { bottom: 7px; right: 7px; border-left: 0; border-top: 0; }
  /* Brackets are CHROME -> always amber. Ibad blue is reserved for live data
     values (LiveDot, streaming numbers), never the frame. */

  .elev-2 { box-shadow: inset 0 1px 0 var(--metal-hi), inset 0 0 0 1px rgba(0,0,0,.45), 0 14px 34px -18px #000; }
  .elev-3 { box-shadow: inset 0 1px 0 var(--metal-hi), var(--shadow-overlay); }

  /* Active-event panel: a restrained accent-tinted edge + brighter brackets mark
     "something is happening here" (live spice blow, enraged worms, active storm).
     The glow is reserved for hover so the resting state stays calm. */
  .slab.hot {
    border-color: color-mix(in srgb, var(--accent) 36%, var(--edge));
  }
  .slab.hot:hover {
    transform: none;
    border-color: color-mix(in srgb, var(--accent) 55%, var(--edge));
    box-shadow:
      inset 0 1px 0 var(--metal-hi),
      0 0 18px -4px var(--accent-glow),
      0 24px 52px -26px var(--shadow-cast);
  }
  .slab.hot::before, .slab.hot::after { border-color: var(--accent); opacity: .85; }
</style>
