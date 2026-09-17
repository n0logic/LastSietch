<script>
  // Rewards: the login-rewards / battlepass surface. A streak tracker + a daily
  // login calendar (manual Solari claim, online-safe), a weekly rotating-weapon
  // track, and a monthly reward (a pre-augmented grade-5 weapon) that unlocks at
  // a login-days requirement within a fixed 28-day period -- NOT the calendar
  // month, despite the name (owner ruling: a calendar month makes February
  // silently harder to hit). Every write is CSRF + uuid-idempotent and fails
  // closed; the module ships DARK behind
  // LASTSIETCH_REWARD_ENABLED, so a deferred claim reads honestly as "not yet enabled".
  // Reads are never gated (a signed-in player can always browse); signed-out seals
  // to an honest Connect-Discord panel.
  import { untrack } from 'svelte';
  import { useAuthGate } from '$lib/authGate.svelte.js';
  import { rewards, loadAll, claimDaily, claimWeekly, closeReveal } from '$lib/rewards.svelte.js';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import Notice from '$lib/components/Notice.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import LiveCountdown from '$lib/components/LiveCountdown.svelte';
  import StreakFlame from '$lib/components/rewards/StreakFlame.svelte';
  import DailyCalendar from '$lib/components/rewards/DailyCalendar.svelte';
  import ClaimAction from '$lib/components/rewards/ClaimAction.svelte';
  import WeeklyTrack from '$lib/components/rewards/WeeklyTrack.svelte';
  import MonthlyTrack from '$lib/components/rewards/MonthlyTrack.svelte';
  import MonthlyReveal from '$lib/components/rewards/MonthlyReveal.svelte';

  const gate = useAuthGate();

  let loaded = false;
  $effect(() => {
    const status = gate.status;
    if (status === 'authed' && !loaded) { loaded = true; untrack(loadAll); }
    else if (status === 'anon') { loaded = false; }
  });

  let today = $derived(rewards.daily.today);

  // Absolute claim-reset instant. Prefer the server value; fall back to the next
  // UTC midnight computed locally so the countdown always has a target.
  function nextUtcMidnight() {
    const d = new Date();
    d.setUTCHours(24, 0, 0, 0);
    return d.toISOString();
  }
  let resetTarget = $derived(rewards.nextClaimUtc || nextUtcMidnight());
</script>

<svelte:head>
  <title>Rewards | Last Sietch</title>
</svelte:head>

<div class="page">
  <PageHeader
    kicker="Last Sietch | Daily tribute"
    title="Rewards"
    sub="Log in each day to build a streak and claim your Solari. Reach the weekly milestone for a rotating tier-six weapon, and log enough days across the current 28-day period for the monthly reward: a pre-augmented grade-five weapon."
  />

  {#if gate.loading}
    <p class="skeleton">loading</p>
  {:else if gate.anon}
    <SealedPanel
      status="empty" action="login"
      emptyText="Sign in to see your streak, daily rewards, and claim your Solari."
    />
  {:else if rewards.status === 'error'}
    <SealedPanel
      status="error"
      errorText="Your rewards could not be read right now. Try again in a moment."
    />
  {:else}
    {#if rewards.status === 'ready' && !rewards.enabled}
      <Notice tone="info" text="Login rewards are not live yet. This is a preview of what is coming." />
    {/if}
    {#if rewards.notice}
      <Notice tone={rewards.noticeTone} text={rewards.notice} />
    {/if}

    <div class="stack">
      <!-- 1. Hero status ribbon: streak summary + countdown + claim. -->
      <CarvedSlab>
        <div class="ribbon">
          <div class="rib-streak">
            <span class="rib-num mono">{rewards.streak.current}</span>
            <span class="rib-lbl">day streak</span>
          </div>
          <div class="rib-reset">
            <span class="rib-cap mono">Next reward in</span>
            <LiveCountdown target={resetTarget} />
          </div>
          <div class="rib-claim">
            <ClaimAction {today} />
          </div>
        </div>
      </CarvedSlab>

      <!-- 2. Streak flame. -->
      <CarvedSlab>
        <StreakFlame
          current={rewards.streak.current}
          cycle={rewards.streak.cycle_day}
          best={rewards.streak.best}
          nextMilestone={rewards.streak.milestone_next}
        />
      </CarvedSlab>

      <!-- 3. Daily calendar strip. -->
      <CarvedSlab sharp={true}>
        <DailyCalendar
          cycle={rewards.daily.cycle}
          ramp={rewards.daily.ramp}
          weeks={rewards.daily.weeks}
          cycleLen={rewards.daily.cycleLen}
          milestones={rewards.daily.milestones}
          calendar={rewards.daily.calendar}
          claiming={rewards.claiming}
          onClaim={claimDaily}
        />
      </CarvedSlab>

      <!-- 5. Weekly + monthly tracks. -->
      <div class="tracks">
        <CarvedSlab>
          <WeeklyTrack
            weekly={rewards.weekly}
            streakCurrent={rewards.streak.current}
            claiming={rewards.claiming}
            onClaim={claimWeekly}
          />
        </CarvedSlab>
        <CarvedSlab>
          <MonthlyTrack monthly={rewards.monthly} />
        </CarvedSlab>
      </div>

      <!-- Opened by the store on a successful monthly claim, never by MonthlyTrack. -->
      <MonthlyReveal open={rewards.reveal.open} reward={rewards.reveal.reward} onclose={closeReveal} />

      <!-- Delivery note: redeemed item rewards land in the player's CHOAM Bank. -->
      <p class="delivery-note" role="note">
        <span class="dn-mark mono" aria-hidden="true">&#9670;</span>
        <span>Item rewards are delivered to your <strong>CHOAM Bank</strong> upon redemption of your
        accumulated rewards. Collect them in game from any bank terminal.</span>
      </p>
    </div>
  {/if}
</div>

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }
  .stack { margin-top: var(--space-4); display: flex; flex-direction: column; gap: var(--space-4); }

  .ribbon { display: flex; align-items: center; gap: var(--space-5); flex-wrap: wrap; }
  .rib-streak { display: flex; flex-direction: column; }
  .rib-num { font-size: var(--text-3xl); font-weight: 700; line-height: 1; color: var(--accent-text); }
  .rib-lbl { font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .16em; }
  .rib-reset { display: flex; flex-direction: column; gap: var(--space-1); }
  .rib-cap { font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: .12em; }
  .rib-claim { margin-left: auto; }

  .tracks { display: grid; gap: var(--space-4); grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr); align-items: start; }

  /* Stagger-rise the panels on enter (opacity/transform only). */
  .stack > :global(*) { opacity: 0; transform: translateY(14px); animation: rise .5s var(--ease-out) forwards; }
  .stack > :global(*:nth-child(1)) { animation-delay: .04s; }
  .stack > :global(*:nth-child(2)) { animation-delay: .10s; }
  .stack > :global(*:nth-child(3)) { animation-delay: .16s; }
  .stack > :global(*:nth-child(4)) { animation-delay: .22s; }
  .stack > :global(*:nth-child(5)) { animation-delay: .28s; }
  @keyframes rise { to { opacity: 1; transform: none; } }

  .delivery-note {
    display: flex; align-items: flex-start; gap: var(--space-2);
    margin: 0; padding: var(--space-3) var(--space-4);
    background: var(--panel); border: 1px solid var(--edge);
    border-radius: var(--radius-sm);
    color: var(--text-muted); font-size: var(--text-sm); line-height: 1.5;
  }
  .delivery-note strong { color: var(--accent-text); font-weight: 600; }
  .dn-mark { color: var(--accent); flex: none; line-height: 1.5; }

  @media (max-width: 780px) {
    .tracks { grid-template-columns: 1fr; }
    .rib-claim { margin-left: 0; width: 100%; }
  }
  @media (prefers-reduced-motion: reduce) {
    .stack > :global(*) { opacity: 1; transform: none; animation: none; }
  }
</style>
