<script>
  // One event, at full size. The deep-linked view behind /events/{id}
  // and the only surface that carries the remind toggle wired to a write.
  //
  // Same three rules as the card, for the same reasons: a null field renders
  // absent (never "Unknown", never a zero), times print UTC with the viewer's
  // local reading as a labelled hint, and Ibad blue appears only on the "starting
  // now" state. The description is operator-written copy rendered as TEXT; it is
  // never {@html}.
  //
  // The banner carries the title here (this is the hero), so the route's
  // PageHeader is the only other place the name appears.
  import EventBanner from './EventBanner.svelte';
  import EventKindChip from './EventKindChip.svelte';
  import RemindToggle from './RemindToggle.svelte';

  let { event = null, reminded = null, busy = false, onToggleRemind } = $props();

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
  let endsLocal = $derived(localHint(event?.ends_utc));
  let mapName = $derived(String(event?.map || '').trim());
  let host = $derived(String(event?.host || '').trim());
  let description = $derived(String(event?.description || '').trim());
  let cancelled = $derived(event?.status === 'cancelled');

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

  let past = $derived.by(() => {
    const s = parseUtc(event?.starts_utc);
    return s != null && Date.now() >= s;
  });
</script>

<article class="detail" class:live class:cancelled>
  <EventBanner banner={event?.banner} {title} />

  <div class="body">
    <div class="head">
      <EventKindChip kind={event?.kind} />
      {#if cancelled}
        <p class="flag cancelled mono">Cancelled</p>
      {:else if live}
        <p class="flag now mono">Starting now</p>
      {/if}
    </div>

    <dl class="meta mono">
      {#if startsUtc}
        <div>
          <dt>starts</dt>
          <dd>{startsUtc}{#if startsLocal}<span class="hint">{startsLocal}</span>{/if}</dd>
        </div>
      {/if}
      {#if endsUtc}
        <div>
          <dt>ends</dt>
          <dd>{endsUtc}{#if endsLocal}<span class="hint">{endsLocal}</span>{/if}</dd>
        </div>
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
  .detail {
    display: flex; flex-direction: column;
    background: var(--panel); border: 1px solid var(--edge);
    border-radius: var(--radius-lg); overflow: hidden;
    box-shadow: inset 0 1px 0 var(--metal-hi), 0 18px 44px -28px var(--shadow-cast);
  }
  /* The one live reading on this page, and the one place Ibad is allowed. */
  .detail.live { border-color: color-mix(in srgb, var(--ls-ibad) 55%, var(--edge)); }
  .detail.cancelled { opacity: .78; }

  .body { display: flex; flex-direction: column; gap: var(--space-4); padding: var(--space-5); }
  .head { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; }

  .flag { margin: 0; font-size: var(--text-xs); letter-spacing: .16em; text-transform: uppercase; }
  .flag.now { color: var(--ls-ibad); }
  .flag.cancelled { color: var(--ls-red); }

  .meta { display: flex; flex-wrap: wrap; gap: var(--space-3) var(--space-5); margin: 0; font-size: var(--text-sm); }
  .meta div { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .meta dt { color: var(--text-muted); text-transform: uppercase; letter-spacing: .1em; font-size: 10px; }
  .meta dd { margin: 0; color: var(--text); }
  .meta .hint { display: block; color: var(--text-muted); font-size: var(--text-xs); letter-spacing: .04em; }

  .desc {
    margin: 0; font-size: var(--text-sm); line-height: 1.6; color: var(--text);
    white-space: pre-wrap; overflow-wrap: anywhere; max-width: 68ch;
  }

  .foot { display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; }
</style>
