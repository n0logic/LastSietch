<script>
  // Chat surface: the channel rail, the message pane, and the composer. Linked
  // accounts only for reading AND posting, so an anonymous visitor gets the
  // login seal and no request is made on their behalf.
  //
  // Dark is a STATE, not an error. While LASTSIETCH_CHAT_ENABLED is off the server
  // answers every chat read with a refusal at 200, the store folds that into
  // `enabled:false`, and this page seals quietly. The channel read is polled, so
  // a flag flipped while the tab sat open seals it within a poll rather than
  // leaving a dead composer on screen.
  import { base } from '$app/paths';
  import { useAuthGate } from '$lib/authGate.svelte.js';
  import {
    chat, watchChat, openChannel, loadOlder, send, clearRefusal,
    canPost, canModerate, channelById, refusalText, rowsFor,
  } from '$lib/chat.svelte.js';
  import PageHeader from '$lib/components/PageHeader.svelte';
  import SealedPanel from '$lib/components/SealedPanel.svelte';
  import CarvedSlab from '$lib/components/CarvedSlab.svelte';
  import LiveDot from '$lib/components/LiveDot.svelte';
  import ChannelRail from '$lib/components/chat/ChannelRail.svelte';
  import MessageList from '$lib/components/chat/MessageList.svelte';
  import Composer from '$lib/components/chat/Composer.svelte';

  const gate = useAuthGate();

  // The store's subscription is torn down with the surface, which also closes
  // the stream: a backgrounded portal tab must not hold an open SSE connection
  // for a page nobody is on.
  $effect(() => {
    if (gate.status !== 'authed') return;
    return watchChat();
  });

  // First channel opens itself. The server decides the order, and the first row
  // it sends is the Sietch channel every linked player has.
  $effect(() => {
    if (!chat.enabled || chat.active || chat.channels.length === 0) return;
    const first = chat.channels[0];
    if (first) openChannel(first.id);
  });

  let active = $derived(chat.active);
  let activeRow = $derived(active ? channelById(active) : null);
  // The server's rows plus this tab's ephemeral command answers. The store
  // keeps the two lists apart; only the render puts them in one column.
  let messages = $derived(rowsFor(active));

  let muted = $derived.by(() => {
    const m = chat.me?.muted;
    if (!m) return null;
    // '' is a mute everywhere; anything else names the one channel it covers.
    if (m.channel && m.channel !== active) return null;
    return m;
  });

  let blocked = $derived.by(() => {
    if (!active) return '';
    if (muted) return 'muted';
    if (!canPost(active)) return 'not_member';
    if (chat.refusal === 'rate_limited' && chat.retryAfter > 0) return 'rate_limited';
    return '';
  });

  let composerReason = $derived.by(() => {
    if (!blocked) return '';
    if (blocked === 'not_member') return 'You can read this channel but not post in it.';
    if (blocked === 'muted' && muted?.until_utc) return 'You are muted here until ' + muted.until_utc + '.';
    return refusalText(blocked);
  });

  // A refusal the player can simply answer by typing something else (duplicate,
  // bad_request) gets a sentence but does NOT lock the box. Saying nothing at
  // all was the bug: the row goes grey, the message never lands, and the player
  // is left guessing whether the desert ate it.
  let composerNotice = $derived(!blocked && chat.refusal ? refusalText(chat.refusal) : '');

  // /chat/moderation is deliberately absent from the nav, so this is the only
  // way in. The link renders for a viewer the SERVER already told us moderates
  // somewhere (any channel with can_moderate), which covers both the admin and
  // the guild leader without this page knowing the difference between them. It
  // grants nothing: the page behind it gates itself, and a viewer who follows a
  // link they should not have gets its seal.
  let moderates = $derived(chat.channels.some((c) => c && c.can_moderate));

  function onselect(id) {
    if (id && id !== chat.active) openChannel(id);
  }

  // Lane D's row actions call this after a delete or a mute so the pane and the
  // rail reflect what just happened without waiting for the next stream event.
  function onchanged() {
    if (chat.active) openChannel(chat.active);
  }
</script>

<svelte:head>
  <title>Chat | Last Sietch</title>
</svelte:head>

<div class="page">
  <PageHeader
    kicker="Last Sietch | open channel"
    title="Chat"
    sub="Word across the sietch, your guild, your House, and the sands you are working."
  >
    {#if chat.enabled && moderates}
      <a class="mod-link mono" href={`${base}/chat/moderation`}>Moderation</a>
    {/if}
  </PageHeader>

  {#if gate.loading}
    <p class="skeleton">loading</p>
  {:else if gate.anon}
    <SealedPanel
      status="empty" action="login"
      emptyText="Sign in to read the channels and speak in them."
    />
  {:else if chat.status === 'loading' || chat.status === 'idle'}
    <SealedPanel status="loading" loadingText="opening the channels" />
  {:else if chat.status === 'error'}
    <SealedPanel
      status="error" width="prose"
      errorText="The channels could not be reached. Try again shortly."
    />
  {:else if !chat.enabled}
    <SealedPanel status="empty" width="prose" emptyText="Chat is being fitted. Back soon." />
  {:else if chat.channels.length === 0}
    <SealedPanel
      status="empty" width="prose"
      emptyText="No channels are open to this character yet."
    />
  {:else}
    <div class="board">
      <aside class="rail-col">
        <CarvedSlab>
          <ChannelRail channels={chat.channels} {active} {onselect} />
        </CarvedSlab>
      </aside>

      <CarvedSlab>
        <div class="pane-col">
          <header class="pane-head">
            <p class="kicker mono">{activeRow?.label || 'Channel'}</p>
            {#if chat.stream === 'open'}
              <span class="live mono"><LiveDot />live</span>
            {:else if chat.stream === 'reconnecting'}
              <span class="live mono idle"><LiveDot tone="idle" />reconnecting</span>
            {/if}
          </header>

          <MessageList
            channel={active}
            channelLabel={activeRow?.label || ''}
            {messages}
            hasMore={chat.hasMore[active] === true}
            loading={chat.loading[active] === true}
            loadingOlder={chat.older[active] === true}
            canModerate={canModerate(active)}
            emptyText="Nothing has been said here yet. Say the first thing."
            onloadolder={() => loadOlder(active)}
            {onchanged}
          />

          <Composer
            disabled={!!blocked}
            reason={composerReason}
            notice={composerNotice}
            retryAfter={chat.retryAfter}
            busy={chat.sending}
            onsend={send}
            ondismiss={clearRefusal}
          />
        </div>
      </CarvedSlab>
    </div>
  {/if}
</div>

<style>
  .page { max-width: var(--content-max); margin: 0 auto; padding: var(--space-6) var(--space-4) var(--space-8); }

  .board { display: grid; grid-template-columns: 220px minmax(0, 1fr); gap: var(--space-4); align-items: start; }

  .pane-col {
    display: flex; flex-direction: column; gap: var(--space-3);
    /* A fixed column so the pane scrolls instead of the page: a chat that grows
       the document pushes the composer off the bottom of the screen. */
    height: clamp(380px, 62vh, 680px); min-height: 0;
  }
  .pane-head { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); }
  /* Panel caption is chrome: amber. The live marker beside it is the one thing
     on this header reporting a live connection, so it carries the Ibad dot. */
  .kicker {
    color: var(--accent); text-transform: uppercase; letter-spacing: .28em;
    font-size: var(--text-xs); margin: 0;
  }
  .live {
    display: inline-flex; align-items: center; gap: var(--space-2);
    font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase;
    color: var(--ls-ibad);
  }
  .live.idle { color: var(--text-muted); }

  .skeleton { color: var(--text-muted); font-size: var(--text-sm); }

  .mod-link {
    display: inline-block; text-decoration: none;
    font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase;
    color: var(--accent-text); border: 1px solid var(--edge);
    border-radius: var(--radius-sm); padding: var(--space-1) var(--space-3);
  }
  .mod-link:hover { border-color: var(--accent); }
  .mod-link:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

  @media (max-width: 780px) {
    .board { grid-template-columns: minmax(0, 1fr); }
    .rail-col :global(.slab) { padding: var(--space-3); }
  }
</style>
