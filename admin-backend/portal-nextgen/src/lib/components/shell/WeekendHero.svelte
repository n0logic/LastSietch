<script>
  // The redesigned Home's first screen (direction A, "The Weekend Clock"): this
  // weekend's event as a poster, one amber action, and the rotation beside it.
  // Props only. The caller resolves the slots with $lib/weekend.js describeSlot.
  //
  // Honesty: the kicker, the dot and the countdown come from heroState(), which
  // only calls a weekend "Live now" when the runner read the effect back. A missed
  // or failed effect shows no countdown and no live dot.
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import EffectChip from '$lib/components/events/EffectChip.svelte';
  import LiveCountdown from '$lib/components/LiveCountdown.svelte';
  import { heroState, weekendAction } from '$lib/shellHome.js';
  import { timePair } from '$lib/weekend.js';

  let { slot = null, rotation = [], appearance, now = Date.now() } = $props();

  let zone = $state('');
  let arrival = $state(false);
  onMount(() => {
    try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (e) { zone = ''; }
    try {
      arrival = !matchMedia('(prefers-reduced-motion: reduce)').matches && !sessionStorage.getItem('shell-arrived');
      sessionStorage.setItem('shell-arrived', '1');
    } catch (e) {}
  });

  let st = $derived(heroState(slot));
  let action = $derived(weekendAction(slot?.effect || null, base));
  let plate = $derived(`/img/v3/plates/${appearance.map}-${appearance.band}-${appearance.weather}`);
  let target = $derived(slot && st.countdown ? new Date(st.countdown === 'end' ? slot.endMs : slot.startMs).toISOString() : null);
  let endPair = $derived(slot ? timePair(slot.endMs, zone) : { et: '', local: '' });
  let startPair = $derived(slot ? timePair(slot.startMs, zone) : { et: '', local: '' });
  let titleText = $derived(slot?.title || 'This weekend');
</script>

<section class="hero" class:arrival aria-labelledby="wk-title" data-state={st.kind}>
  <picture>
    <source media="(max-width: 759px)" srcset={`${plate}-small.jpg`} />
    <img class="plate" src={`${plate}.jpg`} alt="" fetchpriority="high" width="1280" height="720" />
  </picture>
  <div class="sun" aria-hidden="true"></div>

  <div class="copy">
    <p class="kicker mono" class:live={st.verified}>
      <span class="dot" class:live={st.verified} aria-hidden="true"></span>{st.kicker}
    </p>
    <h1 id="wk-title">{titleText}</h1>
    {#if slot?.info}
      <EffectChip effect={slot.effect} state={slot.state} scope={slot.scope} open={slot.open} detail={true} />
    {:else}
      <p class="lede">No set bonus this weekend yet. The Events page has the plan.</p>
    {/if}

    {#if target}
      <p class="count">
        <span class="cl mono">{st.countdown === 'end' ? 'Ends in' : 'Starts in'}</span>
        <LiveCountdown {target} />
      </p>
    {/if}
    {#if slot}
      <p class="when">
        {st.countdown === 'end' ? `Ends ${endPair.et}` : `Starts ${startPair.et}`}
        {#if (st.countdown === 'end' ? endPair.local : startPair.local)}
          <span class="local">{st.countdown === 'end' ? endPair.local : startPair.local} your time</span>
        {/if}
      </p>
    {/if}

    <div class="acts">
      <a class="primary" href={action.href}>{action.label} <span aria-hidden="true">↗</span></a>
      <a class="ghost" href={slot?.event ? `${base}/events/${slot.event.id}` : `${base}/events`}>
        {slot?.event ? 'Details and reminder' : 'All events'}
      </a>
    </div>
  </div>

  {#if rotation.length > 0}
    <aside class="rot" aria-label="Weekend rotation">
      <p class="rot-label mono">The weekend rotation</p>
      <ol>
        {#each rotation as r}
          <li class:now={r.open}>
            <span class="d mono">{r.open ? 'Now' : r.dayText}</span>
            <span class="n">{r.title}<small>{r.info ? r.info.chip : 'No set bonus'}</small></span>
            {#if r.restart}<span class="rs mono">Restart</span>{:else if r.open && r.state === 'running'}<span class="rs live mono">Live</span>{/if}
          </li>
        {/each}
      </ol>
      <a class="rot-more" href={`${base}/events`}>Full calendar <span aria-hidden="true">↗</span></a>
    </aside>
  {/if}
</section>

<style>
  .hero {
    position: relative; isolation: isolate; overflow: hidden;
    display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(280px, .85fr); gap: var(--space-6); align-items: end;
    min-height: min(68vh, 640px); padding: 64px var(--space-6) 40px;
    border: 1px solid var(--edge); border-radius: 6px; background: var(--ls-sand-1);
  }
  .hero > picture { position: absolute; inset: 0; z-index: -3; }
  .plate { position: absolute; inset: 0; z-index: -3; width: 100%; height: 100%; object-fit: cover; object-position: 62% 40%; }
  .hero::before {
    content: ""; position: absolute; inset: 0; z-index: -2;
    background: linear-gradient(90deg, rgba(8,6,4,.92) 0%, rgba(8,6,4,.72) 36%, rgba(8,6,4,.12) 68%), linear-gradient(0deg, var(--bg) 0%, transparent 32%);
  }
  .sun {
    position: absolute; inset: 0; z-index: -1; pointer-events: none; mix-blend-mode: screen;
    background: radial-gradient(60% 55% at 88% 0%, rgba(232,132,58,.38), rgba(184,48,122,.18) 50%, transparent 74%);
  }
  @media (prefers-reduced-motion: no-preference) {
    .sun { animation: breathe 9s ease-in-out infinite alternate; }
    .hero.arrival .plate { animation: settle 1.6s var(--ease-out) both; }
  }
  @keyframes breathe { from { opacity: .65; } to { opacity: 1; } }
  @keyframes settle { from { transform: scale(1.14); filter: brightness(.45); } to { transform: scale(1); filter: none; } }

  .copy { max-width: 640px; display: grid; gap: var(--space-3); justify-items: start; }
  .kicker { margin: 0; display: flex; align-items: center; gap: 10px; font-size: var(--text-xs); letter-spacing: .16em; text-transform: uppercase; color: var(--accent-text); }
  .kicker.live { color: var(--ls-ibad); }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--accent); }
  .dot.live { background: var(--ls-ibad); box-shadow: 0 0 0 3px var(--ls-ibad-glow); }
  h1 { margin: 0; font: 700 clamp(2.4rem, 6.4vw, 5.6rem)/.95 var(--font-display); text-transform: uppercase; letter-spacing: .01em; color: #f3ead7; text-shadow: 0 2px 30px rgba(0,0,0,.6); }
  .lede { margin: 0; color: #e0d4bb; }
  .hero :global(.meaning) { color: #e0d4bb; font-size: var(--text-base); max-width: 46ch; }
  .count { margin: var(--space-2) 0 0; display: flex; align-items: baseline; gap: var(--space-3); font: 500 clamp(1.5rem, 3.4vw, 2.4rem)/1 var(--font-mono); }
  .count .cl { font-size: var(--text-xs); letter-spacing: .16em; text-transform: uppercase; color: var(--text-muted); }
  .count :global(*) { font: inherit; }
  .when { margin: 0; font-size: var(--text-sm); color: #d4c7ad; }
  .local { display: block; font-size: var(--text-xs); color: var(--text-muted); }
  .acts { display: flex; flex-wrap: wrap; gap: var(--space-3); margin-top: var(--space-3); }
  .primary, .ghost { display: inline-flex; align-items: center; justify-content: center; gap: 8px; min-height: 44px; padding: 0 22px; border-radius: 4px; text-decoration: none; font: 700 15px var(--font-display); letter-spacing: .12em; text-transform: uppercase; }
  .primary { background: linear-gradient(180deg, var(--accent-bright), var(--accent)); color: #1a0f02; border: 1px solid var(--accent-bright); box-shadow: 0 0 24px var(--accent-glow); }
  .primary:hover { filter: brightness(1.08); }
  .ghost { color: var(--text); border: 1px solid var(--edge-hi); background: rgba(12,10,7,.5); }
  .ghost:hover { border-color: var(--accent-soft); color: var(--accent-text); }

  .rot { padding: 16px 18px 12px; background: rgba(12,10,7,.74); backdrop-filter: blur(10px); border: 1px solid var(--edge-hi); border-radius: 6px; }
  .rot-label { margin: 0 0 8px; font-size: var(--text-xs); letter-spacing: .16em; text-transform: uppercase; color: var(--text-muted); }
  .rot ol { list-style: none; margin: 0; padding: 0; }
  .rot li { display: grid; grid-template-columns: 72px 1fr auto; gap: 12px; align-items: center; min-height: 52px; padding: 8px 0; border-top: 1px solid var(--border-subtle); }
  .rot li:first-child { border-top: 0; }
  .rot li.now { margin-inline: -18px; padding-inline: 18px; border-left: 3px solid var(--accent-bright); background: linear-gradient(90deg, color-mix(in srgb, var(--accent) 14%, transparent), transparent); }
  .d { font-size: var(--text-xs); color: var(--text-muted); white-space: nowrap; }
  .n { font: 600 18px/1.1 var(--font-display); letter-spacing: .04em; text-transform: uppercase; }
  .n small { display: block; margin-top: 2px; font: 400 12px var(--font-sans); letter-spacing: 0; text-transform: none; color: var(--text-muted); }
  .rot li.now .n { color: var(--accent-text); }
  .rs { font-size: 11px; letter-spacing: .08em; text-transform: uppercase; padding: 2px 8px; border: 1px solid var(--edge-hi); border-radius: 99px; color: var(--text-muted); }
  .rs.live { color: var(--ls-ibad); border-color: var(--ls-ibad); }
  .rot-more { display: inline-block; margin-top: 8px; font-size: var(--text-sm); text-decoration: none; }

  @media (max-width: 900px) {
    .hero { grid-template-columns: 1fr; padding: 120px var(--space-4) var(--space-5); min-height: 0; gap: var(--space-5); }
    .hero::before { background: linear-gradient(0deg, rgba(8,6,4,.96) 42%, rgba(8,6,4,.1) 92%); }
    .acts a { flex: 1 1 100%; }
  }
</style>
