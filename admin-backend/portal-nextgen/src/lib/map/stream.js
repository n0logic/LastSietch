// M-LIVE SSE client. Wraps EventSource over GET /portal/maps/{key}/stream and
// feeds every named event through the same onFix(kind, payload) callback the
// pollers use (engine.applyFix), so transport is invisible to the renderer and
// to reckon.js.
//
// Contract (frozen with the M-LIVE backend, task #14 / spec 3b; server side
// documents the same terms on portal_map_stream in routers/portal.py):
// - Named events: spice | worms | sandstorm | players. data = JSON, byte-shape
//   identical to the corresponding poller payload (/live fields, /players
//   body). Non-spice maps stream players only.
// - id: one monotonic integer counter per stream, global across event types.
// - On connect the server emits the current snapshot of every feed, then only
//   on payload change (serialization compare), plus a 25s keepalive comment.
// - Resume: manual reconnects pass ?last_event_id=N; the server currently
//   IGNORES it and replays the full snapshot, which is safe because applyFix
//   ingestion is snap-idempotent. Kept so the server can start honoring it
//   without a client change.
// - Fatal mid-stream failure: the server sends one 'event: error' frame then
//   closes. We never parse it (not in STREAM_EVENTS); the close itself lands
//   in onerror and drives the down/reconnect path.
// - Over the per-IP/total connection caps the server 429s: EventSource treats
//   any non-200 as fatal (readyState CLOSED) and we back off and retry.
//
// Failure model: onStatus('down') means callers must run the fallback pollers;
// onStatus('up') means the stream is delivering and pollers can pause. Callers
// start in the down state. liveStream returns null when EventSource is
// unsupported (caller keeps polling, exactly today's behavior).

export const STREAM_EVENTS = ['spice', 'worms', 'sandstorm', 'players'];
export const BACKOFF_BASE_MS = 1000;
export const BACKOFF_MAX_MS = 30000;
// Backoff resets only after the connection has been up this long. The server
// always delivers a snapshot burst on connect, so resetting on ANY event would
// let a connection that snapshots then dies (proxy killing SSE, mid-deploy
// flap) hammer at the base delay forever instead of backing off.
export const STABLE_RESET_MS = 30000;
// Keepalive comments are invisible to the EventSource API, so a zombie
// connection (TCP open, no events) is indistinguishable from a quiet one.
// Feeds behind the stream refresh at 10s (worms) to 90s (spice); players sits
// on the 60s dune status cache. Events only fire on change though, so a calm
// server can be legitimately silent: 3 minutes buys wide margin before we
// treat the connection as dead and force a reconnect (snapshot-on-reconnect
// makes a false positive cost one cheap replay).
export const WATCHDOG_MS = 180000;

export function backoffDelay(attempt) {
  return Math.min(BACKOFF_MAX_MS, BACKOFF_BASE_MS * Math.pow(2, attempt));
}

export function streamUrl(key, lastEventId) {
  return '/portal/maps/' + encodeURIComponent(key) + '/stream' +
    (lastEventId ? '?last_event_id=' + encodeURIComponent(lastEventId) : '');
}

// opts (all optional): { onStatus(state: 'up'|'down'),
//   EventSourceClass, setTimeoutFn, clearTimeoutFn, nowFn }  -- injection
//   seams for the node smoke; defaults are the browser globals.
// Returns { close() } or null when SSE is unsupported.
export function liveStream(key, onFix, opts) {
  opts = opts || {};
  const ES = opts.EventSourceClass ||
    (typeof EventSource !== 'undefined' ? EventSource : null);
  if (!ES) return null;
  const setT = opts.setTimeoutFn || function (fn, ms) { return setTimeout(fn, ms); };
  const clearT = opts.clearTimeoutFn || function (t) { clearTimeout(t); };
  const nowFn = opts.nowFn || function () { return Date.now(); };
  const onStatus = opts.onStatus || function () {};
  const CLOSED = ES.CLOSED != null ? ES.CLOSED : 2;

  let es = null;
  let closed = false;
  let up = false;
  let attempt = 0;
  let lastEventId = '';
  let connectedAt = 0;
  let reconnectTimer = null;
  let watchdogTimer = null;

  function markDown() {
    if (up) { up = false; onStatus('down'); }
  }

  function clearTimers() {
    if (reconnectTimer != null) { clearT(reconnectTimer); reconnectTimer = null; }
    if (watchdogTimer != null) { clearT(watchdogTimer); watchdogTimer = null; }
  }

  function armWatchdog() {
    if (watchdogTimer != null) clearT(watchdogTimer);
    watchdogTimer = setT(function () {
      watchdogTimer = null;
      reconnect();
    }, WATCHDOG_MS);
  }

  function reconnect() {
    if (closed) return;
    if (es) { es.close(); es = null; }
    clearTimers();
    markDown();
    const delay = backoffDelay(attempt);
    attempt += 1;
    reconnectTimer = setT(function () {
      reconnectTimer = null;
      connect();
    }, delay);
  }

  function onEvent(kind, e) {
    if (closed) return;
    if (e.lastEventId) lastEventId = e.lastEventId;
    // Reset backoff only once the connection has PROVEN stable; the connect
    // snapshot alone must not (see STABLE_RESET_MS).
    if (nowFn() - connectedAt >= STABLE_RESET_MS) attempt = 0;
    armWatchdog();
    if (!up) { up = true; onStatus('up'); }
    let payload = null;
    try { payload = JSON.parse(e.data); } catch (err) { return; }
    if (payload != null) onFix(kind, payload);
  }

  function connect() {
    if (closed) return;
    connectedAt = nowFn();
    es = new ES(streamUrl(key, lastEventId));
    STREAM_EVENTS.forEach(function (kind) {
      es.addEventListener(kind, function (e) { onEvent(kind, e); });
    });
    es.onerror = function () {
      if (closed) return;
      if (!es || es.readyState === CLOSED) {
        // Fatal (non-200, network refusal): the browser will not retry.
        reconnect();
      } else {
        // Native auto-reconnect in flight (sends Last-Event-ID itself).
        // Report down so pollers resume; the next delivered event flips up.
        markDown();
      }
    };
    armWatchdog();
  }

  connect();

  return {
    close: function () {
      closed = true;
      clearTimers();
      if (es) { es.close(); es = null; }
      markDown();
    },
  };
}
