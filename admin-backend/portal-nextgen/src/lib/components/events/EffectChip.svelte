<script>
  // What a weekend event does to the game, in plain words, and whether the
  // runner has it live. Props only; the caller resolves the effect with
  // $lib/weekend.js (effectOf for an event, describeSlot for a weekend).
  //
  // Ibad blue only on `running`: that is the one state the runner read back on
  // the servers. A missed or failed effect wears red and never reads as live. A
  // title-derived or planned effect has no state and shows none, because nothing
  // measured it.
  import { EFFECTS, scopeText, stateText } from '$lib/weekend.js';

  let { effect = null, state = null, scope = null, open = false, detail = false } = $props();

  let info = $derived(EFFECTS[effect] || null);
  let label = $derived(stateText(state, open));
  let where = $derived(scopeText(scope));
</script>

{#if info}
  <div class="effect">
    <span class="chip mono">{info.chip}</span>
    {#if label}
      <span class="state mono" data-state={state}>{label}</span>
    {/if}
    {#if info.restart}
      <span class="restart mono">Short restart at start and end</span>
    {/if}
    {#if detail}
      <p class="meaning">{info.meaning}{#if where}{' '}{where}{/if}</p>
    {/if}
  </div>
{/if}

<style>
  .effect { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); }
  .chip, .state, .restart {
    display: inline-flex; align-items: center; white-space: nowrap;
    font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase;
    padding: var(--space-1) var(--space-3); border-radius: var(--radius-sm);
  }
  .chip {
    color: var(--accent-text);
    background: color-mix(in srgb, var(--accent) 12%, transparent);
    border: 1px solid color-mix(in srgb, var(--accent) 45%, var(--edge));
  }
  .state { color: var(--text-muted); border: 1px solid var(--edge); }
  .state[data-state='running'] { color: var(--ls-ibad); border-color: var(--ls-ibad); }
  .state[data-state='failed'], .state[data-state='missed'] { color: var(--ls-red); border-color: var(--ls-red); }
  .restart { color: var(--text-muted); border: 1px dashed var(--edge); }
  .meaning { flex-basis: 100%; margin: 0; font-size: var(--text-sm); color: var(--text-muted); }
</style>
