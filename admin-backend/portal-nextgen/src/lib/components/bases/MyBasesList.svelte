<script>
  // The caller's own bases, one section per linked character (V1 all-linked
  // parity). Each section lists that character's blueprint rows; an offline hint
  // shows when the character is online (rename is offline-gated). A section that
  // could not be read from the relay degrades to an honest note.
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import BlueprintRow from './BlueprintRow.svelte';

  let { sections = [], nameMax = 40 } = $props();
</script>

<div class="mybases">
  <p class="panel-kicker mono">My bases</p>
  {#if sections.length === 0}
    <SealedPanel
      status="empty" action="none" art="no-bases"
      emptyText="No linked characters with bases yet."
    />
  {:else}
    {#each sections as sec (sec.account_id)}
      <section class="section">
        <div class="shead">
          <h3 class="cname">{sec.character_name}</h3>
          {#if sec.online}
            <span class="gate mono" title="Rename is only available while you are logged out">online · rename offline</span>
          {/if}
        </div>
        {#if !sec.available}
          <SealedPanel
            status="error"
            errorText="This character's bases could not be read right now."
          />
        {:else if (sec.blueprints || []).length === 0}
          <SealedPanel
            status="empty" action="none" art="no-bases"
            emptyText="No saved bases on this character."
          />
        {:else}
          <ul class="rows" role="list">
            {#each sec.blueprints as bp (bp.bp_id)}
              <BlueprintRow {bp} accountId={sec.account_id} {nameMax} />
            {/each}
          </ul>
        {/if}
      </section>
    {/each}
  {/if}
</div>

<style>
  .panel-kicker { margin: 0 0 var(--space-3); color: var(--accent); text-transform: uppercase; letter-spacing: .18em; font-size: var(--text-xs); }
  .section { margin-bottom: var(--space-4); }
  .section:last-child { margin-bottom: 0; }
  .shead { display: flex; align-items: baseline; gap: var(--space-3); margin-bottom: var(--space-2); }
  .cname { margin: 0; font-family: var(--font-display); font-size: var(--text-lg); text-transform: uppercase; letter-spacing: .04em; color: var(--text); }
  .gate { font-size: var(--text-xs); color: var(--accent-text); }
  .rows { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-1); }
</style>
