// Chat row timestamps. Plain .js with no imports so the tests can run it under node.
//
// Today's rows keep the short "21:14"; anything older carries its date ("Oct 27 21:14"),
// and a year only when it is not this year. Both are the reader's local time.

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

const pad = (n) => String(n).padStart(2, '0');

// The wire stamp is UTC and may arrive with or without a zone marker; a bare
// "2026-09-04 11:02:03" parsed as local time would print the wrong hour.
export function parseUtc(s) {
  if (typeof s !== 'string' || !s.trim()) return null;
  let v = s.trim().replace(' ', 'T');
  if (!/(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(v)) v += 'Z';
  const t = Date.parse(v);
  return Number.isNaN(t) ? null : t;
}

export function chatStamp(created, now = Date.now()) {
  const t = parseUtc(created);
  if (t == null) return { text: '', title: '' };
  const d = new Date(t);
  const n = new Date(now);
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const day = `${MONTHS[d.getMonth()]} ${d.getDate()}`;
  const full = `${day}, ${d.getFullYear()} ${time}`;
  const sameDay = d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate();
  if (sameDay) return { text: time, title: full };
  if (d.getFullYear() === n.getFullYear()) return { text: `${day} ${time}`, title: full };
  return { text: `${day}, ${d.getFullYear()} ${time}`, title: full };
}
