<script>
  // PageHeader: the route hero. Distilled from routes/rewards/+page.svelte, whose
  // kicker / h1 / sub block is copied verbatim across 11 other routes.
  //
  // Route hero ONLY. The 33 in-panel `<p class="kicker mono">` captions are a
  // different thing (a panel caption, not a page header) and are deliberately
  // left alone; wave 3 decides whether they get their own primitive.
  //
  // One <h1> per document, so this must not be used twice on a route. `children`
  // is a trailing controls slot (filters, a switch, an action button).
  let { kicker = '', title = '', sub = '', children } = $props();
</script>

<header class="hero">
  {#if kicker}<p class="kicker mono">{kicker}</p>{/if}
  <h1>{title}</h1>
  {#if sub}<p class="sub">{sub}</p>{/if}
  {@render children?.()}
</header>

<style>
  .hero { margin-bottom: var(--space-5); }
  /* Kicker is chrome: amber, never Ibad blue. */
  .kicker {
    color: var(--accent); text-transform: uppercase; letter-spacing: .34em;
    font-size: var(--text-xs); margin: 0 0 var(--space-2);
  }
  .hero h1 {
    font-size: clamp(2rem, 5.2vw, 3.4rem); line-height: 1.02; letter-spacing: .01em;
    text-transform: uppercase; margin: 0 0 var(--space-3);
  }
  .sub { color: var(--text-muted); max-width: 56ch; margin: 0; }
</style>
