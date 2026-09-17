<script>
  // Plain-language one-liner shown directly under a section header so a first-time
  // player knows what the section is for. ALWAYS visible (inline, not a hover
  // tooltip): the portal is a touch PWA where hover does not exist. An optional
  // `detail` adds a small "?" affordance that expands one extra line of context.
  let { text, detail = '' } = $props();
  let open = $state(false);
</script>

<p class="eli5">
  {text}
  {#if detail}
    <button
      class="q mono"
      type="button"
      aria-expanded={open}
      aria-label="More detail"
      onclick={() => (open = !open)}
    >?</button>
  {/if}
</p>
{#if detail && open}
  <p class="eli5-detail" role="note">{detail}</p>
{/if}

<style>
  .eli5 {
    margin: 0 0 var(--space-3);
    color: var(--text-muted);
    font-size: var(--text-sm);
    line-height: 1.45;
  }
  .q {
    display: inline-flex; align-items: center; justify-content: center;
    width: 1.2rem; height: 1.2rem; margin-left: var(--space-1);
    font-size: var(--text-xs); line-height: 1; vertical-align: middle;
    color: var(--text-muted); background: var(--bg-elevated);
    border: 1px solid var(--edge); border-radius: 50%; cursor: pointer;
    transition: border-color var(--motion-fast) var(--ease-out), color var(--motion-fast) var(--ease-out);
  }
  .q:hover { border-color: var(--accent); color: var(--accent-text); }
  .q:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .eli5-detail {
    margin: calc(-1 * var(--space-2)) 0 var(--space-3);
    color: var(--text-muted); font-size: var(--text-xs); line-height: 1.5; opacity: .85;
  }
</style>
