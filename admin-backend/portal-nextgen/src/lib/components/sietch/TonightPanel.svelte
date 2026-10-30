<script>
  // Tonight: what the desert asks tonight, the Knock, and how much of tonight's
  // earning is already in hand. Every number here is the server's.
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import Notice from '$lib/components/Notice.svelte';
  import KnockButton from './KnockButton.svelte';
  import { nightCopy } from '$lib/sietch.svelte.js';
  import { COPY } from './copy.js';
  import { detectQuality } from '$lib/quality.js';

  let { data, phase = 'before' } = $props();

  const ART_V = '20261024a';
  let cardFailed = $state(false);
  const saveData = detectQuality().saveData;

  let night = $derived(data?.night || null);
  let copy = $derived(nightCopy(night));
  let earning = $derived(data?.flags?.earning === true);
  // Only these activities are earned by Knocking; the others earn elsewhere.
  let knocks = $derived(['porches', 'riddles', 'stall', 'seek', 'haunt'].includes(night?.activity));
  let clue = $derived(night?.clue_current || null);
  let spots = $derived(Array.isArray(night?.spots_public) ? night.spots_public : []);
  let knockBlock = $derived(
    !earning ? COPY.knock.closed
    : data?.eligible === false ? COPY.knock.ineligible
    : ''
  );
  const BARS = ['visit', 'knock', 'landsraad', 'activity'];
  function bar(key) {
    const p = data?.progress?.[key];
    const cap = Number(p?.cap) || 0;
    const used = Math.min(Number(p?.used) || 0, cap);
    return { cap, used, pct: cap ? Math.round((used / cap) * 100) : 0 };
  }
</script>

{#if phase === 'before'}
  <CarvedSlab>
    <p class="kicker mono">Before dusk</p>
    <h2>The doors open {COPY.event.starts_text}</h2>
    <p class="lede">{COPY.event.summary}</p>
    <p class="fine">{COPY.event.dst_note}</p>
  </CarvedSlab>
{:else if phase === 'night' && !night}
  <!-- The server sends no night while the event gate is off (earning=false): the
       doors are shut for now, not the event over. -->
  <CarvedSlab>
    <p class="kicker mono">Tonight</p>
    <h2>The lanterns are dark</h2>
    <p class="lede">{COPY.knock.closed}</p>
  </CarvedSlab>
{:else if phase === 'after' || !night}
  <CarvedSlab>
    <p class="kicker mono">After the last night</p>
    <h2>The Seven Nights are over</h2>
    <p class="lede">Thank you for keeping the lanterns lit. Your LS Tokens stay in your Hoard.</p>
    {#if data?.flags?.shop}<p class="fine">The Token Stall stays open until {COPY.stall.closes_text}.</p>{/if}
  </CarvedSlab>
{:else}
  {#if !earning}
    <Notice tone="warn" text={COPY.knock.closed} />
  {/if}
  <div class="grid">
    <CarvedSlab hot={earning}>
      {#if !saveData && !cardFailed && night.n >= 1 && night.n <= 7}
        <img class="card" src={`/img/sietch-nights/night${night.n}-card-1200x800.webp?v=${ART_V}`} alt="" loading="lazy" decoding="async" width="1200" height="800" onerror={() => { cardFailed = true; }} />
      {/if}
      <p class="kicker mono">Night {night.n} | {copy.runs_text}</p>
      <h2>{copy.title}</h2>
      <p class="hook">{copy.hook}</p>
      <p class="how">{copy.how}</p>
      <p class="award mono">{copy.award_text}</p>

      {#if clue?.riddle}
        <blockquote class="riddle">
          <span class="mono">Riddle {clue.seq} of {clue.of}</span>
          {clue.riddle}
        </blockquote>
      {/if}

      {#if spots.length}
        <ul class="spots" aria-label="Tonight's doors">
          {#each spots as s (s.id)}
            <li class:done={s.done}>
              <span class="dot" aria-hidden="true"></span>
              <span class="name">{s.name}</span>
              {#if s.done}<span class="tick mono">Knocked</span>{/if}
            </li>
          {/each}
        </ul>
        <p class="fine">Doors are in Hagga Basin. The map shows the area, never the exact door.</p>
      {/if}

      {#if knocks}
        <div class="knock-wrap">
          <KnockButton disabled={!!knockBlock} reason={knockBlock} />
        </div>
      {/if}
    </CarvedSlab>

    <CarvedSlab sharp>
      <p class="kicker mono">Tonight's take</p>
      <ul class="bars">
        {#each BARS as key (key)}
          {@const b = bar(key)}
          {#if b.cap > 0}
            <li>
              <div class="bar-head">
                <span>{COPY.progress[key]}</span>
                <span class="mono">{b.used} / {b.cap}</span>
              </div>
              <div class="track" role="progressbar" aria-label={COPY.progress[key]} aria-valuemin="0" aria-valuemax={b.cap} aria-valuenow={b.used}>
                <span class="fill" class:full={b.used >= b.cap} style="width: {b.pct}%"></span>
              </div>
            </li>
          {/if}
        {/each}
      </ul>
      <div class="balance">
        <span class="bal-num mono">{Number(data?.balance) || 0}</span>
        <span class="bal-lbl">{COPY.event.currency} in your Hoard</span>
      </div>
      {#if data?.eligible === false}
        <p class="fine">{COPY.knock.ineligible} Your progress still shows here.</p>
      {/if}
    </CarvedSlab>
  </div>
{/if}

<style>
  h2 { font-size: var(--text-2xl); text-transform: uppercase; margin: 0 0 var(--space-3); }
  .lede { color: var(--text-muted); max-width: 62ch; }
  .fine { font-size: var(--text-sm); color: var(--text-muted); margin: var(--space-2) 0 0; }
  .grid { display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(0, 1fr); gap: var(--space-4); margin-top: var(--space-3); align-items: start; }
  @media (max-width: 860px) { .grid { grid-template-columns: 1fr; } }
  .card {
    display: block; width: calc(100% + 2 * var(--space-5)); max-width: none; height: auto; aspect-ratio: 3 / 1.4; object-fit: cover;
    margin: calc(-1 * var(--space-5)) calc(-1 * var(--space-5)) var(--space-4);
    border-radius: var(--radius-md) var(--radius-md) 0 0; border-bottom: 1px solid var(--edge);
  }
  .hook { font-size: var(--text-lg); font-style: italic; margin: 0 0 var(--space-3); color: var(--text); }
  .how { color: var(--text-muted); margin: 0 0 var(--space-2); max-width: 62ch; }
  .award { font-size: var(--text-sm); color: var(--ls-melange-hi); margin: 0; letter-spacing: .04em; }
  :global([data-theme='day']) .award { color: #6a2fa0; }
  .riddle {
    margin: var(--space-4) 0 0; padding: var(--space-3) var(--space-4);
    border-left: 2px solid var(--ls-melange-soft); background: color-mix(in srgb, var(--ls-melange-soft) 8%, transparent);
    font-style: italic; font-size: var(--text-lg);
  }
  .riddle span { display: block; font-style: normal; font-size: var(--text-xs); letter-spacing: .14em; text-transform: uppercase; color: var(--text-muted); margin-bottom: var(--space-1); }
  .spots { list-style: none; margin: var(--space-4) 0 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-2); }
  .spots li { display: flex; align-items: center; gap: var(--space-2); }
  .dot { width: 10px; height: 10px; border-radius: 50%; border: 1px solid var(--ls-melange-hi); }
  .spots li.done .dot { background: var(--ls-amber-bright); border-color: var(--ls-amber-bright); box-shadow: 0 0 8px var(--ls-amber-glow); }
  .spots li.done .name { color: var(--text-muted); }
  .tick { font-size: var(--text-xs); color: var(--accent-text); letter-spacing: .1em; text-transform: uppercase; }
  .knock-wrap { margin-top: var(--space-5); padding-top: var(--space-4); border-top: 1px solid var(--edge); }

  .bars { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--space-3); }
  .bar-head { display: flex; justify-content: space-between; font-size: var(--text-sm); margin-bottom: 4px; }
  .track { height: 8px; border-radius: 4px; background: var(--metal-0); border: 1px solid var(--edge); overflow: hidden; }
  .fill { display: block; height: 100%; background: linear-gradient(90deg, var(--ls-melange-soft), var(--ls-melange-hi)); transition: width var(--motion-mid) var(--ease-out); }
  .fill.full { background: linear-gradient(90deg, var(--ls-amber), var(--ls-amber-bright)); }
  .balance { display: flex; align-items: baseline; gap: var(--space-2); margin-top: var(--space-5); padding-top: var(--space-4); border-top: 1px solid var(--edge); }
  .bal-num { font-size: var(--text-3xl); font-weight: 700; color: var(--ls-amber-bright); line-height: 1; }
  :global([data-theme='day']) .bal-num { color: #8a5613; }
  .bal-lbl { color: var(--text-muted); font-size: var(--text-sm); }
</style>
