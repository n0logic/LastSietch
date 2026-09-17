<script>
  // Placeable-dyepack swatch chips. The backend attaches swatch:{chips:[hex...],
  // exact:false} to any reward whose template is a placeable Swatch, computed
  // from the house's armor dyepack palette (a PROXY, not the true placeable
  // colors). We render the hex squares and, when exact is false, an honest
  // "house palette" badge so the color is never misrepresented as the real one.
  // The accurate placeable-dyepack RE extraction is a filed follow-up task.
  let { swatch = null } = $props();

  let chips = $derived(Array.isArray(swatch?.chips) ? swatch.chips.filter(Boolean) : []);
  let exact = $derived(swatch?.exact === true);
</script>

{#if chips.length}
  <span class="swatch">
    <span class="chips" aria-label="Dye palette">
      {#each chips as hex, i (i)}
        <span class="chip" style="background:{hex}" title={hex}></span>
      {/each}
    </span>
    {#if !exact}<span class="badge mono" title="Proxy from the house armor dyepack, not the exact placeable dye">house palette</span>{/if}
  </span>
{/if}

<style>
  .swatch { display: inline-flex; align-items: center; gap: var(--space-2); }
  .chips { display: inline-flex; gap: 3px; }
  .chip {
    width: 14px; height: 14px; border-radius: 3px;
    border: 1px solid rgba(0, 0, 0, .45);
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, .15);
  }
  .badge {
    font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: .08em;
    border: 1px solid var(--edge); border-radius: var(--radius-sm); padding: 1px var(--space-1);
  }
</style>
