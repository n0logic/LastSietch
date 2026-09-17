<script>
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import { serverTime, subscribeServerTime } from '$lib/server-time.svelte.js';
  import { serverClockText, resetDateText, resetCountdown } from '$lib/server-time.js';
  let { compact = false } = $props();
  let localZone = $state(null);
  onMount(() => {
    localZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    return subscribeServerTime();
  });
  let clock = $derived(serverClockText(serverTime.nowMs, serverTime.timeZone));
  let reset = $derived(serverTime.status === 'loading' ? 'Checking schedule' : resetDateText(serverTime.nextResetMs, serverTime.timeZone, compact));
  let countdown = $derived(resetCountdown(serverTime.nextResetMs, serverTime.nowMs));
  let unavailable = $derived(serverTime.status === 'unavailable');
</script>

{#if compact}
  <div class="time-strip" aria-label="Server clock and scheduled reset" data-server-schedule="compact">
    <p><span>Server time</span> <time class="mono" datetime={serverTime.nowMs === null ? undefined : new Date(serverTime.nowMs).toISOString()}>{unavailable ? 'Unavailable' : clock}</time></p>
    <a href={`${base}/desert#server-time`} title="Scheduled Coriolis reset, Eastern time"><span>Coriolis</span> <b>{reset}</b></a>
    {#if serverTime.status === 'stale'}<span class="sync-note">Time sync interrupted</span>{/if}
  </div>
{:else}
  <section id="server-time" class="schedule" aria-label="Last Sietch time and Coriolis reset" data-server-schedule="full">
    <div class="clock-reading">
      <p class="label">Last Sietch time</p>
      <time class="clock mono" datetime={serverTime.nowMs === null ? undefined : new Date(serverTime.nowMs).toISOString()}>{unavailable ? 'Time unavailable' : clock}</time>
      <p class="detail">Eastern time, with daylight saving applied.</p>
      {#if serverTime.status === 'stale'}<p class="detail caution">Time sync interrupted. Showing the last synchronized clock.</p>{/if}
    </div>
    <div class="reset-reading">
      <p class="label">Next scheduled Coriolis reset</p>
      <time class="reset" datetime={serverTime.nextResetMs === null ? undefined : new Date(serverTime.nextResetMs).toISOString()}>{reset}</time>
      {#if countdown}<p class="countdown mono">{countdown}</p>{/if}
      {#if localZone && localZone !== serverTime.timeZone && serverTime.nextResetMs !== null}
        <p class="detail">Your time: {resetDateText(serverTime.nextResetMs, localZone)}</p>
      {/if}
      <p class="detail">Deep Desert cycle reset, not the next local sandstorm. <a href={`${base}/help#howto-server-time`}>About this schedule</a></p>
    </div>
  </section>
{/if}

<style>
  .time-strip { display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 6px 20px; color: var(--text-muted); font-size: 12px; }
  .time-strip p { margin: 0; display: flex; flex-wrap: wrap; gap: 6px; }
  .time-strip time { color: var(--text); font-variant-numeric: tabular-nums; }
  .time-strip a { display: flex; flex-wrap: wrap; gap: 6px; color: var(--text-muted); text-decoration: none; }
  .time-strip a:hover { color: var(--accent-text); }
  .time-strip b { color: var(--text); font-weight: 500; }
  .schedule { display: grid; grid-template-columns: minmax(0, .85fr) minmax(0, 1.5fr); gap: 20px 32px; padding: 20px 0; margin: 20px 0; border-top: 1px solid var(--edge); border-bottom: 1px solid var(--edge); scroll-margin-top: 150px; }
  .label { color: var(--text-muted); font-size: 12px; margin: 0 0 8px; letter-spacing: .06em; }
  .clock { display: block; font-size: clamp(18px, 2vw, 24px); font-variant-numeric: tabular-nums; }
  .reset { display: block; font-size: clamp(17px, 1.6vw, 22px); color: var(--text); overflow-wrap: anywhere; }
  .countdown { color: var(--accent-text); font-size: 14px; font-variant-numeric: tabular-nums; margin: 8px 0; }
  .detail { color: var(--text-muted); font-size: 12px; line-height: 1.6; margin: 8px 0 0; }
  .detail a { color: inherit; text-underline-offset: 3px; }
  .caution, .sync-note { color: var(--accent-text); }
  @media (max-width: 650px) { .schedule { grid-template-columns: minmax(0, 1fr); gap: 16px; } }
  @media (max-width: 400px) { .time-strip { font-size: 11px; } }
</style>
