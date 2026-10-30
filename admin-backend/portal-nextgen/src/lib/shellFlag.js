// Which Home a viewer gets: the redesigned one (LASTSIETCH_PORTAL_SHELL_NEXT_ENABLED, read
// through /portal/features as `shell_next`) or the shipped one.
//
// PURE. No store, no fetch, no storage access: the store hands in what it read and
// this decides, so the suite can run every combination under node.
//
// FAIL CLOSED. Anything other than an explicit true from the server, or an explicit
// owner override, is the shipped Home. An unread feature map, a failed read and an
// unknown override value all mean "old".
//
// The override exists so the owner can look at the redesign on the live site before
// the flag is flipped: `?shell=next` turns it on for this browser and remembers it,
// `?shell=old` turns it back off. It only changes what THIS browser draws.

export const SHELL_KEY = 'ls-shell';

/** 'next' | 'old' | null from a query string value or a stored value. */
export function shellChoice(value) {
  return value === 'next' || value === 'old' ? value : null;
}

/** The final answer. `query` beats `stored`; both beat the server flag. */
export function resolveShell({ flag, query, stored }) {
  const choice = shellChoice(query) || shellChoice(stored);
  if (choice === 'next') return true;
  if (choice === 'old') return false;
  return flag === true;
}

export const SHELL_FLAG_KEY = 'ls-shell-flag';

/** What the switch will most likely resolve to, before the features read answers:
 *  true, false, or null when nothing is known. The override wins; otherwise the
 *  flag value the last read saw ('1' or '0'). Only used to pick a loading panel
 *  over the shipped page; resolveShell() stays the one real answer. */
export function shellGuess({ query, stored, lastFlag }) {
  const choice = shellChoice(query) || shellChoice(stored);
  if (choice) return choice === 'next';
  if (lastFlag === '1') return true;
  if (lastFlag === '0') return false;
  return null;
}
