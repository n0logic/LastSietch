<script>
  // Monthly track: a single premium node in spice-violet (--ls-melange), the
  // module's third tier alongside Daily/Weekly. Despite the name, it does NOT
  // run on the calendar month (owner ruling: a calendar month makes February
  // silently harder to hit than a 31-day month). It unlocks once the player has
  // logged in on `requirement` distinct days within a fixed 28-day period (four
  // ISO weeks, lined up with the weekly weapon rotation) -- a day-COUNT gate,
  // not a consecutive streak, and it does NOT reset on a missed day the way the
  // daily streak does. The claim itself lives on the module's single Claim button
  // (ClaimAction -> claimAll), which collects daily + weekly + monthly in one press;
  // this component shows the item and the gate, and never claims on its own.
  import HudGauge from '$lib/components/HudGauge.svelte';
  import RewardCard from './RewardCard.svelte';

  let { monthly = null } = $props();

  // The backend flattens the reward fields directly onto `monthly` (same shape
  // as `weekly`); there is no nested `.reward`.
  let reward = $derived(monthly?.template_id ? {
    template_id: monthly.template_id,
    name: monthly.name,
    icon: monthly.icon,
    grade: monthly.quality_level,
    rarity: monthly.rarity,
    type: monthly.type || 'weapon',
  } : null);

  let claimed = $derived(monthly?.claimed === true);
  let unlocked = $derived(monthly?.unlocked === true);
  let state = $derived(claimed ? 'claimed' : unlocked ? 'available' : 'locked');

  const STATE = {
    locked:    { glyph: '?',      label: 'Locked' },
    available: { glyph: '!',      label: 'Available' },
    claimed:   { glyph: '✓', label: 'Claimed' },
  };
  let chip = $derived(STATE[state]);

  // The N-logins-per-28-day-period requirement. Threshold, period bounds, and
  // the player's own day count are landing on the backend alongside this
  // change; fall back to the known current rule and hide anything we can't
  // resolve to a real number/date rather than render "undefined".
  let requirement = $derived.by(() => {
    const n = Number(monthly?.requirement);
    return Number.isFinite(n) && n > 0 ? n : 15;
  });
  let progressDays = $derived.by(() => {
    // `progress` is the real key the overview ships (verified against the handler).
    // The older aliases are kept only as a safety net for a partial deploy where
    // the backend is briefly older than this bundle; they have never existed in
    // any shipped payload, so if `progress` is ever renamed, fix it here rather
    // than adding a fourth guess.
    const n = Number(monthly?.progress ?? monthly?.login_days_count ?? monthly?.days_logged_in);
    return Number.isFinite(n) ? Math.max(0, Math.min(n, requirement)) : null;
  });
  // This period's last day, formatted short ("Aug 9"). `period_end` is
  // confirmed live ("YYYY-MM-DD", the exact last day of the current 28-day
  // period). Omitted entirely if absent rather than guessed from another
  // field -- `resets_label` below is a DIFFERENT date (the next period's
  // start) and would be wrong, not just imprecise, if reused here.
  let periodEndLabel = $derived.by(() => {
    const raw = monthly?.period_end;
    const d = raw ? new Date(raw) : null;
    return (d && !isNaN(d.getTime()))
      ? d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' })
      : null;
  });
  // The NEXT period's start (a distinct date from periodEndLabel above,
  // period_end + 1 day). `resets_label` already carries this directly.
  let nextPeriodLabel = $derived(monthly?.resets_label || null);

  // The augment list the backend ships alongside the item (name/grade/label). Roll
  // VALUES are randomised by the writer at grant time and are deliberately absent
  // here, so this previews WHICH augments ride the weapon, never their numbers.
  let augments = $derived(Array.isArray(monthly?.augments) ? monthly.augments : []);
  // "Plasma Cannon" reads better than "a grade 5 weapon"; fall back to the generic
  // line only when the payload predates the item fields.
  let itemName = $derived(monthly?.name || null);
</script>

<div class="mt">
  <p class="panel-kicker mono">Monthly reward</p>

  <div class="chest {state}">
    {#if reward}
      <RewardCard {reward} />
    {:else}
      <div class="glyph" aria-hidden="true">{chip.glyph}</div>
    {/if}
    <div class="body">
      <span class="state-lbl mono">{chip.label}</span>
      <p class="desc">
        {#if itemName}
          <strong>{itemName}</strong>, grade {monthly?.quality_level ?? 5}, minted straight to your CHOAM bank.
        {:else}
          A grade 5 weapon with randomised augment rolls, minted straight to your CHOAM bank.
        {/if}
      </p>
      {#if augments.length}
        <ul class="augs">
          {#each augments as a (a.name)}
            <li><span class="aug-g mono">G{a.grade}</span> {a.label || a.name}</li>
          {/each}
        </ul>
        <p class="note">Roll values are rolled fresh when you claim, so no two are alike.</p>
        <p class="note">
          All {augments.length} are already on the weapon. How many you can use at once depends on your
          character's weapon augment limit, so any beyond it stay locked on the item and unlock as that
          limit rises.
        </p>
      {/if}
      {#if state === 'locked'}
        <p class="rule">
          Unlocks at {requirement} login days in this 28-day period{periodEndLabel ? `, through ${periodEndLabel}` : ''}.
        </p>
        <p class="note">Unlike the streak, a missed day does not reset this count.</p>
      {:else if state === 'claimed'}
        <p class="rule">Next 28-day period begins{nextPeriodLabel ? ` ${nextPeriodLabel}` : ' soon'}.</p>
      {:else}
        <p class="rule">Unlocked for this period. Use Claim at the top of the page.</p>
      {/if}
      {#if nextPeriodLabel && state !== 'claimed'}
        <p class="note">A different weapon rotates in on {nextPeriodLabel}.</p>
      {/if}
    </div>
  </div>

  {#if progressDays != null}
    <div class="progress">
      <!-- Only the number sits inside the ring; "15 of 15" plus a two-word label
           overflowed an 88 px circle at the full count (live QA 2026-09-03). -->
      <HudGauge
        value={progressDays} max={requirement}
        display={String(progressDays)}
        label={`of ${requirement} days`} size={104}
      />
    </div>
  {/if}

</div>

<style>
  .mt { display: flex; flex-direction: column; gap: var(--space-3); }
  .panel-kicker { margin: 0; color: var(--accent); text-transform: uppercase; letter-spacing: .18em; font-size: var(--text-xs); }

  .chest {
    display: flex; align-items: center; gap: var(--space-4); flex-wrap: wrap;
    padding: var(--space-4); border-radius: var(--radius-md);
    border: 1px solid color-mix(in srgb, var(--ls-melange) 45%, var(--edge));
    background: color-mix(in srgb, var(--ls-melange) 9%, var(--metal-0));
    box-shadow: inset 0 1px 0 var(--metal-hi), 0 0 18px -6px var(--ls-melange-glow);
  }
  .glyph {
    flex: 0 0 auto; width: 3.4rem; height: 3.4rem; display: grid; place-items: center;
    font-family: var(--font-display); font-size: var(--text-2xl); font-weight: 700;
    color: var(--ls-melange-hi);
    border: 1px solid color-mix(in srgb, var(--ls-melange) 55%, var(--edge));
    border-radius: var(--radius-sm);
    background: color-mix(in srgb, var(--ls-melange) 14%, transparent);
    box-shadow: 0 0 14px -2px var(--ls-melange-glow);
  }
  .body { display: flex; flex-direction: column; gap: var(--space-1); min-width: 0; }
  .state-lbl {
    font-size: var(--text-xs); letter-spacing: .16em; text-transform: uppercase;
    color: var(--ls-melange-hi);
  }
  /* Claimed reuses the app-wide green-checkmark convention (DailyCalendar,
     Landsraad's collected-swatch tag); locked/available stay in the melange
     family, the monthly node's signature color. */
  .chest.claimed .state-lbl { color: var(--ls-green); }
  .chest.available .glyph { box-shadow: 0 0 20px -1px var(--ls-melange-glow); }
  .desc { margin: 0; font-size: var(--text-sm); color: var(--text-muted); }
  .rule { margin: 0; font-size: var(--text-sm); color: var(--text); }
  .note { margin: 0; font-size: var(--text-xs); color: var(--text-muted); }

  /* Augment list: one line per augment, grade badge in the melange family so it
     reads as part of the monthly node rather than the amber reward-card system. */
  .augs { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 2px; }
  .augs li { font-size: var(--text-sm); color: var(--text); display: flex; align-items: center; gap: var(--space-2); }
  .aug-g {
    flex: 0 0 auto; font-size: var(--text-xs); line-height: 1;
    padding: 2px 5px; border-radius: var(--radius-sm);
    color: var(--ls-melange-hi);
    border: 1px solid color-mix(in srgb, var(--ls-melange) 55%, var(--edge));
    background: color-mix(in srgb, var(--ls-melange) 14%, transparent);
  }

  .progress { display: flex; }
</style>
