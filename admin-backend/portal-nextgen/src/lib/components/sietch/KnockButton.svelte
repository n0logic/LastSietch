<script>
  // The Knock: one big carved door-knocker button and the line the desert answers
  // with. The position check happens server-side; the button only asks. A 30 s
  // client rest mirrors the server's rate limit so the player can see why.
  import { sietch, knock, knockLine, KNOCK_COOLDOWN_MS } from '$lib/sietch.svelte.js';
  import { COPY } from './copy.js';

  let { disabled = false, reason = '' } = $props();

  let now = $state(Date.now());
  $effect(() => {
    const id = setInterval(() => { now = Date.now(); }, 1000);
    return () => clearInterval(id);
  });

  let restLeft = $derived(sietch.knock.at ? Math.max(0, Math.ceil((sietch.knock.at + KNOCK_COOLDOWN_MS - now) / 1000)) : 0);
  let resting = $derived(restLeft > 0);
  let r = $derived(sietch.knock.result);
  let line = $derived(knockLine(r));
  let tone = $derived(!r ? '' : r.result === 'treat' ? 'treat' : r.result === 'band' ? `band ${r.band}` : r.result === 'error' ? 'error' : 'quiet');
  const BANDS = ['cold', 'cool', 'warm', 'scorching'];
</script>

<div class="knock">
  <button
    class="knocker"
    class:busy={sietch.knock.busy}
    disabled={disabled || sietch.knock.busy || resting}
    onclick={() => knock()}
    aria-describedby="knock-answer"
  >
    <span class="ring" aria-hidden="true"></span>
    <span class="word">{sietch.knock.busy ? COPY.knock.button_busy : COPY.knock.button}</span>
    {#if resting && !sietch.knock.busy}<span class="rest mono" aria-hidden="true">{restLeft}s</span>{/if}
  </button>

  <div class="answer {tone}" id="knock-answer" role="status" aria-live="polite">
    {#if disabled && reason}
      <p>{reason}</p>
    {:else if r}
      {#if r.result === 'treat' && r.award}<p class="award mono">+{r.award} {r.award === 1 ? 'LS Token' : COPY.event.currency}</p>{/if}
      <p>{line}</p>
      {#if r.result === 'band'}
        <ol class="therm" aria-hidden="true">
          {#each BANDS as b, i (b)}<li class:on={BANDS.indexOf(r.band) >= i} class={b}></li>{/each}
        </ol>
      {/if}
      {#if r.next_clue?.riddle}
        <blockquote class="riddle">
          <span class="mono">Riddle {r.next_clue.seq} of {r.next_clue.of}</span>
          {r.next_clue.riddle}
        </blockquote>
      {/if}
    {:else}
      <p class="hint">Stand at the door in game, then press Knock.</p>
    {/if}
  </div>
</div>

<style>
  .knock { display: flex; align-items: center; gap: var(--space-5); flex-wrap: wrap; }
  .knocker {
    position: relative; flex: 0 0 auto;
    width: 128px; height: 128px; border-radius: 50%;
    display: inline-flex; flex-direction: column; align-items: center; justify-content: center;
    font-family: var(--font-display); font-size: var(--text-xl); letter-spacing: .12em; text-transform: uppercase;
    color: #f3ecff; cursor: pointer;
    border: 2px solid color-mix(in srgb, var(--ls-melange-hi) 70%, transparent);
    background:
      radial-gradient(circle at 50% 35%, rgba(255, 255, 255, .14), transparent 60%),
      radial-gradient(circle, #3a2463 0%, #1b1033 70%, #0d0820 100%);
    box-shadow: inset 0 -6px 16px rgba(0, 0, 0, .6), 0 0 24px var(--ls-melange-glow);
    transition: transform var(--motion-fast) var(--ease-out), box-shadow var(--motion-fast) var(--ease-out), filter var(--motion-fast) var(--ease-out);
  }
  .knocker:hover:not(:disabled) { transform: scale(1.03); box-shadow: inset 0 -6px 16px rgba(0, 0, 0, .6), 0 0 36px var(--ls-melange-glow); }
  .knocker:active:not(:disabled) { transform: scale(.97); }
  .knocker:disabled { cursor: not-allowed; filter: saturate(.35) brightness(.75); box-shadow: inset 0 -6px 16px rgba(0, 0, 0, .6); }
  .ring {
    position: absolute; inset: 10px; border-radius: 50%;
    border: 1px dashed color-mix(in srgb, var(--ls-melange-pale) 40%, transparent);
  }
  .knocker.busy .ring { animation: spin 1.6s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .rest { font-size: var(--text-xs); letter-spacing: .1em; color: var(--ls-melange-pale); margin-top: 2px; }

  .answer { flex: 1 1 16rem; min-width: 0; }
  .answer p { margin: 0 0 var(--space-2); }
  .hint { color: var(--text-muted); }
  .answer.quiet p { color: var(--text-muted); }
  .answer.error p { color: var(--ls-red); }
  .answer.treat p { color: var(--text); }
  .award {
    font-size: var(--text-2xl); font-weight: 700; color: var(--ls-amber-bright) !important;
    text-shadow: 0 0 18px var(--ls-amber-glow);
    animation: pop var(--motion-slow) var(--ease-out);
  }
  @keyframes pop { from { transform: translateY(6px) scale(.9); opacity: 0; } to { transform: none; opacity: 1; } }
  .therm { list-style: none; display: flex; gap: 4px; margin: var(--space-2) 0 0; padding: 0; }
  .therm li { width: 42px; height: 8px; border-radius: 4px; background: var(--metal-0); border: 1px solid var(--edge); }
  .therm li.on.cold { background: #5a7fb0; }
  .therm li.on.cool { background: #7f9a8a; }
  .therm li.on.warm { background: #d4891c; }
  .therm li.on.scorching { background: #e0502e; box-shadow: 0 0 10px rgba(224, 80, 46, .6); }
  .riddle {
    margin: var(--space-3) 0 0; padding: var(--space-3) var(--space-4);
    border-left: 2px solid var(--ls-melange-soft); background: color-mix(in srgb, var(--ls-melange-soft) 8%, transparent);
    font-style: italic;
  }
  .riddle span { display: block; font-style: normal; font-size: var(--text-xs); letter-spacing: .14em; text-transform: uppercase; color: var(--text-muted); margin-bottom: var(--space-1); }

  @media (max-width: 480px) {
    .knock { justify-content: center; text-align: center; }
    .therm { justify-content: center; }
  }
  @media (prefers-reduced-motion: reduce) {
    .knocker.busy .ring, .award { animation: none; }
  }
</style>
