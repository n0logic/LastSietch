// Shared unread-mail count for the topbar bell badge. A single $state proxy so
// the badge in the layout and the Mailbox surface stay in sync: reading the inbox
// marks messages read on the server, then calls refresh() to pull the new count.
// Session-gated; any failure degrades to 0 (no fabricated badge for anon).
import { api } from './api.js';

export const mailbox = $state({
  unread: 0,
  status: 'idle', // 'idle' | 'loading' | 'ready' | 'error'
});

/** Pull the unread count from the server. Never throws; anon/failure -> 0. */
export async function refreshUnread() {
  mailbox.status = 'loading';
  try {
    const r = await api.messages.unreadCount();
    mailbox.unread = Number(r?.count) || 0;
    mailbox.status = 'ready';
  } catch (e) {
    mailbox.unread = 0;
    mailbox.status = 'error';
  }
}

/** Optimistic local decrement (e.g. after reading one message) so the badge
 *  responds immediately; a subsequent refreshUnread() reconciles with truth. */
export function decrementUnread(n = 1) {
  mailbox.unread = Math.max(0, mailbox.unread - n);
}
