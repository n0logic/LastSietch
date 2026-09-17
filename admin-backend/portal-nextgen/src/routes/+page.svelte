<script>
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import AnnouncementBanner from '$lib/components/AnnouncementBanner.svelte';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import HomeInstruments from '$lib/components/home/HomeInstruments.svelte';
  import ServerSchedule from '$lib/components/ServerSchedule.svelte';
  import VerdictCard from '$lib/components/home/VerdictCard.svelte';
  import ArrivalHero from '$lib/components/home/ArrivalHero.svelte';
  import LiveCountdown from '$lib/components/LiveCountdown.svelte';
  import { useAuthGate } from '$lib/authGate.svelte.js';
  import { home, subscribe, setDim, loadHomeOverview } from '$lib/home.svelte.js';
  import { homeAppearance } from '$lib/home/appearance.js';
  import { api } from '$lib/api.js';
  import { EVENT_BANNERS } from '$lib/events.svelte.js';

  const gate = useAuthGate();
  onMount(subscribe);
  let nextEvent = $state(null);
  let eventsRead = $state(false);
  onMount(() => {
    let mounted = true;
    api.events.list('upcoming').then((response) => {
      if (!mounted) return;
      nextEvent = response?.events?.[0] || null;
      eventsRead = true;
    }).catch(() => {});
    return () => { mounted = false; };
  });
  $effect(() => { if (gate.authed) loadHomeOverview(); });

  let s = $derived(home.signals);
  let appearance = $derived(homeAppearance(s, home.raw.overview?.clock));
  let instances = $derived(home.raw.data?.instances ?? [{ key: 'pve', label: 'PvE', dim: 0 }, { key: 'pvp', label: 'PvP', dim: 1 }]);
  let freshSec = $derived(home.lastReadAt ? Math.max(0, Math.round((s.now - home.lastReadAt) / 1000)) : null);
  let fresh = $derived(home.raw.live != null && home.raw.live.available !== false && freshSec != null && freshSec <= 30);
  let banner = $derived(EVENT_BANNERS.includes(nextEvent?.banner) ? nextEvent.banner : null);
</script>

<svelte:head><title>Home | Last Sietch</title></svelte:head>

<div class="page" data-stand={appearance.map}>
  <AnnouncementBanner announcement={home.raw.announcement} />
  <p class="start-link">New to Last Sietch? <a href={`${base}/start`}>Start with the first-session guide <span aria-hidden="true">↗</span></a></p>
  <div class="arrival-grid">
    <ArrivalHero {appearance} signals={s} {fresh} />
    <section class="reading" aria-label="Your next move">
      <p class="reading-label">Right now</p>
      <VerdictCard verdict={home.verdict} subLines={home.subLines} compact />
      <div class="ready-actions">
        {#if gate.authed && s.rewardsEnabled !== false && (s.claimableTotal > 0 || s.weeklyClaimable || s.monthlyClaimable)}
          <a href={`${base}/rewards`} class="ready">Rewards ready <span aria-hidden="true">↗</span></a>
        {/if}
        {#if gate.authed && s.deliveriesPending > 0}
          <a href={`${base}/mailbox#deliveries`} class="ready">{s.deliveriesPending} {s.deliveriesPending === 1 ? 'package' : 'packages'} still landing <span aria-hidden="true">↗</span></a>
        {/if}
        {#if gate.authed}<a href={`${base}/activity`}>Since your last visit <span aria-hidden="true">↗</span></a>{/if}
      </div>
      <p class="fresh" class:live={fresh}>
        {#if freshSec === null}Connecting to the desert{:else if fresh}Updated {freshSec}s ago{:else}Last update {freshSec}s ago. Reconnecting.{/if}
      </p>
    </section>
  </div>

  <ServerSchedule />

  <div class="board-context">
    <span>{appearance.label} · {appearance.mode}{#if appearance.untracked} · positions untracked{/if}</span>
    <div class="instance-toggle" role="tablist" aria-label="Instance">
      {#each instances as inst}
        <button type="button" role="tab" aria-selected={inst.dim === s.dim} class:on={inst.dim === s.dim} onclick={() => setDim(inst.dim)}>{inst.label}</button>
      {/each}
    </div>
  </div>

  <HomeInstruments signals={s} linked={s.linked} loading={gate.loading} homeStatus={home.homeStatus} />

  <div class="discover">
    <section class="desert" aria-label="Explore the desert">
      <div class="section-head"><h2>Your desert</h2><a href={`${base}/desert`}>World pulse <span aria-hidden="true">↗</span></a></div>
      <div class="map-links">
        {#each [{ name: 'Habbanya', slug: 'habbanya', mode: 'PvE' }, { name: 'Kulon', slug: 'kulon', mode: 'PvP' }, { name: 'Amtal', slug: 'amtal', mode: 'Full PvP · untracked' }] as map}
          <a class="map-link" href={`${base}/maps/hagga?inst=${map.slug}`}>
            <img src={`/img/v3/plates/${map.slug}-dusk-clear-small.jpg`} alt="" loading="lazy" width="640" height="360" />
            <span><b>{map.name}</b><small>{map.mode}</small></span>
          </a>
        {/each}
      </div>
      <div class="more-places"><a href={`${base}/maps`}>All maps</a><a href={`${base}/bases`}>Base blueprints</a><a href={`${base}/storage`}>Your storage</a></div>
    </section>
    <CarvedSlab>
      <p class="reading-label">Next event</p>
      {#if nextEvent}
        {#if banner}<img class="event-art" src={`/img/v3/banners/${banner}.jpg`} alt="" loading="lazy" />{/if}
        <h2>{nextEvent.title}</h2>
        {#if nextEvent.starts_utc}<p class="event-time"><LiveCountdown target={nextEvent.starts_utc} prefix="Starts in" /></p>{/if}
        <a class="event-link" href={`${base}/events/${nextEvent.id}`}>View event and reminder <span aria-hidden="true">↗</span></a>
      {:else}
        <h2>{eventsRead ? 'The calendar is clear' : 'Check the calendar'}</h2>
        <p class="event-time">{eventsRead ? 'The next gathering will appear here when it is announced.' : 'The next event could not be read yet.'}</p>
        <a class="event-link" href={`${base}/events`}>Open events <span aria-hidden="true">↗</span></a>
      {/if}
    </CarvedSlab>
  </div>
  <p class="footnote">Ibad blue marks live readings. <a href={`${base}/maps`}>All maps</a> · <a href={`${base}/rules`}>Server rules</a> · <a href={`${base}/help`}>Help</a></p>
</div>

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: 24px 24px 48px; }
  .start-link { display: flex; flex-wrap: wrap; gap: 6px 12px; margin: 0 0 16px; color: var(--text-muted); font-size: 13px; }
  .arrival-grid { display: grid; grid-template-columns: minmax(0, 1.7fr) minmax(300px, 1fr); gap: 20px; align-items: stretch; }
  .reading { padding: 24px; background: var(--panel); border: 1px solid var(--edge); border-top: 2px solid var(--accent-soft); border-radius: 6px; display: flex; flex-direction: column; }
  .reading-label { margin: 0 0 12px; font-size: 12px; letter-spacing: .06em; color: var(--text-muted); }
  .ready-actions { display: flex; flex-direction: column; margin-top: 12px; gap: 6px; }
  .ready-actions a { display: flex; align-items: center; justify-content: space-between; gap: 8px; min-height: 40px; padding: 8px 0; text-decoration: none; border-bottom: 1px solid var(--edge); font-size: 14px; }
  .ready-actions a.ready { color: var(--accent-text); }
  .fresh { margin: auto 0 0; padding-top: 16px; color: var(--text-muted); font-size: 12px; }
  .fresh.live { color: var(--ls-ibad); }
  .board-context { margin: 24px 0 12px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; font-size: 13px; color: var(--text-muted); }
  .instance-toggle { display: flex; gap: 4px; }
  .instance-toggle button { min-height: 40px; padding: 8px 14px; font: inherit; color: var(--text-muted); background: none; border: 1px solid var(--edge); border-radius: 4px; cursor: pointer; }
  .instance-toggle button.on { color: var(--accent-text); border-color: var(--accent-soft); background: var(--bg-elevated); }
  .discover { display: grid; grid-template-columns: minmax(0, 1.7fr) minmax(300px, 1fr); gap: 24px; margin-top: 28px; }
  .desert { min-width: 0; }
  .section-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 16px; gap: 12px; }
  .section-head h2 { font-size: 24px; }
  .section-head a, .more-places a, .event-link { font-size: 13px; text-decoration: none; }
  .map-links { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
  .map-link { position: relative; aspect-ratio: 1.2; border-radius: 6px; overflow: hidden; color: #eee5d5; background: #15110d; text-decoration: none; }
  .map-link img { width: 100%; height: 100%; object-fit: cover; transition: transform .25s; }
  .map-link:hover img { transform: scale(1.03); }
  .map-link > span { position: absolute; inset: auto 0 0; padding: 32px 14px 14px; background: linear-gradient(transparent, rgba(12,10,8,.95)); }
  .map-link b { display: block; font: 700 23px var(--font-display); }
  .map-link small { display: block; font-size: 12px; color: #d4c4a9; }
  .more-places { display: flex; flex-wrap: wrap; gap: 20px; padding-top: 16px; }
  .event-art { width: 100%; aspect-ratio: 3; object-fit: cover; border-radius: 4px; margin-bottom: 12px; }
  .event-time { font-size: 14px; color: var(--text-muted); margin: 12px 0; }
  .footnote { font-size: 12px; color: var(--text-muted); margin: 32px 0 0; }
  [data-stand='amtal'] .reading { border-top-color: #ad7056; }
  :global([data-theme='night']) [data-stand='amtal'] .reading { background: linear-gradient(rgba(38,23,18,.96), rgba(25,17,13,.97)), url('/img/v3/textures/red-rock.jpg'); }
  @media (max-width: 1100px) { .arrival-grid, .discover { grid-template-columns: minmax(0, 1.3fr) minmax(280px, 1fr); } .map-links { grid-template-columns: 1fr; } .map-link { aspect-ratio: 3; } }
  @media (max-width: 759px) { .page { padding: 12px 12px 32px; } .arrival-grid, .discover { grid-template-columns: 1fr; gap: 12px; } .reading { padding: 16px; } .board-context { margin-top: 18px; } .map-links { grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; } .map-link { aspect-ratio: .8; } .map-link > span { padding: 24px 8px 10px; } .map-link b { font-size: 20px; } .map-link small { font-size: 11px; } }
</style>
