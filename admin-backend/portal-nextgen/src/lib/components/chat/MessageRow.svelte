<script>
  // One message. Char name, HH:MM local, body. The body is PLAIN TEXT: no
  // innerHTML, no {@html}, no markdown. It is split into TOKENS by
  // $lib/chat/links.js and each one is rendered through interpolation, so the
  // only markup this row can ever produce is an <a> around a host the client
  // has checked against its OWN allowlist. A url the allowlist does not carry
  // stays text, whatever the server chose to store.
  //
  // `kind` is the server's (plan 7b): `emote` is one italic sentence with the
  // speaker inside it, `roll` carries a die glyph, `system` is this tab's own
  // ephemeral answer and is muted with no actions on it, `say` is a message.
  //
  // A deleted row STAYS. It renders "message removed" in place so the list does
  // not jump under a reader who was mid-sentence, which is also why the server
  // returns the row with an empty body rather than dropping it.
  //
  // The action cluster is lane D's RowActions: this row hands it the message,
  // the channel and whether the viewer moderates here, and takes an onchanged
  // callback back so the surface can refresh after a delete or a mute.
  //
  // `channelLabel` is the channel's HUMAN name, and it is not decoration: the
  // mute dialog behind these actions says which room the mute covers, and
  // without the label it would say "guild:41" to the person deciding.
  import RowActions from '$lib/components/chat/RowActions.svelte';
  import { tokenize } from '$lib/chat/links.js';

  let {
    message,
    channel = '',
    channelLabel = '',
    canModerate = false,
    onchanged,
  } = $props();

  // The wire stamp is UTC and may arrive with or without a zone marker; a bare
  // "2026-09-04 11:02:03" parsed as local time would print the wrong hour.
  function parseUtc(s) {
    if (typeof s !== 'string' || !s.trim()) return null;
    let v = s.trim().replace(' ', 'T');
    if (!/(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(v)) v += 'Z';
    const t = Date.parse(v);
    return Number.isNaN(t) ? null : t;
  }

  const pad = (n) => String(n).padStart(2, '0');

  let stamp = $derived.by(() => {
    const t = parseUtc(message?.created_utc);
    if (t == null) return '';
    const d = new Date(t);
    return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  });

  let mine = $derived(message?.mine === true);
  let deleted = $derived(message?.deleted === true);
  let pending = $derived(message?.pending === true);
  let failed = $derived(message?.failed === true);

  let kind = $derived(message?.kind || 'say');
  let emote = $derived(kind === 'emote');
  let roll = $derived(kind === 'roll');
  let system = $derived(kind === 'system');

  let tokens = $derived(tokenize(deleted ? '' : (message?.body || '')));
</script>

<article class="row" class:mine class:deleted class:pending class:failed class:emote class:system>
  <header class="head">
    {#if !emote && !system}<span class="who" class:me={mine}>{message?.char_name || ''}</span>{/if}
    {#if stamp}<time class="when mono" datetime={message?.created_utc}>{stamp}</time>{/if}
    {#if !deleted && !pending && !system}
      <RowActions {message} {channel} {channelLabel} {canModerate} {onchanged} />
    {/if}
  </header>
  {#if deleted}
    <p class="gone">message removed</p>
  {:else}
    <!-- One line on purpose: the paragraph is pre-wrap, so a newline or an
         indent between these tags would be printed inside the message. -->
    <p class="body">{#if roll}<span class="glyph" aria-hidden="true">&#9860;</span>{/if}{#if emote}{message?.char_name || ''} {/if}{#each tokens as t, i (i)}{#if t.t === 'url'}<a class="link" href={t.href} target="_blank" rel="noopener noreferrer nofollow">{t.label}</a>{:else}{t.s}{/if}{/each}</p>
  {/if}
  {#if failed}
    <p class="failed-note mono" role="status">not sent</p>
  {/if}
</article>

<style>
  .row {
    display: flex; flex-direction: column; gap: 2px;
    padding: var(--space-2) var(--space-3);
    border-left: 2px solid transparent;
  }
  /* Own rows are tinted from the right, so a glance down the column separates
     what the player said from what was said to them without a second colour. */
  .row.mine {
    border-left-color: transparent;
    border-right: 2px solid color-mix(in srgb, var(--accent) 40%, transparent);
    background: color-mix(in srgb, var(--accent) 5%, transparent);
  }
  .row.pending { opacity: .62; }
  .row.failed { border-right-color: var(--ls-red); }

  .head { display: flex; align-items: baseline; gap: var(--space-2); min-height: 1.1em; }
  /* The actions trigger rests invisible and reveals on its own hover, focus and
     open state (RowActions owns those, plus always-on where there is no hover at
     all). This adds the ROW as a fourth reveal, so the cluster appears with the
     message the pointer is actually over rather than only under the trigger. */
  .row:hover :global(.rowactions .more) { opacity: 1; }
  .who {
    font-family: var(--font-display); font-size: var(--text-sm);
    letter-spacing: .06em; color: var(--text);
  }
  .who.me { color: var(--accent-text); }
  .when { font-size: var(--text-xs); color: var(--text-muted); }

  .body {
    margin: 0; font-size: var(--text-sm); line-height: 1.5; color: var(--text);
    /* Newlines are kept, long unbroken strings wrap instead of stretching the
       pane. Both matter: the server allows up to 5 newlines in a body. */
    white-space: pre-wrap; overflow-wrap: anywhere;
  }
  /* An emote is one italic sentence with the speaker inside it, which is why
     the name is rendered in the BODY here and not in the header above. */
  .row.emote .body { font-style: italic; color: var(--text); }
  /* A system row is this tab answering the player. Nobody said it, so it does
     not carry a speaker's weight. */
  .row.system .body { color: var(--text-muted); }
  .glyph { margin-right: .4em; }

  /* A link is chrome the player may follow, so it takes the amber text accent.
     It is never Ibad blue: that is reserved for live game data. */
  .body .link { color: var(--accent-text); text-decoration: underline; text-underline-offset: 2px; }
  .body .link:hover { color: var(--accent-bright); }
  .body .link:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

  .gone {
    margin: 0; font-size: var(--text-sm); line-height: 1.5;
    color: var(--text-muted); font-style: italic;
  }
  .failed-note { margin: 0; font-size: var(--text-xs); color: var(--ls-red); letter-spacing: .1em; }
</style>
