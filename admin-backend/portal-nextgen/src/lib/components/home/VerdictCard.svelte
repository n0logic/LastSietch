<script>
  // The answer. One sentence in the h1 and at most three supporting lines under
  // it, chosen by the ladder in home.svelte.js / homeVerdict.js. This component
  // decides nothing: it is handed a verdict and prints it, so there is exactly
  // one place in the app where a rank is picked.
  //
  // IBAD BLUE IS RESERVED. The blue is the house signal for a value streaming
  // off the live server, and Home spends it on ranks 3 and 9 only: the storm
  // sweeping the map and the spice blowing, the two verdicts that are literally
  // a live read of the sand. Everything else, including the danger red of a
  // downed station, stays on amber chrome. Anything that glows blue everywhere
  // stops meaning "live" anywhere.
  import LiveCountdown from '$lib/components/LiveCountdown.svelte';
  import LiveDot from '$lib/components/LiveDot.svelte';

  let { verdict = null, subLines = [], compact = false } = $props();

  let v = $derived(verdict || { rank: 0, text: '', tone: 'idle', timerUtc: null });
  let ibad = $derived(v.rank === 3 || v.rank === 9);
  let lines = $derived(Array.isArray(subLines) ? subLines : []);
</script>

<h1 class="verdict verdict-{v.tone}" class:ibad class:compact>
  {#if v.tone !== 'idle'}<LiveDot tone={v.tone} />{/if}
  {v.text}
</h1>

{#if v.timerUtc}
  <p class="timer mono"><LiveCountdown target={v.timerUtc} /></p>
{/if}

{#if lines.length}
  <ul class="lines">
    {#each lines as line}<li>{line}</li>{/each}
  </ul>
{/if}

<style>
  .verdict {
    font-family: var(--font-display); font-weight: 700;
    font-size: clamp(1.9rem, 5.2vw, 3.4rem); line-height: 1.02; letter-spacing: .005em;
    margin: var(--space-2) 0 var(--space-3); display: flex; align-items: center; gap: var(--space-3);
    text-wrap: balance; color: var(--text);
  }
  .verdict-danger { color: var(--ls-red); text-shadow: 0 0 22px rgba(214,90,68,.25); }
  .verdict.compact { font-size: clamp(25px, 2.6vw, 36px); line-height: 1.12; text-shadow: none; }
  .verdict-warn { color: var(--ls-yellow); }
  .verdict-live { color: var(--text); }
  /* Ranks 3 and 9 ONLY. The rank test lives in the class binding above, so this
     rule cannot be reached by any other verdict. */
  .verdict.ibad { text-shadow: 0 0 26px var(--ls-ibad-glow); }
  .verdict-live.ibad { color: var(--ls-ibad); }

  .timer {
    margin: 0 0 var(--space-2); color: var(--text-muted);
    font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase;
  }
  .lines {
    list-style: none; margin: 0 0 var(--space-3); padding: 0;
    display: flex; flex-direction: column; gap: var(--space-1);
  }
  .lines li {
    color: var(--text-muted); font-size: var(--text-sm); line-height: 1.45;
    padding-left: var(--space-3); border-left: 2px solid var(--edge);
  }
</style>
