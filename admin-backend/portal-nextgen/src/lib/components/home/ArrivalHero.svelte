<script>
  import { onMount } from 'svelte';
  import { motion, loadMotion } from '$lib/ambient.svelte.js';

  let { appearance, signals = {}, fresh = false } = $props();
  let reduced = $state(true);
  let phone = $state(true);
  let visible = $state(true);
  let saver = $state(true);
  let arrival = $state(false);
  let video = $state(null);
  let failed = $state(false);
  let play = $derived(!reduced && !saver && visible && !failed && motion.preference !== 'still'
    && (motion.preference === 'full' || !phone));
  let plate = $derived(`/img/v3/plates/${appearance.map}-${appearance.band}-${appearance.weather}`);
  let loop = $derived(`/video/v3/loops/${appearance.map}${appearance.weather === 'storm' ? '-storm' : ''}.mp4`);

  onMount(() => {
    loadMotion();
    const reduce = matchMedia('(prefers-reduced-motion: reduce)');
    const mobile = matchMedia('(max-width: 759px)');
    const update = () => { reduced = reduce.matches; phone = mobile.matches; visible = !document.hidden; };
    update();
    saver = navigator.connection?.saveData === true || (navigator.deviceMemory != null && navigator.deviceMemory < 4);
    try {
      arrival = !reduced && motion.preference !== 'still' && !sessionStorage.getItem('fremkit-arrived');
      sessionStorage.setItem('fremkit-arrived', '1');
    } catch (e) {}
    reduce.addEventListener('change', update);
    mobile.addEventListener('change', update);
    document.addEventListener('visibilitychange', update);
    return () => {
      reduce.removeEventListener('change', update);
      mobile.removeEventListener('change', update);
      document.removeEventListener('visibilitychange', update);
    };
  });

  $effect(() => {
    if (!video) return;
    if (play) video.play().catch(() => { failed = true; });
    else video.pause();
  });
</script>

<section class="arrival-hero" class:arrival aria-label="Your desert" data-stand={appearance.map}>
  <picture>
    <source media="(max-width: 759px)" srcset={`${plate}-small.jpg`} />
    <img src={`${plate}.jpg`} alt="" fetchpriority="high" width="1280" height="720" />
  </picture>
  {#if play}
    <video bind:this={video} src={loop} aria-hidden="true" muted loop playsinline preload="none" onerror={() => (failed = true)}></video>
  {/if}
  <div class="scrim"></div>
  <div class="copy">
    <div class="badges"><span>{appearance.mode}</span>{#if appearance.untracked}<span>Untracked</span>{:else if fresh}<span class="live">Live conditions</span>{/if}</div>
    <p class="eyebrow">{signals.charOnline === true ? 'Your current board' : 'Your desert at a glance'}</p>
    <h2>{appearance.label}</h2>
    <p class="context">
      {#if signals.charName}{signals.charName} · {signals.charOnline === true ? 'online' : signals.charOnline === false ? 'offline' : 'status unavailable'}{:else}The Last Sietch Portal{/if}
    </p>
    <a class="bronze" href={appearance.href}>Open the war-table <span aria-hidden="true">↗</span></a>
  </div>
</section>

<style>
  .arrival-hero { position: relative; min-height: 330px; overflow: hidden; isolation: isolate; border: 1px solid var(--edge); border-radius: 8px; background: #15110d; }
  picture, img, video, .scrim { position: absolute; inset: 0; width: 100%; height: 100%; }
  img, video { object-fit: cover; object-position: 60% center; }
  video { opacity: .24; }
  .scrim { background: linear-gradient(90deg, rgba(12,10,8,.84), rgba(12,10,8,.1)), linear-gradient(0deg, rgba(12,10,8,.8), transparent 75%); }
  .copy { position: relative; padding: 28px; color: #eee5d5; }
  .badges { display: flex; gap: 8px; margin-bottom: 44px; }
  .badges span { font: 400 11px var(--font-mono); padding: 4px 8px; background: rgba(12,10,8,.7); border: 1px solid rgba(236,227,211,.2); border-radius: 3px; }
  .badges .live { color: var(--ls-ibad); }
  .eyebrow { font-size: 12px; margin: 0 0 4px; color: #d4c4a9; }
  h2 { font-size: clamp(38px, 5vw, 66px); line-height: 1; text-transform: uppercase; }
  .context { font-size: 13px; margin: 12px 0 18px; color: #d4c4a9; }
  .bronze { display: inline-flex; align-items: center; gap: 24px; min-height: 44px; padding: 10px 16px; border: 1px solid #a57d42; border-radius: 3px; color: #f6e2ba; background: linear-gradient(rgba(117,77,28,.84), rgba(66,42,15,.94)), url('/img/v3/textures/oxidized-bronze.jpg'); font-size: 14px; text-decoration: none; }
  .bronze:hover { filter: brightness(1.13); }
  .bronze:active { transform: translateY(1px); }
  .arrival picture { animation: arrive 1s var(--ease-out) both; }
  .arrival .copy { animation: copy-in .6s .15s var(--ease-out) both; }
  @keyframes arrive { from { opacity: .3; transform: scale(1.06); } to { opacity: 1; transform: scale(1); } }
  @keyframes copy-in { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
  @media (max-width: 759px) { .arrival-hero { min-height: 200px; } .copy { padding: 16px; } .badges { margin-bottom: 18px; } h2 { font-size: 38px; } .context { margin: 8px 0 12px; } .bronze { min-height: 40px; padding: 8px 12px; } }
  @media (prefers-reduced-motion: reduce) { .arrival picture, .arrival .copy { animation: none; } }
</style>
