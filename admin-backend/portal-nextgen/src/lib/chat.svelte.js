// Portal chat store. One $state proxy for the whole surface: the channel list,
// a per-channel page of messages, the composer's refusal state, and the SSE
// connection that keeps all of it live. Mutate the proxy's PROPERTIES in place
// (never reassign `chat` itself) so the shared reference every component holds
// stays live, the way traffic.svelte.js does it.
//
// DARK BY DEFAULT. `enabled:false` is the honest "not open yet" state and it is
// QUIET: the page seals, the stream closes, nothing is announced. The flag can
// also flip while a tab sits open, so the channel read is polled and a
// `chat_disabled` refusal on a send folds straight back into this state.
//
// Identity is the server's. Nothing here resolves an account: `char_name` is
// the only handle the client ever sees, and the session cookie decides which
// channels come back at all. A channel the server did not list cannot be opened.
//
// Bodies are PLAIN TEXT, always. They are rendered through Svelte interpolation
// with `white-space: pre-wrap`, never innerHTML and never {@html}: the server
// strips links to [link] but the client must not be the thing that trusts it.
import { api, uuidv4 } from './api.js';

// The channel list carries unread counts and the mute state, both of which move
// without any action from this tab, and it is the poll that notices the feature
// flag going dark. Slower than this buys a staler seal; faster buys nothing.
const CHANNELS_POLL_MS = 60_000;

export const PAGE_SIZE = 50;
export const BODY_MAX = 500;

// The five commands, in the order the server lists them when it refuses an
// unknown one. /me and /roll are the SERVER's: they go out as an ordinary send
// and come back as a message with a `kind`. The other three never leave this
// tab, and answer with an ephemeral row nobody else sees.
export const COMMANDS = [
  { name: '/me', usage: '/me <action>', help: 'Say what you are doing.' },
  { name: '/roll', usage: '/roll [NdM]', help: 'Roll dice. 1d20 by default.' },
  { name: '/w', usage: '/w <name> <message>', help: 'Whisper one player through the mailbox.' },
  { name: '/price', usage: '/price <item>', help: 'The cheapest live listings for an item.' },
  { name: '/help', usage: '/help', help: 'List these commands.' },
];

// The subject every chat whisper lands under in the recipient's mailbox, so a
// whisper is recognisable there as something said in a room.
const WHISPER_SUBJECT = 'Whisper from chat';
// How many ladder rungs /price answers with.
const PRICE_RUNGS = 3;

// Ten pages of live history per channel. A tab left open on a busy channel for a
// day would otherwise grow its array without limit, and every row is a rendered
// component. Trimming raises hasMore, so scrolling up reloads what was dropped.
export const MAX_ROWS = 500;

// Reconnect backoff: 1 s doubling to a 30 s ceiling.
export const BACKOFF_BASE_MS = 1000;
export const BACKOFF_MAX_MS = 30000;
// The backoff resets only after a connection has PROVEN stable for this long,
// the way map/stream.js does it. Resetting on the open itself would let a
// connection that opens and immediately dies (a proxy killing SSE, a deploy
// flapping) hammer at the base delay forever instead of backing off.
export const STABLE_RESET_MS = 30000;
// Keepalive comments are invisible to the EventSource API, so a zombie
// connection (socket open, nothing arriving) cannot be told from a quiet
// channel. After this long with no named event we refill the active channel
// from the REST read rather than trust the silence.
export const WATCHDOG_MS = 90000;
// A stream that cannot connect at all for this long stops being a blip, and the
// surface falls back to polling the open channel until it comes back.
export const FALLBACK_AFTER_MS = 60000;
export const FALLBACK_POLL_MS = 10000;
// A hidden tab drops its stream after this long. The server snapshots channel
// membership when the stream opens, so closing it is also how a player removed
// from a guild stops receiving that guild's messages.
export const HIDDEN_CLOSE_MS = 60000;

export function backoffDelay(attempt) {
  return Math.min(BACKOFF_MAX_MS, BACKOFF_BASE_MS * Math.pow(2, attempt));
}

// Refusal tokens are the server's vocabulary; these are the sentences. A token
// with no sentence here still seals the composer, it just says the generic one,
// because inventing a specific reason for an unknown refusal is a lie.
const REFUSAL_TEXT = {
  chat_disabled: 'Chat is being fitted. Back soon.',
  not_member: 'You are not in this channel.',
  muted: 'You are muted in this channel.',
  rate_limited: 'Too many messages just now.',
  duplicate: 'You just said that.',
  bad_request: 'That message could not be sent.',
  unknown_command: 'That is not a command.',
};

export function refusalText(token) {
  // An unknown command is refused WITH the vocabulary the server accepts, so
  // the list the player reads is the server's own and not this file's guess.
  // The local list is the fallback for a refusal that arrived without one.
  if (token === 'unknown_command') {
    const names = chat.commands.length ? chat.commands : COMMANDS.map((c) => c.name);
    return REFUSAL_TEXT.unknown_command + ' Try ' + names.join(', ') + '.';
  }
  return REFUSAL_TEXT[token] || 'That message could not be sent.';
}

export const chat = $state({
  status: 'idle',      // idle | loading | ready | error
  enabled: false,      // the chat flag, as the server reports it
  channels: [],        // [{id, kind, label, can_post, can_moderate, unread}]
  active: '',          // the open channel id, '' before the first open
  messages: {},        // {[channel]: [row, ...]} ascending by id
  local: {},           // {[channel]: [row, ...]} this tab's ephemeral rows
  hasMore: {},         // {[channel]: bool} another page exists behind the top
  loading: {},         // {[channel]: bool} the first page is in flight
  older: {},           // {[channel]: bool} an older page is in flight
  me: null,            // {char_name, muted: {until_utc, channel} | null, admin}
  stream: 'idle',      // idle | open | reconnecting
  sending: false,
  refusal: '',         // the last send refusal token, '' when clear
  commands: [],        // the server's command list, from an unknown_command refusal
  retryAfter: 0,       // seconds left on a rate_limited countdown
});

/** The channel row for an id, or null. The list is short (single digits), so a
 *  scan is cheaper than an index that has to be kept true. */
export function channelById(id) {
  return chat.channels.find((c) => c && c.id === id) || null;
}

/** The rows a channel renders: the server's page, then this tab's ephemeral
 *  rows. The two lists stay separate on purpose. A local row is not a message,
 *  it is this tab answering the player, so it must never reach mergeRow, the
 *  row cap, the read receipt or the server at all. */
export function rowsFor(channel) {
  if (!channel) return [];
  const server = chat.messages[channel] || [];
  const local = chat.local[channel] || [];
  return local.length ? server.concat(local) : server;
}

export function canPost(id) {
  const c = channelById(id);
  return !!(c && c.can_post);
}

export function canModerate(id) {
  const c = channelById(id);
  return !!(c && c.can_moderate);
}

function visible() {
  return typeof document === 'undefined' || document.visibilityState !== 'hidden';
}

/** Drop the standing refusal. The composer calls this on the next keystroke
 *  after a non-blocking notice ("you just said that"), so the sentence belongs
 *  to the send that earned it and not to the message being typed now. */
export function clearRefusal() {
  chat.refusal = '';
  chat.commands = [];
  stopCountdown();
  chat.retryAfter = 0;
}

// --------------------------------------------------------------------------- //
// Rate limit countdown
// --------------------------------------------------------------------------- //

let countdownTimer = null;

function stopCountdown() {
  if (countdownTimer != null) { clearInterval(countdownTimer); countdownTimer = null; }
}

/** Tick `retry_after` down to zero, then lift the refusal it belongs to. The
 *  composer reads the number; the store owns the clock so a remount does not
 *  restart it. */
function startCountdown(seconds) {
  stopCountdown();
  chat.retryAfter = Math.max(0, Math.ceil(Number(seconds) || 0));
  if (chat.retryAfter <= 0) return;
  countdownTimer = setInterval(() => {
    chat.retryAfter = Math.max(0, chat.retryAfter - 1);
    if (chat.retryAfter > 0) return;
    stopCountdown();
    if (chat.refusal === 'rate_limited') chat.refusal = '';
  }, 1000);
}

// --------------------------------------------------------------------------- //
// Row bookkeeping
// --------------------------------------------------------------------------- //

function listFor(channel) {
  if (!chat.messages[channel]) chat.messages[channel] = [];
  return chat.messages[channel];
}

/** Drop the oldest rows past the cap and raise hasMore, so the top of the pane
 *  reloads on demand instead of the array growing forever.
 *
 *  This runs on the LIVE append path only. loadOlder is the reader deliberately
 *  asking for more, and trimming there would delete the page they just asked
 *  for, so a reader who walks a long way back keeps what they walked through
 *  until the channel goes quiet enough for them to leave. */
function capRows(channel) {
  const list = chat.messages[channel];
  if (!list || list.length <= MAX_ROWS) return;
  list.splice(0, list.length - MAX_ROWS);
  chat.hasMore[channel] = true;
}

/** Insert a server row in ascending id order, replacing a row with the same id.
 *  Pending (optimistic) rows have a null id and always trail the settled ones,
 *  so a live row lands in front of them rather than under the player's own
 *  in-flight message. */
function mergeRow(channel, row) {
  if (!row || row.id == null) return;
  const list = listFor(channel);
  const seen = list.findIndex((m) => m.id != null && m.id === row.id);
  if (seen >= 0) { list[seen] = { ...list[seen], ...row }; return; }
  let i = list.length;
  while (i > 0 && (list[i - 1].id == null || list[i - 1].id > row.id)) i -= 1;
  list.splice(i, 0, row);
  capRows(channel);
}

/** Settle a send: the optimistic row is REPLACED by the server row, matched on
 *  client_key. If the stream already delivered that same row (the event can
 *  beat the POST response), the optimistic row is dropped instead of leaving
 *  the message on screen twice. */
function settleSend(channel, clientKey, row) {
  const list = chat.messages[channel];
  if (!list) return;
  const optimistic = list.findIndex((m) => m.client_key === clientKey);
  const already = list.findIndex((m) => m.id != null && row && m.id === row.id);
  if (optimistic >= 0 && already >= 0 && already !== optimistic) {
    list.splice(optimistic, 1);
    return;
  }
  if (optimistic >= 0) {
    list[optimistic] = { ...row, client_key: clientKey, pending: false, failed: false };
    return;
  }
  mergeRow(channel, row);
}

/** Mark the optimistic row failed and hang the refusal token on it, so the row
 *  stays where the player typed it instead of vanishing without a word. */
function failSend(channel, clientKey, token) {
  const list = chat.messages[channel];
  if (!list) return;
  const i = list.findIndex((m) => m.client_key === clientKey);
  if (i < 0) return;
  list[i] = { ...list[i], pending: false, failed: true, error: token };
}

function markDeleted(channel, id) {
  const list = chat.messages[channel];
  if (!list) return;
  const i = list.findIndex((m) => m.id != null && m.id === Number(id));
  if (i < 0) return;
  list[i] = { ...list[i], body: '', deleted: true };
}

function bumpUnread(channel, n) {
  const c = channelById(channel);
  if (!c) return;
  c.unread = Math.max(0, (Number(c.unread) || 0) + n);
}

function lastServerId(channel) {
  const list = chat.messages[channel] || [];
  for (let i = list.length - 1; i >= 0; i -= 1) {
    if (list[i].id != null) return list[i].id;
  }
  return 0;
}

// --------------------------------------------------------------------------- //
// Reads
// --------------------------------------------------------------------------- //

let channelsSeq = 0;

/** Pull the channel list. This is also the dark check: a refusal seals the page
 *  and closes the stream rather than erroring, because "not open yet" is not a
 *  failure. */
export async function loadChannels() {
  const seq = ++channelsSeq;
  if (chat.status === 'idle') chat.status = 'loading';
  try {
    const r = await api.chat.channels();
    if (seq !== channelsSeq) return;
    if (!r || r.ok === false) {
      goDark();
      return;
    }
    chat.enabled = true;
    chat.channels = Array.isArray(r.channels) ? r.channels : [];
    chat.me = r.me || null;
    chat.status = 'ready';
    if (chat.active && !channelById(chat.active)) chat.active = '';
    syncStream();
  } catch (e) {
    if (seq !== channelsSeq) return;
    chat.status = 'error';
    chat.enabled = false;
    closeStream();
  }
}

/** The flag went off (or was never on). Seal, drop the rows, end the stream. */
function goDark() {
  chat.enabled = false;
  chat.channels = [];
  chat.messages = {};
  chat.local = {};
  chat.hasMore = {};
  chat.active = '';
  chat.status = 'ready';
  closeStream();
}

/** Open a channel: load the newest page, then mark it read. Re-opening a channel
 *  that already holds rows still refreshes, because the page may have sat behind
 *  a closed stream. */
export async function openChannel(id) {
  if (!id || !chat.enabled) return;
  // Ephemeral rows belong to the room they were asked in, and to this visit.
  if (chat.active && chat.active !== id) clearLocal(chat.active);
  chat.active = id;
  clearRefusal();
  await loadLatest(id);
  await markRead(id);
}

/** Replace a channel's rows with the newest page. Used on open and after every
 *  reconnect, so anything missed while the stream was down is filled in. */
export async function loadLatest(channel) {
  if (!channel || chat.loading[channel]) return;
  chat.loading[channel] = true;
  try {
    const r = await api.chat.messages(channel, { limit: PAGE_SIZE });
    if (!r || r.ok === false) {
      if (r && r.error === 'chat_disabled') goDark();
      return;
    }
    const rows = Array.isArray(r.messages) ? r.messages : [];
    // Pending rows survive a refresh: a message still in flight is the player's,
    // and dropping it under them would look like the send was lost.
    const pending = (chat.messages[channel] || []).filter((m) => m.pending);
    chat.messages[channel] = rows.concat(pending);
    chat.hasMore[channel] = r.has_more === true;
  } catch (e) {
    // A failed read leaves the last good rows alone rather than blanking them.
  } finally {
    chat.loading[channel] = false;
  }
}

/** Prepend the page behind the top of the list. */
export async function loadOlder(channel) {
  if (!channel || chat.older[channel] || !chat.hasMore[channel]) return;
  const list = chat.messages[channel] || [];
  const first = list.find((m) => m.id != null);
  if (!first) return;
  chat.older[channel] = true;
  try {
    const r = await api.chat.messages(channel, { before: first.id, limit: PAGE_SIZE });
    if (!r || r.ok === false) return;
    const rows = Array.isArray(r.messages) ? r.messages : [];
    const known = new Set(list.map((m) => m.id).filter((v) => v != null));
    chat.messages[channel] = rows.filter((m) => !known.has(m.id)).concat(list);
    chat.hasMore[channel] = r.has_more === true;
  } catch (e) {
    // Same as loadLatest: keep what is on screen.
  } finally {
    chat.older[channel] = false;
  }
}

/** Tell the server how far this account has read, and zero the local pip. */
export async function markRead(channel) {
  if (!channel || !chat.enabled) return;
  const lastId = lastServerId(channel);
  if (!lastId) return;
  const c = channelById(channel);
  if (c) c.unread = 0;
  try {
    await api.chat.read(channel, lastId);
  } catch (e) {
    // The pip is a convenience and a failed receipt is not worth a sentence.
    // A DARK flag noticed here is worth acting on though: this read runs on
    // every open and every live row, so it is usually the first call to learn
    // the flag went off, and the surface should not wait for the next poll.
    if (envelopeOf(e)?.error === 'chat_disabled') goDark();
  }
}

// --------------------------------------------------------------------------- //
// Refusals
// --------------------------------------------------------------------------- //

/** The refusal envelope behind a rejected mutation.
 *
 *  api.js's sendCsrfJSON is FAIL-CLOSED: it throws on a non-2xx AND on a 200
 *  carrying {ok:false}, hanging the whole envelope on `err.data`. Every chat
 *  refusal is a 200 with a token (plan 1c), so the tokens never arrive as a
 *  return value on the POST paths (send, read, and lane D's writes): they arrive
 *  as an exception, and reading them off the error is the ONLY way the composer
 *  ever sees a countdown, a mute reason, or the dark flag.
 *
 *  The GET paths are the other way round: getJSON returns a 200 body as-is, so
 *  `{ok:false, error:'chat_disabled'}` from the channel and message reads is a
 *  normal return and is handled at those call sites. */
function envelopeOf(e) {
  return (e && e.data) || null;
}

/** Fold a refusal envelope into the surface state and hand back its token. One
 *  place, because a refusal that only the composer understood would leave the
 *  mute state and the dark flag behind whenever it arrived somewhere else. */
function applyRefusal(channel, envelope) {
  const token = (envelope && envelope.error) || 'bad_request';
  chat.refusal = token;
  chat.commands = token === 'unknown_command' && Array.isArray(envelope && envelope.commands)
    ? envelope.commands.filter((c) => typeof c === 'string')
    : [];
  if (token === 'rate_limited') startCountdown(envelope && envelope.retry_after);
  if (token === 'muted' && chat.me) {
    chat.me.muted = { until_utc: (envelope && envelope.until_utc) ?? null, channel };
  }
  if (token === 'chat_disabled') goDark();
  return token;
}

// --------------------------------------------------------------------------- //
// Commands
// --------------------------------------------------------------------------- //

// The three this tab answers itself. /me and /roll are deliberately absent: they
// are messages, and the server owns what an emote and a die roll become.
const LOCAL_RE = /^\/(w|price|help)(?:\s+([\s\S]*))?$/i;

export function isLocalCommand(text) {
  return LOCAL_RE.test(String(text || '').trim());
}

/** An EPHEMERAL row: this tab talking to the player, not a message anyone said.
 *  id is null so nothing downstream can mistake it for a server row (the pane
 *  keys these on their client_key), kind is 'system' so it renders muted with no
 *  actions, and it lives in `local` so the cap, the read receipt and the stream
 *  never see it. It is gone on the next channel switch. */
function pushLocal(channel, body) {
  if (!chat.local[channel]) chat.local[channel] = [];
  chat.local[channel].push({
    id: null,
    client_key: uuidv4(),
    char_name: '',
    body: String(body || ''),
    created_utc: new Date().toISOString(),
    kind: 'system',
    mine: false,
    deleted: false,
  });
  return true;
}

export function clearLocal(channel) {
  if (channel && chat.local[channel]) chat.local[channel] = [];
}

/** Answer a malformed command with its own usage line and hand the typed words
 *  back to the box: the player is one edit away from meaning it. */
function usage(channel, name) {
  const c = COMMANDS.find((x) => x.name === name);
  pushLocal(channel, c ? c.usage + '  ' + c.help : name);
  return false;
}

async function runLocalCommand(channel, text) {
  const m = LOCAL_RE.exec(text.trim());
  const name = m[1].toLowerCase();
  const rest = (m[2] || '').trim();
  if (name === 'help') return pushLocal(channel, COMMANDS.map((c) => c.usage + '  ' + c.help).join('\n'));
  if (name === 'w') return whisperCommand(channel, rest);
  return priceCommand(channel, rest);
}

/** The mailbox answers a refusal with a player-facing SENTENCE rather than a
 *  token: a blocked pair, an unknown name and a rate limit each arrive as their
 *  own line of copy on {ok:false, error}. Saying it back verbatim is more honest
 *  than mapping it to a sentence of ours that could drift from what the mailbox
 *  actually did. The status fallbacks cover a refusal with no body at all. */
function whisperRefusal(e) {
  const said = String(envelopeOf(e)?.error || '').trim();
  if (said) return said;
  if (e?.status === 404) return 'No player by that name is linked to the portal.';
  if (e?.status === 403) return 'You cannot whisper that player.';
  if (e?.status === 429) return 'You are whispering too quickly. Try again shortly.';
  return 'The whisper did not send.';
}

/** /w <name> <message>. The name may be quoted, because a character name with a
 *  space in it is otherwise indistinguishable from the first word of the
 *  message. This is the MAILBOX, not the room: nothing is posted to the channel,
 *  and the recipient's block list and the mailbox rate limits apply unchanged. */
async function whisperCommand(channel, rest) {
  const m = /^(?:"([^"]+)"|(\S+))\s+([\s\S]+)$/.exec(rest);
  if (!m) return usage(channel, '/w');
  const name = (m[1] || m[2]).trim();
  const body = m[3].trim();
  if (!name || !body) return usage(channel, '/w');
  try {
    await api.messages.send({
      recipient_char_name: name,
      subject: WHISPER_SUBJECT,
      body,
    });
    return pushLocal(channel, 'Whispered to ' + name);
  } catch (e) {
    pushLocal(channel, 'Not whispered to ' + name + '. ' + whisperRefusal(e));
    return false;
  }
}

/** The row the player meant: an exact name match first, then the first row that
 *  actually has a listing behind it. */
function bestMatch(rows, term) {
  const want = term.toLowerCase();
  const live = rows.filter((r) => r && r.template_id);
  return live.find((r) => String(r.name || '').toLowerCase() === want)
    || live.find((r) => (Number(r.listing_count) || 0) > 0)
    || live[0]
    || null;
}

function marketDown(channel) {
  pushLocal(channel, 'The market could not be reached just now.');
  return false;
}

/** /price <item>. Two reads, because the browse search answers with one row per
 *  ITEM (a name, a cheapest price, a count) while the three rungs the player
 *  asked for are that item's own ladder. The search is what turns typed words
 *  into a template id; the ladder is the thing with prices on it. */
async function priceCommand(channel, term) {
  if (!term) return usage(channel, '/price');
  try {
    const r = await api.market.search({ q: term });
    // A refused or unavailable read is not an empty market. Saying "no
    // listings" there would be this tab inventing a fact about live data.
    if (!r || r.ok === false || r.browse_unavailable) return marketDown(channel);
    const row = bestMatch(Array.isArray(r && r.rows) ? r.rows : [], term);
    if (!row) return pushLocal(channel, 'No listings for ' + term);
    const detail = await api.market.item(row.template_id);
    if (!detail || detail.ok === false) return marketDown(channel);
    const rungs = (Array.isArray(detail.ladder) ? detail.ladder : [])
      .filter((x) => x && typeof x.price === 'number')
      .sort((a, b) => a.price - b.price)
      .slice(0, PRICE_RUNGS);
    if (rungs.length === 0) return pushLocal(channel, 'No listings for ' + term);
    const name = detail.name || row.name || term;
    return pushLocal(channel, name + ': ' + rungs
      .map((x) => x.price.toLocaleString() + ' x' + (Number(x.qty) || 1))
      .join(', '));
  } catch (e) {
    return marketDown(channel);
  }
}

// --------------------------------------------------------------------------- //
// Send
// --------------------------------------------------------------------------- //

/** Optimistic send. The client mints a uuid client_key (api.js uuidv4, which is
 *  crypto.randomUUID where the platform has it) and the server's UNIQUE index on
 *  it makes a replay return the SAME row instead of posting twice, so a retry
 *  after a dropped response is safe. The optimistic row is replaced by the
 *  server row on the client_key match, or marked failed carrying the refusal
 *  token. */
export async function send(text) {
  const channel = chat.active;
  const body = String(text || '').trim();
  if (!channel || !body || chat.sending) return false;
  // /w, /price and /help are answered by this tab and are never posted to the
  // room. /me and /roll are the server's, and go out as an ordinary send.
  if (isLocalCommand(body)) {
    clearRefusal();
    chat.sending = true;
    try {
      return await runLocalCommand(channel, body);
    } finally {
      chat.sending = false;
    }
  }
  const clientKey = uuidv4();
  const list = listFor(channel);
  list.push({
    id: null,
    client_key: clientKey,
    char_name: chat.me?.char_name || '',
    body,
    created_utc: new Date().toISOString(),
    mine: true,
    deleted: false,
    pending: true,
    failed: false,
    error: '',
  });
  chat.sending = true;
  clearRefusal();
  try {
    const r = await api.chat.send(channel, body, clientKey);
    // A refusal does not come back here: sendCsrfJSON THROWS on a 200 with
    // {ok:false}. This branch is the success, plus a defensive read of an
    // envelope with no message, which would otherwise settle as a phantom row.
    if (!r || !r.message) {
      failSend(channel, clientKey, applyRefusal(channel, r));
      return false;
    }
    settleSend(channel, clientKey, r.message);
    if (chat.me && r.message.char_name) chat.me.char_name = r.message.char_name;
    await markRead(channel);
    return true;
  } catch (e) {
    failSend(channel, clientKey, applyRefusal(channel, envelopeOf(e)));
    return false;
  } finally {
    chat.sending = false;
  }
}

/** Drop a failed optimistic row (the composer's dismiss). */
export function discardFailed(channel, clientKey) {
  const list = chat.messages[channel];
  if (!list) return;
  const i = list.findIndex((m) => m.client_key === clientKey);
  if (i >= 0) list.splice(i, 1);
}

// --------------------------------------------------------------------------- //
// Stream
// --------------------------------------------------------------------------- //

let es = null;
let esChannels = '';
let attempt = 0;
let reconnectTimer = null;
let stableTimer = null;
// Whether the CURRENT connection has held long enough to count as healthy.
let stable = false;
let watchdogTimer = null;
let fallbackTimer = null;
let torn = false;
// True once a connection has dropped. The NEXT open reloads the active
// channel's newest page, because events that fired while we were down were
// delivered to nobody.
let reopening = false;
// When the stream stopped being up, 0 while it is up. The poll fallback reads
// this rather than a flag, because "down" only earns a fallback after a while.
let downSince = 0;

export function streamUrl(ids) {
  return '/portal/chat/stream?channels=' + encodeURIComponent(ids.join(','));
}

function clearReconnect() {
  if (reconnectTimer != null) { clearTimeout(reconnectTimer); reconnectTimer = null; }
}

function clearStreamTimers() {
  clearReconnect();
  if (stableTimer != null) { clearTimeout(stableTimer); stableTimer = null; }
  if (watchdogTimer != null) { clearTimeout(watchdogTimer); watchdogTimer = null; }
}

/** Refill the open channel when the stream has gone quiet for too long. A
 *  keepalive comment never reaches the EventSource API, so silence is
 *  ambiguous: this reads the truth over REST rather than guessing which kind
 *  of silence it was. */
function armWatchdog() {
  if (watchdogTimer != null) clearTimeout(watchdogTimer);
  watchdogTimer = setTimeout(() => {
    watchdogTimer = null;
    if (torn) return;
    if (chat.active) loadLatest(chat.active);
    armWatchdog();
  }, WATCHDOG_MS);
}

/** Every named event is proof the connection is alive. */
function sawFrame() {
  armWatchdog();
}

function stopFallback() {
  if (fallbackTimer != null) { clearInterval(fallbackTimer); fallbackTimer = null; }
}

/** While the stream has been down longer than FALLBACK_AFTER_MS, poll the open
 *  channel so the surface keeps moving without it. */
function startFallback() {
  if (fallbackTimer != null) return;
  fallbackTimer = setInterval(() => {
    if (torn || !downSince) return;
    if (Date.now() - downSince < FALLBACK_AFTER_MS) return;
    if (chat.active && visible()) loadLatest(chat.active);
  }, FALLBACK_POLL_MS);
}

function markStreamDown() {
  chat.stream = 'reconnecting';
  reopening = true;
  if (!downSince) downSince = Date.now();
  startFallback();
}

function onMessage(e) {
  sawFrame();
  let payload = null;
  try { payload = JSON.parse(e.data); } catch (err) { return; }
  if (!payload || !payload.channel) return;
  const channel = payload.channel;
  mergeRow(channel, payload);
  if (channel === chat.active && visible()) {
    markRead(channel);
  } else if (!payload.mine) {
    bumpUnread(channel, 1);
  }
}

function onDeleted(e) {
  sawFrame();
  let payload = null;
  try { payload = JSON.parse(e.data); } catch (err) { return; }
  if (!payload || !payload.channel) return;
  markDeleted(payload.channel, payload.id);
}

/** Lane A's 25 s heartbeat. It carries no payload and moves no state: its whole
 *  job is to prove the socket is alive, because the `: keepalive` comment beside
 *  it never reaches the EventSource API. Without this the watchdog could not
 *  tell a dead connection from a quiet channel and would refill every 90 s on a
 *  perfectly healthy stream. */
function onHeartbeat() {
  sawFrame();
}

function onMute(e) {
  sawFrame();
  let payload = null;
  try { payload = JSON.parse(e.data); } catch (err) { return; }
  if (!payload || !chat.me) return;
  chat.me.muted = payload.muted || null;
  if (!payload.muted && chat.refusal === 'muted') chat.refusal = '';
}

function connect(ids) {
  if (torn || typeof EventSource === 'undefined' || ids.length === 0) return;
  esChannels = ids.join(',');
  stable = false;
  es = new EventSource(streamUrl(ids));
  const CLOSED = EventSource.CLOSED != null ? EventSource.CLOSED : 2;
  es.onopen = () => {
    chat.stream = 'open';
    downSince = 0;
    stopFallback();
    // The backoff resets only once this connection has held for
    // STABLE_RESET_MS. An open that dies seconds later must keep climbing.
    if (stableTimer != null) clearTimeout(stableTimer);
    stableTimer = setTimeout(() => {
      stableTimer = null;
      stable = true;
      attempt = 0;
    }, STABLE_RESET_MS);
    armWatchdog();
    // Refill after a gap: the stream delivered nothing while it was down, so the
    // newest page is the only way to know what was said.
    if (reopening) {
      reopening = false;
      if (chat.active) loadLatest(chat.active);
      loadChannels();
    }
  };
  es.addEventListener('heartbeat', onHeartbeat);
  es.addEventListener('message', onMessage);
  es.addEventListener('deleted', onDeleted);
  es.addEventListener('mute', onMute);
  es.onerror = () => {
    if (torn) return;
    // The server closes every stream at 10 minutes so it can re-resolve channel
    // membership, so most drops here are a scheduled handover rather than a
    // fault. A connection that had proven stable reconnects from the BASE delay:
    // only genuinely flapping connections climb the backoff.
    if (stable) attempt = 0;
    markStreamDown();
    if (!es || es.readyState === CLOSED) {
      // Fatal (a non-200, a refused connection, the 204 the server answers while
      // dark): the browser will not retry on its own, so we back off and do it.
      scheduleReconnect(ids);
    }
    // Otherwise the browser's own retry is already in flight and carries
    // Last-Event-ID for us; the next onopen flips the state back.
  };
}

function scheduleReconnect(ids) {
  if (es) { es.close(); es = null; }
  clearStreamTimers();
  const delay = backoffDelay(attempt);
  attempt += 1;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    if (torn) return;
    connect(ids);
  }, delay);
}

/** Open the stream over every channel the server listed, or reopen it when that
 *  set has changed. Cheap to call: a matching set is a no-op. */
export function syncStream() {
  if (!chat.enabled) { closeStream(); return; }
  const ids = chat.channels.map((c) => c.id).filter(Boolean);
  if (ids.length === 0) { closeStream(); return; }
  if (es && esChannels === ids.join(',')) return;
  closeStream();
  torn = false;
  attempt = 0;
  connect(ids);
}

export function closeStream() {
  torn = true;
  clearStreamTimers();
  stopFallback();
  if (es) { es.close(); es = null; }
  esChannels = '';
  downSince = 0;
  chat.stream = 'idle';
}

// --------------------------------------------------------------------------- //
// Subscription
// --------------------------------------------------------------------------- //

let subscribers = 0;
let pollTimer = null;
let listening = false;
let hiddenTimer = null;

function clearHiddenTimer() {
  if (hiddenTimer != null) { clearTimeout(hiddenTimer); hiddenTimer = null; }
}

function stopPoll() {
  if (pollTimer != null) { clearInterval(pollTimer); pollTimer = null; }
}

function startPoll() {
  if (pollTimer != null || subscribers === 0) return;
  pollTimer = setInterval(() => { if (visible()) loadChannels(); }, CHANNELS_POLL_MS);
}

function onVisibility() {
  if (subscribers === 0) return;
  if (visible()) {
    clearHiddenTimer();
    loadChannels();
    startPoll();
    // A stream dropped while the tab was hidden comes back with a refill, so
    // the gap is filled from the REST read rather than silently skipped.
    if (chat.enabled && !es) { reopening = true; syncStream(); }
    // A channel read while the tab was hidden is read now that it is not.
    if (chat.active) markRead(chat.active);
  } else {
    stopPoll();
    clearHiddenTimer();
    hiddenTimer = setTimeout(() => {
      hiddenTimer = null;
      if (visible() || subscribers === 0) return;
      // The server snapshots channel membership when the stream OPENS, so a
      // long-lived connection outlives the membership it was granted under.
      // Closing a hidden tab's stream is how a player removed from a guild
      // stops receiving that guild's messages, and it costs a backgrounded tab
      // nothing it was using.
      reopening = true;
      closeStream();
    }, HIDDEN_CLOSE_MS);
  }
}

/** Subscribe the chat surface: loads the channel list, keeps it polled while the
 *  tab is visible, and holds the stream open. Returns the teardown, so a caller
 *  can hand it straight back from $effect. */
export function watchChat() {
  subscribers += 1;
  if (!listening && typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', onVisibility);
    listening = true;
  }
  loadChannels();
  startPoll();
  return () => {
    subscribers = Math.max(0, subscribers - 1);
    if (subscribers > 0) return;
    stopPoll();
    stopCountdown();
    clearHiddenTimer();
    closeStream();
    if (listening && typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', onVisibility);
      listening = false;
    }
  };
}
