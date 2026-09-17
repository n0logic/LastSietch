<script>
  // Weekly track: a SINGLE card (the backend returns one weekly object, not an
  // array). It carries the rotating tier-six weapon reward (RewardCard: icon +
  // G{n} grade badge + rarity ring + redundant text label), a DurabilityMeter bar
  // showing streak progress toward the 7-day requirement, a redundant claim-state
  // chip, and its own Claim button when unlocked+claimable. Unlocks at a 7-day
  // streak; the online-safe G29 bank mint means no offline gate.
  import RewardCard from './RewardCard.svelte';
  import DurabilityMeter from '$lib/components/storage/DurabilityMeter.svelte';

  let { weekly = null, streakCurrent = 0, claiming = false, onClaim = () => {} } = $props();

  const STATE = {
    locked:    { glyph: '\u{1F512}', label: 'Locked' },
    claimable: { glyph: '!',         label: 'Claim' },
    claimed:   { glyph: '✓',    label: 'Claimed' },
  };

  let req = $derived(Number(weekly?.requirement) || 7);
  let reward = $derived(weekly ? {
    template_id: weekly.template_id,
    name: weekly.name,
    icon: weekly.icon,
    grade: weekly.quality_level,
    rarity: weekly.rarity,
    type: 'weapon',
  } : null);
  let progress = $derived({ current: Math.max(0, Math.min(streakCurrent, req)), max: req });
  let state = $derived(
    !weekly ? 'locked' : weekly.claimed ? 'claimed' : weekly.claimable ? 'claimable' : 'locked'
  );
  let chip = $derived(STATE[state]);
  // Rotation day (server-authoritative UTC label); the weapon swaps at the ISO-week
  // boundary. Falls back to "Monday" if the backend predates the rotates_* fields.
  let rotatesDay = $derived(weekly?.rotates_day || 'Monday');
  let rotatesLabel = $derived(weekly?.rotates_label || '');
</script>

<div class="wk">
  <p class="panel-kicker mono">Weekly reward</p>

  {#if !weekly}
    <p class="hollow">No weekly reward to show yet.</p>
  {:else}
    <div class="node {state}">
      <RewardCard {reward} />
      <div class="meta">
        <span class="tag">
          <span class="g" aria-hidden="true">{chip.glyph}</span>
          <span class="t">{chip.label}</span>
        </span>
        <div class="bar">
          <span class="bar-cap mono">Streak {progress.current}/{req}</span>
          <DurabilityMeter durability={progress} size="bar" label="Streak progress" />
        </div>
        {#if state === 'locked'}
          <p class="need">Reach a {req}-day streak to unlock.</p>
        {:else if state === 'claimable'}
          <button class="wk-claim" type="button" onclick={onClaim} disabled={claiming}>
            {claiming ? 'Claiming…' : 'Claim weapon'}
          </button>
        {/if}
        {#if state !== 'claimed'}
          <p class="deposit-note">Delivered straight to your CHOAM bank. Usually no logout needed.</p>
        {/if}
        <p class="rotate-note">
          <span class="r-glyph" aria-hidden="true">↻</span>
          A new weapon rotates in every {rotatesDay}{rotatesLabel ? ` (next ${rotatesLabel} UTC)` : ''}.
        </p>
      </div>
    </div>
  {/if}
</div>

<style>
  .wk { display: flex; flex-direction: column; gap: var(--space-3); }
  .panel-kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .18em; font-size: var(--text-xs); }
  .hollow { color: var(--text-muted); font-size: var(--text-sm); margin: 0; }
  .deposit-note { color: var(--text-muted); font-size: var(--text-xs); margin: 0; }
  .rotate-note {
    display: flex; align-items: center; gap: 5px;
    color: var(--text-muted); font-size: var(--text-xs); margin: 0;
    letter-spacing: .02em;
  }
  .rotate-note .r-glyph { color: var(--accent); font-size: var(--text-sm); line-height: 1; }

  .node { display: flex; gap: var(--space-4); align-items: flex-start; }
  .node :global(.rcard) { flex: 0 0 8rem; }
  .node.locked :global(.rcard) { opacity: .7; }
  .meta { display: flex; flex-direction: column; gap: var(--space-2); min-width: 0; flex: 1 1 auto; }

  .tag { display: inline-flex; align-items: center; gap: 5px; font-size: var(--text-xs); letter-spacing: .08em; text-transform: uppercase; }
  .tag .t { color: var(--text-muted); }
  .node.claimed .g, .node.claimed .t { color: var(--ls-green); }
  .node.claimable .g, .node.claimable .t { color: var(--accent-bright); font-weight: 700; }
  .node.locked .g, .node.locked .t { color: var(--text-muted); }

  .bar { display: flex; flex-direction: column; gap: var(--space-1); }
  .bar-cap { font-size: var(--text-xs); color: var(--text-muted); letter-spacing: .04em; }
  .need { margin: 0; font-size: var(--text-sm); color: var(--text-muted); }

  .wk-claim {
    align-self: flex-start;
    font-family: var(--font-display); letter-spacing: .06em; font-size: var(--text-sm);
    color: var(--bg-deep); background: var(--accent); border: 1px solid var(--accent);
    border-radius: var(--radius-sm); padding: var(--space-2) var(--space-4);
    box-shadow: 0 0 12px var(--accent-glow); cursor: pointer;
    transition: filter var(--motion-fast) var(--ease-out);
  }
  .wk-claim:hover:not(:disabled) { filter: brightness(1.12); }
  .wk-claim:disabled { opacity: .55; cursor: progress; }
</style>
