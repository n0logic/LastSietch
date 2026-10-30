<script>
  // The Events page's feature card (P2, direction A): this weekend as the one big
  // answer, next weekend beside it. Props only; the page resolves both slots with
  // describeSlot() and hands in the reminder wiring.
  //
  // Honesty: the kicker, the live dot, the countdown and the "confirmed" line all
  // come from heroState() and confirmedText(). A weekend is only "Live now" when
  // the runner read the effect back. A failed or missed effect shows no countdown,
  // no dot and says in words that it is not confirmed. Ibad blue is the confirmed
  // dot and the countdown, nothing else.
  import { base } from '$app/paths';
  import EffectChip from './EffectChip.svelte';
  import RemindToggle from './RemindToggle.svelte';
  import LiveCountdown from '$lib/components/LiveCountdown.svelte';
  import { heroState } from '$lib/shellHome.js';
  import { honestNote } from '$lib/eventsNext.js';
  import { timePair } from '$lib/weekend.js';

  let { slot = null, next = null, zone = '', reminded = null, busy = false, onToggleRemind } = $props();

  let st = $derived(heroState(slot));
  let note = $derived(slot ? honestNote(slot) : '');
  let target = $derived(slot && st.countdown ? new Date(st.countdown === 'end' ? slot.endMs : slot.startMs).toISOString() : null);
  let start = $derived(slot ? timePair(slot.startMs, zone) : { et: '', local: '' });
  let end = $derived(slot ? timePair(slot.endMs, zone) : { et: '', local: '' });
  let nextStart = $derived(next ? timePair(next.startMs, zone) : { et: '', local: '' });
  let remindable = $derived(slot?.event && !slot.open);
</script>

<section class="feature" aria-labelledby="ev-feature-title">
  <article class="main" data-state={st.kind}>
    <p class="kicker mono" class:live={st.verified}>
      <span class="dot" class:live={st.verified} aria-hidden="true"></span>{st.kicker}
    </p>
    <h2 id="ev-feature-title">
      {#if slot?.event}<a href={`${base}/events/${slot.event.id}`}>{slot.title}</a>{:else}{slot?.title || 'This weekend'}{/if}
    </h2>

    {#if slot?.info}
      <EffectChip effect={slot.effect} state={slot.state} scope={slot.scope} open={slot.open} detail={true} />
    {:else}
      <p class="lede">No set bonus for this weekend yet.</p>
    {/if}

    {#if note}<p class="note" class:warn={st.kind === 'notlive'}>{note}</p>{/if}

    {#if target}
      <p class="count">
        <span class="cl mono">{st.countdown === 'end' ? 'Ends in' : 'Starts in'}</span>
        <LiveCountdown {target} />
      </p>
    {/if}

    {#if slot}
      <dl class="when">
        <div>
          <dt>{slot.open ? 'Started' : 'Starts'}</dt>
          <dd>{start.et}{#if start.local}<span class="local">{start.local} your time</span>{/if}</dd>
        </div>
        <div>
          <dt>Ends</dt>
          <dd>{end.et}{#if end.local}<span class="local">{end.local} your time</span>{/if}</dd>
        </div>
      </dl>
    {/if}

    {#if remindable}
      <div class="acts"><RemindToggle {reminded} {busy} onToggle={() => onToggleRemind?.(slot.event.id)} /></div>
    {/if}
  </article>

  {#if next}
    <aside class="next" aria-label="Next weekend">
      <p class="kicker mono">Next weekend{#if next.planned && next.info} · Planned{/if}</p>
      <h3>{next.title}</h3>
      {#if next.info}
        <EffectChip effect={next.effect} state={next.state} scope={next.scope} open={next.open} />
      {:else}
        <p class="lede">No set bonus yet.</p>
      {/if}
      <p class="when-line">{nextStart.et}{#if nextStart.local}<span class="local">{nextStart.local} your time</span>{/if}</p>
    </aside>
  {/if}
</section>

<style>
  .feature { display: grid; gap: var(--space-4); grid-template-columns: 1fr; }
  @media (min-width: 900px) { .feature { grid-template-columns: minmax(0, 1.6fr) minmax(0, 1fr); } }

  .main, .next {
    position: relative; isolation: isolate; overflow: hidden; min-width: 0;
    padding: var(--space-5); background: var(--panel); border: 1px solid var(--edge);
    border-top: 2px solid var(--accent-soft); border-radius: var(--radius-sm);
    display: grid; gap: var(--space-3); align-content: start; justify-items: start;
  }
  .main::before {
    content: ""; position: absolute; inset: 0; z-index: -1; pointer-events: none;
    background: radial-gradient(60% 80% at 100% 0%, color-mix(in srgb, var(--accent) 22%, transparent), transparent 70%);
  }
  .main[data-state='live'] { border-top-color: var(--ls-ibad); }
  .main[data-state='notlive'] { border-top-color: var(--ls-red); }

  .kicker { margin: 0; display: flex; align-items: center; gap: 10px; font-size: var(--text-xs); letter-spacing: .16em; text-transform: uppercase; color: var(--accent-text); }
  .kicker.live { color: var(--ls-ibad); }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--accent); }
  .dot.live { background: var(--ls-ibad); box-shadow: 0 0 0 3px var(--ls-ibad-glow); }
  @media (prefers-reduced-motion: no-preference) {
    .dot.live { animation: pulse 2.4s ease-in-out infinite; }
  }
  @keyframes pulse { 50% { box-shadow: 0 0 0 6px transparent; } }

  h2 { margin: 0; font: 700 clamp(2.2rem, 7vw, 4.2rem)/.98 var(--font-display); text-transform: uppercase; letter-spacing: .01em; overflow-wrap: anywhere; }
  h2 a { color: inherit; text-decoration: none; }
  h2 a:hover { color: var(--accent-text); }
  h3 { margin: 0; font: 600 1.5rem/1.1 var(--font-display); text-transform: uppercase; letter-spacing: .04em; }
  .lede, .note { margin: 0; color: var(--text-muted); font-size: var(--text-sm); max-width: 52ch; }
  .note.warn { color: var(--ls-red); }
  .count { margin: var(--space-1) 0 0; display: flex; align-items: baseline; gap: var(--space-3); font: 500 clamp(1.4rem, 5vw, 2.2rem)/1 var(--font-mono); }
  .count .cl { font-size: var(--text-xs); letter-spacing: .16em; text-transform: uppercase; color: var(--text-muted); }
  .count :global(*) { font: inherit; }
  .when { margin: 0; display: grid; gap: var(--space-2); font-size: var(--text-sm); width: 100%; }
  .when div { display: grid; grid-template-columns: 4.5rem 1fr; gap: var(--space-2); }
  .when dt { color: var(--text-muted); }
  .when dd { margin: 0; overflow-wrap: anywhere; }
  .when-line { margin: 0; font-size: var(--text-sm); }
  .local { display: block; color: var(--text-muted); font-size: var(--text-xs); }
  .acts { display: flex; gap: var(--space-3); }
</style>
