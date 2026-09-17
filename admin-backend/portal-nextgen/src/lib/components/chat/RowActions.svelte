<script>
  // The three moderation verbs that hang off ONE chat message: Report, Delete,
  // Mute author. Rendered by MessageRow, which owns the row; this component owns
  // nothing but the actions and the two dialogs behind them.
  //
  // The gates here HIDE controls that the server would refuse anyway. They are
  // not the decision: portal_chat_mod.py re-checks admin / guild leader / author
  // on every write, and the five-minute author window is measured there against
  // the row's own created_utc. A clock skewed forward on the player's machine
  // buys them a Delete button and a `forbidden` when they press it, which is the
  // right way round. Never the other way: nothing here grants anything.
  //
  // Deleted rows carry no actions at all. The row stays in the list so the view
  // does not jump, but there is nothing left to report, remove or mute over: the
  // body is already gone and the author is already known to whoever removed it.
  //
  // The trigger follows storage/ItemCell's `.more`: dim at rest, full on hover,
  // on keyboard focus and while its menu is open, and always visible on a device
  // with no hover at all. It is a real <button> in the tab order, so the whole
  // set is reachable without a pointer.
  import { api } from '$lib/api.js';
  import ReportDialog from './ReportDialog.svelte';
  import MuteDialog from './MuteDialog.svelte';

  let {
    message,          // { id, char_name, body, created_utc, mine, deleted }
    channel,          // channel id the row was read from
    channelLabel = '',
    canModerate = false,
    onchanged,        // called after a write that changed what the list shows
  } = $props();

  // The author's own window, mirrored from plan 1d. Five minutes, from the
  // message's own stamp, never from a local "posted just now" flag.
  const AUTHOR_WINDOW_MS = 5 * 60 * 1000;

  const REFUSALS = {
    forbidden: 'That message is no longer yours to remove.',
    not_found: 'That message is already gone.',
    chat_disabled: 'Chat is being fitted. Back soon.',
  };

  // "YYYY-MM-DD HH:MM:SSZ", the one wire format the portal reads. A stamp with
  // no zone marker is still UTC; supplying the Z stops Date.parse from reading
  // it as the viewer's local clock.
  function parseUtc(s) {
    if (typeof s !== 'string' || !s.trim()) return null;
    let v = s.trim().replace(' ', 'T');
    if (!/(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(v)) v += 'Z';
    const t = Date.parse(v);
    return Number.isNaN(t) ? null : t;
  }

  import { clickOutside } from '$lib/actions/clickOutside.js';
  let open = $state(false);
  let dialog = $state(null); // null | 'report' | 'mute'
  let busy = $state(false);
  let error = $state('');
  // Re-read on every menu open rather than on a timer: a row whose window has
  // just closed must not keep a live Delete, and a ticking clock per message is
  // a timer per message.
  let now = $state(Date.now());

  // An optimistic row carries id:null until the server row replaces it (and a
  // FAILED send keeps id:null for good). Every verb here addresses a message by
  // its server id, so a row that has not got one yet has nothing to report,
  // remove or mute over: the actions appear when the message actually exists.
  let hasId = $derived(message?.id != null);
  let deleted = $derived(!!message?.deleted);
  let mine = $derived(!!message?.mine);
  let postedAt = $derived(parseUtc(message?.created_utc));
  let inAuthorWindow = $derived(postedAt != null && now - postedAt < AUTHOR_WINDOW_MS);
  let canDelete = $derived(canModerate || (mine && inAuthorWindow));
  let author = $derived(String(message?.char_name || ''));

  function toggle() {
    now = Date.now();
    error = '';
    open = !open;
  }

  function closeAll() {
    open = false;
    dialog = null;
    error = '';
  }

  function onKeydown(e) {
    if (e.key === 'Escape' && open && !dialog) {
      e.stopPropagation();
      closeAll();
    }
  }

  async function remove() {
    if (busy) return;
    busy = true;
    error = '';
    try {
      await api.chat.remove(channel, message?.id);
      closeAll();
      onchanged?.();
    } catch (err) {
      // Refusals arrive as a 200 with {ok:false, error}, which sendCsrfJSON
      // rethrows with the envelope on err.data. `forbidden` is the one a player
      // will actually meet: the five-minute window closing between the render
      // and the click.
      error = REFUSALS[err?.data?.error] || 'That message could not be removed right now.';
    } finally {
      busy = false;
    }
  }

  function done() {
    closeAll();
    onchanged?.();
  }
</script>

<svelte:window onkeydown={onKeydown} />

{#if hasId && !deleted}
  <div class="rowactions" class:open use:clickOutside={() => { if (open && !dialog) closeAll(); }}>
    <button
      class="more mono" type="button"
      aria-label="Message actions for {author}"
      aria-expanded={open}
      onclick={toggle}
    >&hellip;</button>

    {#if open}
      <div class="pop" role="menu">
        <button class="pop-item" type="button" role="menuitem" onclick={() => { dialog = 'report'; }}>
          Report
        </button>
        {#if canDelete}
          <button class="pop-item" type="button" role="menuitem" disabled={busy} onclick={remove}>
            {busy ? 'Removing' : 'Delete'}
          </button>
        {/if}
        {#if canModerate}
          <button class="pop-item" type="button" role="menuitem" onclick={() => { dialog = 'mute'; }}>
            Mute author&hellip;
          </button>
        {/if}
        <button class="pop-item close" type="button" onclick={closeAll}>Close</button>
        {#if error}<p class="err" role="alert">{error}</p>{/if}
      </div>
    {/if}
  </div>

  {#if dialog === 'report'}
    <ReportDialog
      {channel}
      messageId={message?.id}
      {author}
      onDone={done}
      onClose={() => { dialog = null; }}
    />
  {:else if dialog === 'mute'}
    <MuteDialog
      charName={author}
      {channel}
      {channelLabel}
      onDone={done}
      onClose={() => { dialog = null; }}
    />
  {/if}
{/if}

<style>
  .rowactions { position: relative; flex: 0 0 auto; }
  .more {
    display: grid; place-items: center; line-height: 1;
    width: 20px; height: 20px; padding: 0;
    color: var(--text-muted); background: transparent;
    border: 1px solid var(--edge); border-radius: 3px; cursor: pointer;
    opacity: 0; transition: opacity var(--motion-fast) var(--ease-out),
                            color var(--motion-fast) var(--ease-out),
                            border-color var(--motion-fast) var(--ease-out);
  }
  .rowactions:hover .more,
  .rowactions:focus-within .more,
  .rowactions.open .more,
  .more:focus-visible { opacity: 1; }
  .more:hover { color: var(--accent-text); border-color: var(--accent); }
  /* Nothing to hover on a touch screen, so the trigger never hides there. */
  @media (hover: none) { .more { opacity: 1; } }

  .pop {
    /* The trigger sits at the LEFT of the row (after name and time), so the menu
       hangs from its left edge; right-anchored it ran off the pane (QA 2026-09-04). */
    position: absolute; left: 0; top: calc(100% + 4px); z-index: 5;
    min-width: 11rem; display: flex; flex-direction: column; gap: 2px;
    padding: var(--space-2);
    background: var(--panel); border: 1px solid var(--edge);
    border-radius: var(--radius-sm);
    box-shadow: 0 18px 44px -28px var(--shadow-cast);
  }
  .pop-item {
    text-align: left; background: transparent; color: var(--text);
    border: 0; border-radius: var(--radius-sm); cursor: pointer;
    font-size: var(--text-sm); padding: var(--space-1) var(--space-2);
  }
  .pop-item:hover:not(:disabled) { color: var(--accent-text); background: var(--metal-1); }
  .pop-item:disabled { opacity: .45; cursor: not-allowed; }
  .pop-item.close { color: var(--text-muted); font-size: var(--text-xs); }
  .err { margin: var(--space-1) 0 0; font-size: var(--text-xs); color: var(--ls-red); }
</style>
