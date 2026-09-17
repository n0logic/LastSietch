<script>
  // A reward tile (forked from storage/ItemCell). Shows the reward icon, a rarity
  // tier ring/border in the amber family, a G{n} grade badge, and a redundant text
  // rarity label. There is no existing rarity-color system, so this is the one new
  // visual system and it must NEVER be hue-only: the color is always paired with a
  // text label (and the grade with a numeric badge). The grade ring reuses
  // DurabilityMeter (size=ring) as a thin fill under the art.
  import DurabilityMeter from '$lib/components/storage/DurabilityMeter.svelte';
  import { iconUrl } from '$lib/icons.js';

  let { reward = null, size = 'md' } = $props();

  // Rarity -> {color token, label}. Amber-family ramp per the UI contract. Includes
  // the game's own rarity vocabulary (Unique = rare draw, Memento = jackpot) so the
  // backend's rarity string maps without translation; unknown falls back to Common.
  const RARITY = {
    common:    { color: 'var(--edge)',          label: 'Common' },
    uncommon:  { color: 'var(--ls-green)',      label: 'Uncommon' },
    rare:      { color: 'var(--accent)',        label: 'Rare' },
    unique:    { color: 'var(--accent)',        label: 'Unique' },
    epic:      { color: 'var(--accent-bright)', label: 'Epic' },
    legendary: { color: 'var(--ls-melange)',    label: 'Legendary' },
    memento:   { color: 'var(--ls-melange)',    label: 'Memento' },
  };

  let rarityKey = $derived(String(reward?.rarity || 'common').toLowerCase());
  let tier = $derived(RARITY[rarityKey] || RARITY.common);
  let name = $derived(reward?.name || reward?.template_id || 'Reward');
  let icon = $derived(iconUrl(reward?.icon));
  let grade = $derived(reward?.grade != null ? Number(reward.grade) : null);
</script>

<div class="rcard {size}" style="--ring:{tier.color}" title={name}>
  <div class="art">
    {#if icon}
      <img src={icon} alt={name} loading="lazy" />
    {:else}
      <span class="glyph mono" aria-hidden="true">{(name[0] || '?').toUpperCase()}</span>
    {/if}
    {#if grade != null}
      <span class="grade mono" title="Grade {grade} of 6">G{grade}</span>
    {/if}
  </div>

  {#if grade != null}
    <DurabilityMeter durability={{ current: grade, max: 6 }} size="ring" label="Grade" />
  {/if}

  <span class="name">{name}</span>
  <span class="rarity" style="color:{tier.color}">
    <span class="dot" aria-hidden="true"></span>{tier.label}
  </span>
</div>

<style>
  .rcard {
    position: relative; display: flex; flex-direction: column; gap: var(--space-1);
    padding: var(--space-2); border-radius: var(--radius-sm);
    background: var(--metal-0); box-shadow: inset 0 1px 0 var(--metal-hi);
    /* Rarity ring: the tier color as a full border + a soft glow. Never hue-only:
       the text label + grade badge repeat the signal. */
    border: 1px solid var(--ring);
    box-shadow: inset 0 1px 0 var(--metal-hi), 0 0 10px -2px color-mix(in srgb, var(--ring) 55%, transparent);
    min-width: 0;
  }
  .rcard.sm { padding: var(--space-1); }
  .art {
    position: relative; aspect-ratio: 1; display: grid; place-items: center;
    background: color-mix(in srgb, var(--ring) 12%, transparent);
    border-radius: var(--radius-sm); overflow: hidden;
  }
  .art img { width: 100%; height: 100%; object-fit: contain; }
  .glyph { font-size: var(--text-xl); color: var(--text-muted); }
  .grade {
    position: absolute; right: 3px; bottom: 3px;
    font-size: var(--text-xs); font-weight: 600; font-variant-numeric: tabular-nums;
    color: var(--bg-deep); background: var(--ring);
    padding: 0 5px; border-radius: 3px; line-height: 1.5;
  }
  .name {
    font-size: var(--text-xs); color: var(--text);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .rarity {
    display: inline-flex; align-items: center; gap: var(--space-1);
    font-family: var(--font-mono); font-size: var(--text-xs);
    letter-spacing: .08em; text-transform: uppercase;
  }
  .rarity .dot {
    width: 7px; height: 7px; border-radius: 999px; background: currentColor;
    box-shadow: 0 0 5px color-mix(in srgb, currentColor 60%, transparent);
  }
</style>
