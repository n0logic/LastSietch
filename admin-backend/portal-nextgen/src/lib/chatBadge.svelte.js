// Unread-chat count for the top-bar chat button, the mailbox.svelte.js pattern.
// Kept out of chat.svelte.js so every page does not load the whole chat store
// for one number.
//
// Two sources, one number. Away from /chat the badge polls the light
// /portal/chat/unread read once a minute, visible tabs only, never a stream.
// While /chat is open its live channel list is the truer count, so chat.svelte.js
// attaches it here and the poll stands down until the page lets go.
//
// FAIL CLOSED, BUT NOT ON A BLIP. Only an explicit chat_disabled refusal hides
// the button; a failed request keeps the last number, or the button flickers.
import { api } from './api.js';

const POLL_MS = 60_000;

export const chatBadge = $state({
  unread: 0,
  dark: false,   // the server said chat_disabled
  live: false,   // /chat is mounted and its channel list is the source
});

let liveTotal = null;
let watchers = 0;
let timer = null;

function visible() {
  return typeof document === 'undefined' || document.visibilityState !== 'hidden';
}

/** The number the badge shows. Read inside a $derived, so it tracks whichever
 *  source is in force. */
export function chatUnreadTotal() {
  return chatBadge.live && liveTotal ? liveTotal() : chatBadge.unread;
}

/** /chat hands over its live total while it is mounted. */
export function attachLive(total) {
  liveTotal = total;
  chatBadge.live = true;
}

/** /chat let go: keep its last count until the next poll reconciles. */
export function detachLive(lastTotal) {
  liveTotal = null;
  chatBadge.live = false;
  chatBadge.unread = Number(lastTotal) || 0;
}

async function loadBadge() {
  if (chatBadge.live || !visible()) return;
  try {
    const r = await api.chat.unread();
    if (r && r.ok === false) {
      if (r.error === 'chat_disabled') {
        chatBadge.dark = true;
        chatBadge.unread = 0;
      }
      return;
    }
    chatBadge.dark = false;
    chatBadge.unread = Number(r?.unread) || 0;
  } catch (e) {
    // Keep the last number: a transient failure is not a closed room.
  }
}

function onVisibility() {
  if (visible()) loadBadge();
}

/** Start the badge poll. Returns the teardown, so the layout can hand it straight
 *  back from $effect. */
export function watchChatUnread() {
  watchers += 1;
  if (watchers === 1) {
    loadBadge();
    timer = setInterval(loadBadge, POLL_MS);
    if (typeof document !== 'undefined') document.addEventListener('visibilitychange', onVisibility);
  }
  return () => {
    watchers = Math.max(0, watchers - 1);
    if (watchers > 0) return;
    if (timer != null) { clearInterval(timer); timer = null; }
    if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', onVisibility);
  };
}
