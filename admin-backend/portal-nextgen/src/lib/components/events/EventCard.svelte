<script>
  // One event in a list. Props only: no store, no api, no fetch. Everything this
  // card knows arrives in `event`, and everything it wants done goes back out
  // through `onOpen` and `onToggleRemind`.
  //
  // Three rules the card exists to keep:
  //   * A NULL FIELD RENDERS ABSENT. No map, no host, no end time and no
  //     description are four things we were not told, and "Unknown" or a zero in
  //     their place would be four statements about the event we cannot make.
  //   * TIMES PRINT IN UTC, with the viewer's local reading as a hint beside
  //     them. UTC is the server's clock and the one the announcement is written
  //     against; the local line is a convenience and is labelled as one.
  //   * IBAD BLUE IS FOR "STARTING NOW" AND NOTHING ELSE. It is the one live
  //     reading on this card (the wall clock has passed the start and the event
  //     has not ended); the kind, the status and the chrome all stay amber.
  //
  // The description is player-facing copy written by an operator and is rendered
  // as TEXT. It is never {@html}.
  import EventBanner from './EventBanner.svelte';
  import EventKindChip from './EventKindChip.svelte';
  import RemindToggle from './RemindToggle.svelte';

  let { event = null, reminded = null, busy = false, onToggleRemind, onOpen } = $props();

  const HOUR_MS = 60 * 60 * 1000;

  const UTC_FMT = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'UTC', year: 'numeric', month: 'short', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  });
  const LOCAL_FMT = new Intl.DateTimeFormat(undefined, {
    month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  });

  // "YYYY-MM-DD HH:MM:SSZ". A stamp that reaches us without its zone marker is
  // still a UTC stamp, so the Z is supplied rather than letting Date.parse read
  // it as the viewer's local time and shift the event by hours.
  function parseUtc(s) {
    if (typeof s !== 'string' || !s.trim()) return null;
    let v = s.trim().replace(' ', 'T');
    if (!/(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(v)) v += 'Z';
    const t = Date.parse(v);
    return Number.isNaN(t) ? null : t;
  }
  function utcLabel(s) {
    const t = parseUtc(s);
    return t == null ? '' : `${UTC_FMT.format(t)} UTC`;
  }
  function localHint(s) {
    const t = parseUtc(s);
    return t == null ? '' : `${LOCAL_FMT.format(t)} your time`;
  }

  let title = $derived(String(event?.title || ''));
  let startsUtc = $derived(utcLabel(event?.starts_utc));
  let startsLocal = $derived(localHint(event?.starts_utc));
  let endsUtc = $derived(utcLabel(event?.ends_utc));
  let mapName = $derived(String(event?.map || '').trim());
  let host = $derived(String(event?.host || '').trim());
  let description = $derived(String(event?.description || '').trim());
  let cancelled = $derived(event?.status === 'cancelled');

  // No timer: the route reloads the list on its own cadence and a fresh `event`
  // re-derives this. A per-card interval would be one timer per row for a state
  // that changes once an hour.
  let live = $derived.by(() => {
    const ev = event;
    if (!ev || ev.status === 'cancelled') return false;
    const s = parseUtc(ev.starts_utc);
    if (s == null) return false;
    const now = Date.now();
    if (now < s || now - s > HOUR_MS) return false;
    const e = parseUtc(ev.ends_utc);
    return e == null ? true : now <= e;
  });

  // A started or cancelled event has nothing left to remind anyone about.
  let past = $derived.by(() => {
    const s = parseUtc(event?.starts_utc);
    return s != null && Date.now() >= s;
  });
</script>

<article class="card" class:live class:cancelled>
  <EventBanner banner={event?.banner} />

  <div class="body">
    <div class="head">
      <h3 class="etitle">
        {#if onOpen}
          <button class="open" type="button" onclick={() => onOpen(event?.id)}>{title}</button>
        {:else}
          {title}
        {/if}
      </h3>
      <EventKindChip kind={event?.kind} />
    </div>

    {#if cancelled}
      <p class="flag cancelled mono">Cancelled</p>
    {:else if live}
      <p class="flag now mono">Starting now</p>
    {/if}

    <dl class="meta mono">
      {#if startsUtc}
        <div>
          <dt>starts</dt>
          <dd>{startsUtc}{#if startsLocal}<span class="hint">{startsLocal}</span>{/if}</dd>
        </div>
      {/if}
      {#if endsUtc}
        <div><dt>ends</dt><dd>{endsUtc}</dd></div>
      {/if}
      {#if mapName}
        <div><dt>map</dt><dd>{mapName}</dd></div>
      {/if}
      {#if host}
        <div><dt>host</dt><dd>{host}</dd></div>
      {/if}
    </dl>

    {#if description}
      <p class="desc">{description}</p>
    {/if}

    <div class="foot">
      <RemindToggle
        {reminded} {busy}
        disabled={cancelled || past}
        onToggle={() => onToggleRemind?.(event?.id)}
      />
    </div>
  </div>
</article>

<style>
  .card {
    display: flex; flex-direction: column; gap: 0;
    background: var(--panel); border: 1px solid var(--edge);
    border-radius: var(--radius-lg); overflow: hidden;
    box-shadow: inset 0 1px 0 var(--metal-hi), 0 18px 44px -28px var(--shadow-cast);
  }
  /* The one live reading on this card, and the one place Ibad is allowed. */
  .card.live { border-color: color-mix(in srgb, var(--ls-ibad) 55%, var(--edge)); }
  .card.cancelled { opacity: .72; }

  .body { display: flex; flex-direction: column; gap: var(--space-3); padding: var(--space-4); }
  .head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-3); }
  .etitle { font-size: var(--text-lg); letter-spacing: .03em; text-transform: uppercase; min-width: 0; }
  .open {
    font: inherit; letter-spacing: inherit; text-transform: inherit; text-align: left;
    color: var(--text); background: none; border: 0; padding: 0; cursor: pointer;
  }
  .open:hover { color: var(--accent-text); }
  .open:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }

  .flag { margin: 0; font-size: var(--text-xs); letter-spacing: .16em; text-transform: uppercase; }
  .flag.now { color: var(--ls-ibad); }
  .flag.cancelled { color: var(--ls-red); }

  .meta { display: flex; flex-wrap: wrap; gap: var(--space-2) var(--space-4); margin: 0; font-size: var(--text-xs); }
  .meta div { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .meta dt { color: var(--text-muted); text-transform: uppercase; letter-spacing: .1em; font-size: 10px; }
  .meta dd { margin: 0; color: var(--text); }
  .meta .hint { display: block; color: var(--text-muted); font-size: 10px; letter-spacing: .04em; }

  .desc {
    margin: 0; font-size: var(--text-sm); line-height: 1.55; color: var(--text-muted);
    white-space: pre-wrap; overflow-wrap: anywhere;
    display: -webkit-box; -webkit-line-clamp: 4; line-clamp: 4;
    -webkit-box-orient: vertical; overflow: hidden;
  }

  .foot { display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; }
</style>
