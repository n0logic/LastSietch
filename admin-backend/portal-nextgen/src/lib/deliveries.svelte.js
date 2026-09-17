// The packages the server sent this player: the Welcome Package on a new
// account, the Return Package after a long time away. ONE read, no polling: a
// package lands once in weeks and the panel is on a page the player has to open,
// so a timer here would spend a request every minute to re-learn the same answer.
//
// `available: false` is the game host not answering. It is NOT an empty mailbox
// and it is NOT an error: the request succeeded and told us it could not see the
// packages. The three are separate states because they are three different
// sentences to a player who is waiting on something.
//
// A null sub-object stays null. Nothing here fills a missing count with 0.
import { api } from './api.js';

export const deliveries = $state({
  status: 'idle',   // idle | loading | ready | empty | error
  available: null,  // true | false | null (not read yet)
  packages: [],     // newest first: {kind, label, granted_at, state, legs[], items[]}
  skip: null,       // {notified_at, eligible_at} when a re-roll landed in the cooldown
  readAt: null,     // when the server read the game host
});

export async function loadDeliveries() {
  deliveries.status = 'loading';
  try {
    const r = await api.deliveries.list();
    const available = r?.available === true;
    const packages = Array.isArray(r?.packages) ? r.packages : [];
    const skip = r?.skip || null;
    deliveries.available = available;
    deliveries.packages = packages;
    deliveries.skip = skip;
    deliveries.readAt = r?.read_at || null;
    // Empty is a READ that came back with nothing in it. An unavailable read
    // never earns it: the host may be holding a package we simply cannot see.
    deliveries.status = available && packages.length === 0 && skip === null
      ? 'empty'
      : 'ready';
  } catch (e) {
    deliveries.available = null;
    deliveries.packages = [];
    deliveries.skip = null;
    deliveries.readAt = null;
    deliveries.status = 'error';
  }
}
