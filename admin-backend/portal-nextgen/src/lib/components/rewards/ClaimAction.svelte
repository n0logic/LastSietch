<script>
  // The single Claim button for the whole module (amber-fill, copies the .acct-login /
  // .btn.sweep treatment). One press collects everything outstanding: accumulated daily
  // Solari, the weekly weapon, and the monthly augmented weapon. The claim is optimistic
  // in the store: flip -> rewardClaim -> success keeps claimed + toast / throw reverts +
  // write-notice. Solari is online-safe, so there is NO offline gate on this path.
  import { rewards, claimAll } from '$lib/rewards.svelte.js';

  let { today = null } = $props();

  let busy = $state(false);
  let amount = $derived(Number(today?.amount) || 0);

  // Already-earned Solari counts even when today's own rung has not landed yet: the
  // backend sweeps the whole unclaimed pool and no longer demands a fresh login to
  // release days the player already earned. Without this the button hid while real
  // Solari sat in the pool with nothing to press.
  let dailyReady = $derived(today?.claimable === true
    || (Number(rewards.daily?.claimableTotal) || 0) > 0);
  let weeklyReady = $derived(rewards.weekly?.claimable === true);
  let monthlyReady = $derived(rewards.monthly?.claimable === true);
  let tiers = $derived([dailyReady, weeklyReady, monthlyReady].filter(Boolean).length);

  // Name what the press will actually collect. With one tier outstanding the button says
  // exactly what it is; with several it counts them, because listing three reward names
  // in a button is longer than the ribbon can hold.
  let label = $derived.by(() => {
    if (tiers > 1) return `Claim ${tiers} rewards`;
    if (weeklyReady) return rewards.weekly?.name ? `Claim ${rewards.weekly.name}` : 'Claim weekly reward';
    if (monthlyReady) return rewards.monthly?.name ? `Claim ${rewards.monthly.name}` : 'Claim monthly reward';
    if (amount > 0) return `Claim ${amount.toLocaleString()} Solari`;
    return 'Claim today’s reward';
  });

  async function onClaim() {
    if (busy) return;
    busy = true;
    await claimAll();
    busy = false;
  }
</script>

{#if tiers > 0}
  <button class="claim" type="button" onclick={onClaim} disabled={busy || rewards.claiming}>
    {#if busy || rewards.claiming}
      Claiming&hellip;
    {:else}
      {label}
    {/if}
  </button>
{/if}

<style>
  .claim {
    font-family: var(--font-display);
    letter-spacing: .06em; font-size: var(--text-base);
    color: var(--bg-deep); background: var(--accent); border: 1px solid var(--accent);
    border-radius: var(--radius-sm); padding: var(--space-2) var(--space-5);
    box-shadow: 0 0 12px var(--accent-glow); cursor: pointer;
    transition: filter var(--motion-fast) var(--ease-out);
  }
  .claim:hover:not(:disabled) { filter: brightness(1.12); }
  .claim:disabled { opacity: .55; cursor: progress; }
</style>
