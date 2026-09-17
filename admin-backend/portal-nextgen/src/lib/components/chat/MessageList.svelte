<script>
  // The message pane. Scrolls its own column, sticks to the newest row while the
  // reader is at the bottom, and holds the reader's place when an older page is
  // prepended (the whole point of loading older on scroll is that the sentence
  // you were reading does not move).
  //
  // A reader who has scrolled UP is not dragged back down by a new arrival: the
  // pane offers a jump instead. That jump animates only when the viewer has not
  // asked for reduced motion, which is also why the scroll is done in script and
  // not with a CSS scroll-behavior the media query would have to fight.
  import { detectQuality } from '$lib/quality.js';
  import MessageRow from '$lib/components/chat/MessageRow.svelte';

  let {
    channel = '',
    channelLabel = '',
    messages = [],
    hasMore = false,
    loading = false,
    loadingOlder = false,
    canModerate = false,
    emptyText = '',
    onloadolder,
    onchanged,
  } = $props();

  // Within this many pixels of the bottom counts as "at the bottom".
  const STICK_PX = 48;
  // Loading the next page this far from the top keeps the fetch ahead of the
  // reader instead of stalling them at the edge.
  const TOP_PX = 96;

  const reducedMotion = detectQuality().reducedMotion;

  let el = $state(null);
  let stick = true;
  let unseen = $state(0);

  // Captured before every DOM patch so a prepend can be undone in the scroll
  // position: the new rows add height above the viewport, and the reader's
  // offset from the OLD top is the thing to restore.
  let preHeight = 0;
  let preTop = 0;
  let prevFirst = null;
  let prevLast = null;
  let prevLen = 0;
  let prevChannel = '';

  function firstId(list) {
    for (const m of list) if (m && m.id != null) return m.id;
    return null;
  }

  function lastId(list) {
    for (let i = list.length - 1; i >= 0; i -= 1) {
      if (list[i] && list[i].id != null) return list[i].id;
    }
    return null;
  }

  function toBottom(smooth = false) {
    if (!el) return;
    stick = true;
    unseen = 0;
    if (smooth && !reducedMotion) el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
    else el.scrollTop = el.scrollHeight;
  }

  function onScroll() {
    if (!el) return;
    stick = el.scrollHeight - el.scrollTop - el.clientHeight < STICK_PX;
    if (stick) unseen = 0;
    if (el.scrollTop < TOP_PX && hasMore && !loadingOlder) onloadolder?.();
  }

  $effect.pre(() => {
    messages.length;
    if (!el) return;
    preHeight = el.scrollHeight;
    preTop = el.scrollTop;
  });

  // Both decisions read the row IDS, never the length. Once a channel reaches
  // the store's MAX_ROWS every new row trims an old one, so the length stops
  // changing while the conversation carries on: a length test would silently
  // stop following new messages on exactly the busiest channels.
  $effect(() => {
    const len = messages.length;
    const first = firstId(messages);
    const last = lastId(messages);
    if (!el) return;
    if (channel !== prevChannel) {
      prevChannel = channel;
      prevFirst = first;
      prevLast = last;
      prevLen = len;
      stick = true;
      unseen = 0;
      el.scrollTop = el.scrollHeight;
      return;
    }
    // An older page prepends SMALLER ids. A cap trim moves the first id too,
    // but forward, so the direction is the honest test and equality is not.
    const prepended = prevFirst != null && first != null && first < prevFirst;
    // The newest id advancing is an arrival. The length test stays as well, for
    // the optimistic row: it has no id yet, so it moves the length and nothing else.
    const appended = (prevLast != null && last != null && last > prevLast) || len > prevLen;
    if (prepended) {
      el.scrollTop = el.scrollHeight - preHeight + preTop;
    } else if (appended) {
      if (stick) el.scrollTop = el.scrollHeight;
      else unseen += Math.max(1, len - prevLen);
    }
    prevFirst = first;
    prevLast = last;
    prevLen = len;
  });
</script>

<div class="pane-wrap">
  <div class="pane" bind:this={el} onscroll={onScroll} tabindex="-1" role="log" aria-live="polite" aria-label="Messages">
    {#if hasMore}
      <div class="older">
        <button type="button" class="more mono" onclick={() => onloadolder?.()} disabled={loadingOlder}>
          {loadingOlder ? 'loading' : 'Older messages'}
        </button>
      </div>
    {/if}

    {#if loading && messages.length === 0}
      <p class="skeleton">loading</p>
    {:else if messages.length === 0}
      <p class="empty">{emptyText}</p>
    {:else}
      {#each messages as m (m.id != null ? `id:${m.id}` : `key:${m.client_key}`)}
        <MessageRow message={m} {channel} {channelLabel} {canModerate} {onchanged} />
      {/each}
    {/if}
  </div>

  {#if unseen > 0}
    <button type="button" class="jump mono" onclick={() => toBottom(true)}>
      {unseen} new
    </button>
  {/if}
</div>

<style>
  .pane-wrap { position: relative; display: flex; flex-direction: column; min-height: 0; flex: 1; }
  .pane {
    flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain;
    display: flex; flex-direction: column; gap: 2px;
    padding: var(--space-2) 0;
  }
  .pane:focus-visible { outline: none; }

  .older { display: flex; justify-content: center; padding: var(--space-2) 0; }
  .more {
    font-size: var(--text-xs); letter-spacing: .12em; text-transform: uppercase;
    color: var(--text-muted); background: transparent;
    border: 1px solid var(--edge); border-radius: var(--radius-sm);
    padding: var(--space-1) var(--space-3); cursor: pointer;
  }
  .more:hover:not(:disabled) { color: var(--text); border-color: var(--edge-hi); }
  .more:disabled { opacity: .45; cursor: default; }
  .more:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

  .empty, .skeleton {
    margin: auto 0; padding: var(--space-5) var(--space-3);
    color: var(--text-muted); font-size: var(--text-sm); text-align: center;
  }

  .jump {
    position: absolute; left: 50%; transform: translateX(-50%);
    bottom: var(--space-3); z-index: 1;
    font-size: var(--text-xs); letter-spacing: .1em; text-transform: uppercase;
    color: var(--bg-deep); background: var(--accent); border: 1px solid var(--accent);
    border-radius: 999px; padding: var(--space-1) var(--space-4); cursor: pointer;
    box-shadow: 0 0 10px var(--accent-glow);
  }
  .jump:focus-visible { outline: 2px solid var(--accent-bright); outline-offset: 2px; }
</style>
