<script>
  // The redesigned Home (P1, direction A "The Weekend Clock"), shown only behind
  // LASTSIETCH_PORTAL_SHELL_NEXT_ENABLED or the owner's ?shell=next override.
  //
  // Order is the owner ruling of 2026-10-29: the weekend first, the player's own
  // status second, then what needs them, the desert and the sietch. It reuses the
  // existing Home store (reads, polling, verdict) and the weekend module from the
  // events work, so a number here can never disagree with the shipped Home.
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import AnnouncementBanner from '$lib/components/AnnouncementBanner.svelte';
  import SietchHomeBanner from '$lib/components/home/SietchHomeBanner.svelte';
  import NeedsYou from './NeedsYou.svelte';
  import StatusBar from './StatusBar.svelte';
  import WeekendHero from './WeekendHero.svelte';
  import { LOGIN_URL } from '$lib/auth.svelte.js';
  import { useAuthGate } from '$lib/authGate.svelte.js';
  import { api } from '$lib/api.js';
  import { home, subscribe, loadHomeOverview } from '$lib/home.svelte.js';
  import { homeAppearance } from '$lib/home/appearance.js';
  import { desertLines, needsYou } from '$lib/shellHome.js';
  import { dayText, describeSlot, monthWeekends, weekendSlots } from '$lib/weekend.js';

  const gate = useAuthGate();
  onMount(subscribe);
  let events = $state([]);
  let now = $state(Date.now());
  onMount(() => {
    let mounted = true;
    Promise.all([api.events.list('live'), api.events.list('upcoming')]).then(([live, upcoming]) => {
      if (mounted) events = [...(live?.events || []), ...(upcoming?.events || [])];
    }).catch(() => {});
    const t = setInterval(() => { now = Date.now(); }, 60_000);
    return () => { mounted = false; clearInterval(t); };
  });
  $effect(() => { if (gate.authed) loadHomeOverview(); });

  let s = $derived(home.signals);
  let appearance = $derived(homeAppearance(s, home.raw.overview?.clock));
  let slots = $derived(weekendSlots(now).map((x) => describeSlot(x, events, now)));
  // The next four weekends from this one, from the month strip. A month's tail may
  // hold fewer than four; the Events page has the rest.
  let rotation = $derived(
    monthWeekends(slots[0].startMs)
      .map((x) => describeSlot(x, events, now))
      .filter((d) => d.endMs > now)
      .slice(0, 4)
      .map((d) => ({ ...d, dayText: dayText(d.startMs) }))
  );
  let needs = $derived(needsYou(s, base));
  // The desert's own reading, line by line. Null stays absent: "no storm" is only
  // said when the storm read answered.
  let deserts = $derived(desertLines(s));
  let freshSec = $derived(home.lastReadAt ? Math.max(0, Math.round((s.now - home.lastReadAt) / 1000)) : null);
  let fresh = $derived(home.raw.live != null && home.raw.live.available !== false && freshSec != null && freshSec <= 30);
</script>

<div class="page" data-stand={appearance.map}>
  <AnnouncementBanner announcement={home.raw.announcement} />
  <WeekendHero slot={slots[0]} {rotation} {appearance} {now} />
  <StatusBar signals={s} loginUrl={LOGIN_URL} />

  <div class="cols">
    <NeedsYou items={needs} signedIn={gate.authed && s.linked === true} />

    <section class="card" aria-labelledby="dn-h">
      <div class="head"><h2 id="dn-h">Desert now</h2><a href={`${base}/maps`}>Maps <span aria-hidden="true">↗</span></a></div>
      <ul class="reads">
        {#each deserts as line}<li class:alert={line.alert}>{line.text}</li>{/each}
      </ul>
      <p class="fresh" class:live={fresh}>
        {#if freshSec === null}Connecting to the desert{:else if fresh}Updated {freshSec}s ago{:else}Last update {freshSec}s ago. Reconnecting.{/if}
        {#if s.onlinePlayers != null} · <span class="live">{s.onlinePlayers}</span> online{/if}
      </p>
      <div class="maps">
        {#each [{ name: 'Habbanya', slug: 'habbanya', mode: 'PvE' }, { name: 'Amtal', slug: 'amtal', mode: 'Full PvP' }] as m}
          <a class="map" href={`${base}/maps/hagga?inst=${m.slug}`}>
            <img src={`/img/v3/plates/${m.slug}-dusk-clear-small.jpg`} alt="" loading="lazy" width="640" height="360" />
            <span><b>{m.name}</b><small>{m.mode}</small></span>
          </a>
        {/each}
      </div>
    </section>

    <section class="card" aria-labelledby="si-h">
      <div class="head"><h2 id="si-h">Sietch</h2><a href={`${base}/events`}>Events <span aria-hidden="true">↗</span></a></div>
      <SietchHomeBanner />
      {#if s.linked && s.guildRead && s.guildName}
        <a class="guild" href={`${base}/guilds`}><b>{s.guildName}</b>
          <span>{#if s.guildOnline != null}<span class="live">{s.guildOnline}</span>{' '}online{#if Array.isArray(s.guildMembers)}{' '}of {s.guildMembers.length}{/if}{:else}Open the roster{/if}</span></a>
      {/if}
      <p class="links"><a href={`${base}/guilds`}>Guilds</a><a href={`${base}/landsraad`}>Landsraad</a><a href={`${base}/chat`}>Chat</a></p>
    </section>
  </div>

  <p class="footnote">Blue figures update live. <a href={`${base}/rules`}>Server rules</a> · <a href={`${base}/help`}>Help</a> · <a href={`${base}/start`}>First session</a></p>
</div>

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: 24px 24px 48px; }
  .cols { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 20px; margin-top: 28px; }
  .card { padding: 20px; background: var(--panel); border: 1px solid var(--edge); border-radius: 6px; min-width: 0; }
  .head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 12px; gap: 12px; }
  h2 { margin: 0; font: 700 24px var(--font-display); letter-spacing: .06em; text-transform: uppercase; }
  .head a, .links a { font-size: var(--text-sm); text-decoration: none; }
  .reads { list-style: none; margin: 0 0 6px; padding: 0; display: grid; gap: 4px; }
  .reads li { font: 700 20px/1.15 var(--font-display); }
  .reads li.alert { color: var(--ls-red); }
  .reads li:not(.alert) { font: 500 15px var(--font-sans); color: var(--text-muted); }
  .fresh { margin: 0 0 14px; font-size: var(--text-xs); color: var(--text-muted); }
  .live { color: var(--ls-ibad); font-family: var(--font-mono); }
  .fresh.live { color: var(--ls-ibad); }
  .maps { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .map { position: relative; aspect-ratio: 1.25; border-radius: 5px; overflow: hidden; color: #eee5d5; text-decoration: none; background: #15110d; }
  .map img { width: 100%; height: 100%; object-fit: cover; }
  .map span { position: absolute; inset: auto 0 0; padding: 30px 10px 10px; background: linear-gradient(transparent, rgba(12,10,8,.95)); }
  .map b { display: block; font: 700 20px var(--font-display); }
  .map small { font: 500 11px var(--font-mono); letter-spacing: .06em; color: #d4c4a9; }
  .guild { display: grid; gap: 2px; padding: 12px 0; text-decoration: none; border-top: 1px solid var(--border-subtle); margin-top: 12px; }
  .guild b { font: 600 18px var(--font-display); letter-spacing: .04em; }
  .guild span { font-size: var(--text-sm); color: var(--text-muted); }
  .links { display: flex; flex-wrap: wrap; gap: 8px 18px; margin: 12px 0 0; }
  .footnote { margin: 32px 0 0; font-size: var(--text-xs); color: var(--text-muted); }
  @media (max-width: 1100px) { .cols { grid-template-columns: 1fr 1fr; } }
  @media (max-width: 759px) { .page { padding: 12px 12px 32px; } .cols { grid-template-columns: 1fr; gap: 12px; margin-top: 20px; } }
</style>
