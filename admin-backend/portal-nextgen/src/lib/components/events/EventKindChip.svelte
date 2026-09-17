<script>
  // The event's category, as amber chrome. A kind is an editorial label, not a
  // live reading off the game, so it never wears Ibad blue: the only Ibad on this
  // surface is the "starting now" state, which IS a fact about the clock.
  //
  // An unrecognised or missing kind renders NOTHING. A chip reading "Other" for a
  // value we do not know is a label invented on the player's behalf, and it would
  // hide the one case worth seeing (a kind the backend grew and the UI has not).
  let { kind = null } = $props();

  const LABELS = {
    community: 'Community',
    faction_war: 'Faction war',
    maintenance: 'Maintenance',
    other: 'Other',
  };

  let label = $derived(LABELS[String(kind ?? '')] || '');
</script>

{#if label}
  <span class="kind mono" data-kind={kind}>{label}</span>
{/if}

<style>
  .kind {
    display: inline-flex; align-items: center; white-space: nowrap;
    font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase;
    color: var(--accent-text);
    padding: var(--space-1) var(--space-3);
    background: color-mix(in srgb, var(--accent) 12%, transparent);
    border: 1px solid color-mix(in srgb, var(--accent) 45%, var(--edge));
    border-radius: var(--radius-sm);
  }
</style>
