// The Home switch for the redesign (P1). One read of /portal/features per page
// view (the server caches it for a minute), plus the owner's ?shell= override.
// See shellFlag.js for the rules; this file only does the I/O.
import { api } from './api.js';
import { SHELL_FLAG_KEY, SHELL_KEY, resolveShell, shellChoice, shellGuess } from './shellFlag.js';

export const shell = $state({
  next: false,
  status: 'loading', // loading | ready
});

function readQuery() {
  try { return new URLSearchParams(location.search).get('shell'); } catch (e) { return null; }
}

function readStored() {
  try { return localStorage.getItem(SHELL_KEY); } catch (e) { return null; }
}

function readLastFlag() {
  try { return localStorage.getItem(SHELL_FLAG_KEY); } catch (e) { return null; }
}

/** True when the switch is unread but the override or the last read says it is on.
 *  Pages use it to hold a loading panel instead of drawing the shipped page first. */
export function shellExpectedNext() {
  return shellGuess({ query: readQuery(), stored: readStored(), lastFlag: readLastFlag() }) === true;
}

function remember(choice) {
  try { localStorage.setItem(SHELL_KEY, choice); } catch (e) {}
}

/** Resolve the switch. Never throws; a failed read leaves the shipped Home. */
export async function loadShell() {
  const query = readQuery();
  if (shellChoice(query)) remember(query);
  let flag = false;
  try {
    const data = await api.server.features();
    flag = data?.features?.shell_next === true;
  } catch (e) {
    flag = false;
  }
  try { localStorage.setItem(SHELL_FLAG_KEY, flag ? '1' : '0'); } catch (e) {}
  shell.next = resolveShell({ flag, query, stored: readStored() });
  shell.status = 'ready';
}
