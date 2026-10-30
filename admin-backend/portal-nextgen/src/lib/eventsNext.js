// Pure helpers for the rebuilt Events page (P2). No Svelte, no store, no fetch: the
// components hand in plain values and the suite runs these under node. The weekend
// vocabulary stays in weekend.js and the hero's honesty rules stay in shellHome.js
// heroState(); this file only adds what the Events page needs on top.
import { ET, STATE_LABELS, parseUtc, timeText } from './weekend.js';
import { heroState } from './shellHome.js';

/** When the runner read the effect back on the servers, or null. Only a running
 *  effect carries a confirmation: a failed, missed, armed or unread one has none,
 *  whatever the row's timestamp says. */
export function confirmedMs(d) {
  if (!d || d.state !== 'running' || !d.event) return null;
  return parseUtc(d.event.effect_started_at);
}

/** "Confirmed live at 9:02 PM EDT" or ''. */
export function confirmedText(d) {
  const ms = confirmedMs(d);
  if (ms == null) return '';
  const t = new Intl.DateTimeFormat('en-US', {
    timeZone: ET, hour: 'numeric', minute: '2-digit', timeZoneName: 'short',
  }).format(ms);
  return `Confirmed live at ${t}`;
}

/** The sentence under the headline that says what we know and what we do not.
 *  Never claims more than the runner's state. */
export function honestNote(d) {
  const st = heroState(d);
  switch (st.kind) {
    case 'live':
      return st.verified
        ? (confirmedText(d) || 'The servers confirmed this bonus is live.')
        : 'On the events board. The servers have not reported this bonus back yet.';
    case 'notlive':
      return d.state === 'missed'
        ? 'This bonus did not start. It is not live.'
        : 'The servers have not confirmed this bonus is live. We will not call it live until they do.';
    case 'pending': return 'Set to start. The servers have not confirmed it yet.';
    case 'done': return 'This weekend has finished.';
    case 'planned': return 'In the weekend rotation. Not on the events board yet.';
    default: return '';
  }
}

/** One weekend on the month strip. `kind` drives the badge colour; only
 *  'live' (runner confirmed) may use live styling. */
export function stripEntry(d, now) {
  const restart = d.restart === true;
  if (!d.info) return { kind: 'open', text: 'No set bonus', restart: false };
  if (d.endMs <= now) {
    if (d.state === 'done') return { kind: 'done', text: 'Finished', restart };
    if (d.state === 'failed') return { kind: 'notlive', text: STATE_LABELS.failed, restart };
    if (d.state === 'missed') return { kind: 'notlive', text: STATE_LABELS.missed, restart };
    return { kind: 'ended', text: 'Ended', restart };
  }
  if (d.open) {
    const st = heroState(d);
    if (st.kind === 'live' && st.verified) return { kind: 'live', text: 'Live now', restart };
    if (st.kind === 'notlive') return { kind: 'notlive', text: d.state === 'missed' ? STATE_LABELS.missed : STATE_LABELS.failed, restart };
    if (st.kind === 'pending') return { kind: 'pending', text: 'Not live yet', restart };
    if (st.kind === 'live') return { kind: 'board', text: 'On the board', restart };
    return { kind: 'planned', text: 'Planned', restart };
  }
  return d.planned ? { kind: 'planned', text: 'Planned', restart } : { kind: 'scheduled', text: 'Scheduled', restart };
}

/** "November" in Eastern. */
export function monthName(ms) {
  return new Intl.DateTimeFormat('en-US', { timeZone: ET, month: 'long' }).format(ms);
}

/** The seven nights with where each stands at `now`. `nights` is COPY.nights. */
export function nightStates(nights, now) {
  return (Array.isArray(nights) ? nights : []).map((n) => {
    const start = Date.parse(n.starts_at);
    const end = Date.parse(n.ends_at);
    const state = now >= end ? 'past' : now >= start ? 'tonight' : 'upcoming';
    return { n: n.n, title: n.title, state, endsAt: n.ends_at };
  });
}

/** The newest `limit` past events, each with the effect words the page may print.
 *  A past row shows its runner state only when the column carried one. */
export function recentRows(past, limit = 6) {
  return (Array.isArray(past) ? past : []).slice(0, limit);
}

/** "Fri, Nov 6, 12:00 AM EST" for a past row's start, in Eastern. */
export function recentWhen(ev) {
  const ms = parseUtc(ev?.starts_utc);
  return ms == null ? '' : timeText(ms, ET);
}

/** The Seven Nights row stays until the Token Stall closes, then it is gone. */
export function nightsVisible(now, closesAt) {
  const end = Date.parse(closesAt);
  return Number.isNaN(end) || now < end;
}
