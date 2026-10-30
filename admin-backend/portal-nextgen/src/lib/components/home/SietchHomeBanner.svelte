<script>
  // Seven Nights of the Sietch (Halloween 2026): the home page's way in to /sietch.
  // Shown only while /portal/features answers sietch:true (LASTSIETCH_SIETCH_NIGHTS_ENABLED),
  // so it leaves the page with the nav entry. Night math comes from the copy table,
  // the same instants the event page counts down to.
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import { api } from '$lib/api.js';
  import { COPY } from '$lib/components/sietch/copy.js';
  import LiveCountdown from '$lib/components/LiveCountdown.svelte';

  let on = $state(false);
  let now = $state(Date.now());

  onMount(() => {
    let mounted = true;
    api.server.features().then((d) => { if (mounted) on = d?.features?.sietch === true; }).catch(() => {});
    const t = setInterval(() => { now = Date.now(); }, 30000);
    return () => { mounted = false; clearInterval(t); };
  });

  let night = $derived(COPY.nights.find((c) => now >= Date.parse(c.starts_at) && now < Date.parse(c.ends_at)) || null);
  let before = $derived(now < Date.parse(COPY.event.starts_at));
  let after = $derived(now >= Date.parse(COPY.event.ends_at));
</script>

{#if on && !after}
  <a class="sn-banner" href={`${base}/sietch`}>
    <img src="/img/v2/banners/seven-nights.webp" alt="" width="1200" height="400" />
    <span class="sn-text">
      <span class="sn-kicker">{COPY.event.name}{#if night} · Night {night.n} of 7{/if}</span>
      {#if night}
        <b>{night.title}</b>
        <span class="sn-hook">{night.hook}</span>
        <span class="sn-time"><LiveCountdown target={night.ends_at} prefix="Tonight ends in" /></span>
      {:else if before}
        <b>The Sietch opens its doors at dusk</b>
        <span class="sn-time"><LiveCountdown target={COPY.event.starts_at} prefix="Night 1 opens in" /></span>
      {/if}
      <span class="sn-cta">Open Seven Nights <span aria-hidden="true">↗</span></span>
    </span>
  </a>
{/if}

<style>
  .sn-banner { position: relative; display: block; margin: 0 0 20px; border-radius: 6px; overflow: hidden; min-height: 150px;
    color: #f1e6d2; background: #120d0a; text-decoration: none; border: 1px solid var(--accent-soft); }
  .sn-banner img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; opacity: .55; transition: transform .25s; }
  .sn-banner:hover img { transform: scale(1.02); }
  .sn-text { position: relative; display: flex; flex-direction: column; gap: 6px; padding: 20px 24px;
    background: linear-gradient(90deg, rgba(12,9,7,.92) 0%, rgba(12,9,7,.7) 55%, transparent 100%); max-width: 640px; }
  .sn-kicker { font-size: 12px; letter-spacing: .08em; text-transform: uppercase; color: var(--accent-text); }
  .sn-text b { font: 700 26px var(--font-display); line-height: 1.15; }
  .sn-hook { font-size: 14px; color: #d9cbb2; }
  .sn-time { font-size: 13px; color: #d9cbb2; }
  .sn-cta { margin-top: 6px; align-self: flex-start; padding: 8px 14px; min-height: 40px; display: inline-flex; align-items: center; gap: 6px;
    font-size: 14px; font-weight: 600; color: var(--bg-deep); background: var(--accent-text); border-radius: 4px; }
  @media (max-width: 640px) {
    .sn-text { padding: 16px; max-width: none; background: linear-gradient(rgba(12,9,7,.75), rgba(12,9,7,.92)); }
    .sn-text b { font-size: 21px; }
  }
</style>
