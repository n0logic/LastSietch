// Chat link rendering (wave 11.1). The server keeps an allowlisted URL in the
// stored body VERBATIM and rewrites every other one to [link: host], so the
// bodies arriving here already carry the server's decision. This file makes the
// SAME decision again, independently, before anything becomes an anchor.
//
// That double check is the point. A stored row is whatever the filter of the
// day allowed when it was written: old rows predate this wave, a filter bug
// ships a URL that should not have survived, and neither of those may become a
// clickable link in a player's browser just because the server said so once.
// The allowlist below is the client's own copy and is pinned equal to
// portal_chat_filters.ALLOWED_HOSTS by test_chat_page.py.
//
// tokenize() returns TOKENS, never markup. The row renders them with Svelte
// interpolation and a real <a> element: there is no {@html} anywhere on the
// chat surface, and a body that is not allowlisted stays plain text.

// The owner's list (plan 7a), exactly. Any change here changes the server list
// in the same commit or the parity pin fails.
export const ALLOWED_HOSTS = [
  'lastsietch.com',
  'discord.com',
  'discord.gg',
  'youtube.com',
  'www.youtube.com',
  'youtu.be',
  'duneawakening.wiki.gg',
  'dune-awakening.fandom.com',
];

// Our own domain is allowed with any subdomain (portal., map., …). Nobody
// else's is: a subdomain of a third party is a host somebody else controls.
export const SUBDOMAIN_HOST = 'lastsietch.com';

// The visible text of a link, in characters. Long enough to show which page it
// points at, short enough that one URL cannot own the pane.
export const LABEL_MAX = 60;

// portal_chat_filters.MAX_LINK_CHARS. A URL longer than this is one the server
// would have replaced with [link: host], so a longer one reaching this file
// came from an older row and is not linked here either.
export const MAX_LINK_CHARS = 200;

// Same shape as the server's _URL_RE: a scheme, or a bare www. host. \S+ is
// deliberately greedy and the trailing punctuation is given back below.
const URL_RE = /(?:https?:\/\/|www\.)\S+/gi;
// Sentence punctuation that followed the URL rather than belonging to it.
const TRAILING_RE = /[.,;:!?'")\]}]+$/;
const IPV4_RE = /^\d{1,3}(?:\.\d{1,3}){3}$/;

/** Is this host one a chat message may link to?
 *
 *  Punycode is refused outright: `xn--` is how a lookalike domain is spelled
 *  once the URL parser has normalised it, and an allowlist that cannot be read
 *  by eye is not an allowlist. IP literals are refused for the same reason,
 *  there being no host name to check at all. */
export function isAllowedHost(host) {
  const h = String(host || '').toLowerCase();
  if (!h) return false;
  if (h.includes('xn--')) return false;
  // '[' opens an IPv6 literal; the v4 form has no letters to check either.
  if (h.startsWith('[') || IPV4_RE.test(h)) return false;
  if (ALLOWED_HOSTS.includes(h)) return true;
  return h.endsWith('.' + SUBDOMAIN_HOST);
}

/** Is there userinfo in the AUTHORITY of this url?
 *
 *  Asked of the parsed url and never of the string, because '@' is an ordinary
 *  path character: 'https://www.youtube.com/@LastSietch' is a channel handle
 *  and must stay a link, while 'https://your-bank.example@discord.gg/x' is
 *  userinfo and must not. A url that does not parse answers TRUE, so an
 *  unreadable authority fails closed rather than being assumed innocent. */
export function hasUserinfo(href) {
  try {
    const u = new URL(href);
    return u.username !== '' || u.password !== '';
  } catch (e) {
    return true;
  }
}

/** Does this exact string survive as a link?
 *
 *  The server's `_keeps_url`, mirrored. Userinfo disqualifies a URL even on an
 *  allowlisted host: the host is where the click GOES and the allowlist has
 *  already approved it, but the string is what the reader SEES, and
 *  'https://your-bank.example@discord.gg/x' reads as a bank link to everybody
 *  who is not parsing URLs for a living. */
export function keepsUrl(url, host) {
  if (hasUserinfo(url)) return false;
  if (String(url || '').length > MAX_LINK_CHARS) return false;
  return isAllowedHost(host);
}

/** The parsed host of a matched URL, lowercased, or '' when it does not parse.
 *
 *  URL.hostname is the host ALONE: userinfo and port are already split off, so
 *  "https://youtube.com@evil.example/" resolves to evil.example the way a
 *  browser would, not to the youtube.com a string test would have believed. */
export function hostOf(href) {
  try {
    return new URL(href).hostname.toLowerCase();
  } catch (e) {
    return '';
  }
}

function label(href) {
  return href.length > LABEL_MAX ? href.slice(0, LABEL_MAX - 1) + '…' : href;
}

/** Split a body into [{t:'text', s}, {t:'url', href, label}].
 *
 *  Anything that is not an allowlisted URL comes back as TEXT, including the
 *  URL itself: a link this client will not open is a string, not a dead anchor
 *  the reader has to discover by clicking. */
export function tokenize(body) {
  const src = typeof body === 'string' ? body : '';
  const out = [];

  const pushText = (s) => {
    if (!s) return;
    const last = out[out.length - 1];
    if (last && last.t === 'text') last.s += s;
    else out.push({ t: 'text', s });
  };

  let at = 0;
  let m;
  URL_RE.lastIndex = 0;
  while ((m = URL_RE.exec(src)) !== null) {
    // A URL at the end of a sentence swallowed the punctuation; hand it back so
    // the anchor is the address and the full stop stays in the sentence.
    const raw = m[0].replace(TRAILING_RE, '');
    const end = m.index + raw.length;
    if (!raw) {
      URL_RE.lastIndex = m.index + m[0].length;
      continue;
    }
    const href = /^www\./i.test(raw) ? 'https://' + raw : raw;
    const host = hostOf(href);
    pushText(src.slice(at, m.index));
    if (host && keepsUrl(href, host)) out.push({ t: 'url', href, label: label(href) });
    else pushText(raw);
    at = end;
    URL_RE.lastIndex = end;
  }
  pushText(src.slice(at));
  return out;
}
