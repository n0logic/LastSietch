// Node smoke for the map engine's DOM-free logic (npm run smoke). Covers the
// M1 math/derivation modules plus the M2 additions: worm audio transitions,
// worm proximity, and the SSE client (via injected EventSource/timer seams).
// DOM rendering (drawMe/drawWaypoints/engine) is browser-QA territory.

import {
  createView, setZoom, panToNormalized, screenToNormalized,
} from '../src/lib/map/transform.js';
import { sectorCenter } from '../src/lib/map/grid.js';
import { headingToCompass, compass, wormLabel } from '../src/lib/map/overlays.js';
import { createReckoner } from '../src/lib/map/reckon.js';
import {
  deriveConsole, createPollers, ME_STALE_MS,
} from '../src/lib/map/pollers.js';
import { wormProximity } from '../src/lib/map/me.js';
import { createWormAudio } from '../src/lib/map/audio.js';
import {
  liveStream, streamUrl, backoffDelay, BACKOFF_MAX_MS, STABLE_RESET_MS,
} from '../src/lib/map/stream.js';

let failures = 0;
function check(name, cond) {
  if (cond) {
    console.log('  ok  ' + name);
  } else {
    failures += 1;
    console.error('FAIL  ' + name);
  }
}
function near(a, b, eps) { return Math.abs(a - b) <= (eps || 1e-6); }

// ---- transform --------------------------------------------------------------
{
  const v = createView(1000);
  v.display = 500;
  setZoom(v, 99);
  check('transform: zoom clamps to 8', v.zoom === 8);
  setZoom(v, 0.1);
  check('transform: zoom clamps to 1', v.zoom === 1);
  setZoom(v, 2);
  panToNormalized(v, 600, 400);
  const n = screenToNormalized(v, v.display / 2, v.display / 2);
  check('transform: panTo/screenToNormalized round-trip',
        near(n.nx, 600, 1e-9) && near(n.ny, 400, 1e-9));
}

// ---- grid -------------------------------------------------------------------
{
  const grid = { rows: 9, cols: 9, row_letters: 'ABCDEFGHI' };
  const a1 = sectorCenter(grid, 1000, 'A1');
  const i9 = sectorCenter(grid, 1000, 'I9');
  check('grid: A1 is bottom-left sector center',
        a1 && near(a1.x, 1000 / 18) && near(a1.y, 1000 - 1000 / 18));
  check('grid: I9 is top-right sector center',
        i9 && near(i9.x, 1000 - 1000 / 18) && near(i9.y, 1000 / 18));
  check('grid: bad sector -> null', sectorCenter(grid, 1000, 'Z0') === null);
}

// ---- overlays: compass math ---------------------------------------------------
{
  check('overlays: headingToCompass cardinal points',
        headingToCompass(0) === 'E' && headingToCompass(90) === 'S' &&
        headingToCompass(180) === 'W' && headingToCompass(270) === 'N');
  check('overlays: headingToCompass non-finite -> empty',
        headingToCompass(null) === '' && headingToCompass(NaN) === '');
  check('overlays: compass N/E', compass(0, -1) === 'N' && compass(1, 0) === 'E');
  const label = wormLabel({ sector: 'C4', threat: 'enraged', age_s: 300 });
  check('overlays: wormLabel content',
        label.indexOf('Sandworm') === 0 && label.indexOf('C4') > 0 &&
        label.indexOf('enraged') > 0);
  check('overlays: wormLabel has no em dashes', label.indexOf('—') === -1);
}

// ---- reckon -------------------------------------------------------------------
{
  // Reduced motion: positions jump, no rAF needed.
  const r = createReckoner({ reducedMotion: true });
  const applied = [];
  const apply = (x, y, est) => applied.push([x, y, est]);
  r.lerpFix('a', 10, 20, { intervalMs: 10000, snap: true, apply });
  check('reckon: snap applies immediately, not an estimate',
        applied.length === 1 && applied[0][0] === 10 && applied[0][2] === false);
  r.lerpFix('a', 30, 40, { intervalMs: 10000, apply });
  check('reckon: reduce mode jumps to new fix',
        applied.length === 2 && applied[1][0] === 30 && applied[1][2] === false);
  r.destroy();
}
{
  // Animated: a queued rAF shim we pump by hand.
  const queue = [];
  globalThis.requestAnimationFrame = (fn) => { queue.push(fn); return queue.length; };
  globalThis.cancelAnimationFrame = () => {};
  const r = createReckoner({ reducedMotion: false });
  const applied = [];
  const apply = (x, y, est) => applied.push([x, y, est]);
  r.lerpFix('w', 0, 0, { intervalMs: 10000, snap: true, apply });
  r.lerpFix('w', 100, 0, { intervalMs: 10000, apply });
  check('reckon: glide scheduled on new fix', queue.length === 1);
  queue.shift()();   // one frame, ~0ms in: position must be near the start
  const last = applied[applied.length - 1];
  check('reckon: mid-glide position is an amber estimate between fixes',
        last[2] === true && last[0] >= 0 && last[0] < 100);
  r.destroy();
}

// ---- deriveConsole -------------------------------------------------------------
{
  const now = Date.parse('2026-07-02T12:00:00Z');
  const src = {
    now,
    meta: {
      spice_mediums: [[1, 2, 'B2', true], [3, 4, 'C3', false]],
      coriolis: { next_cycle_utc: '2026-07-14T05:00:00Z' },
    },
    instance: { dim: 1 },
    spice: { dimensions: { 1: { large_active: true, ram_active_fields: [{ sector: 'd5' }] } } },
    worms: { dimensions: {
      1: { worms: [{ threat: 'enraged', sector: 'A1' }, { threat: 'submerged' }] },
      0: { worms: [{ threat: 'breaching', sector: 'Z9' }] },
    } },
    sandstorm: { dimensions: { 1: {
      storm_sector: 'E5', heading_yaw: 90,
      storm_scanned_utc: new Date(now - 60000).toISOString(),
    } } },
    spiceAtMs: now - 5000,
    wormsAtMs: now - 5000,
  };
  const c = deriveConsole(src);
  check('console: spice active w/ uppercased blow sector',
        c.spice.active && c.spice.blows[0].sector === 'D5');
  check('console: medium counts', c.spice.mediumTotal === 2 && c.spice.mediumActive === 1);
  check('console: worms scoped to selected dim only',
        c.worms.total === 2 && c.worms.roaming === 1 && c.worms.danger === true &&
        c.worms.sectors.length === 1 && c.worms.sectors[0] === 'A1');
  check('console: asOfMs receipt stamps pass through',
        c.spice.asOfMs === src.spiceAtMs && c.worms.asOfMs === src.wormsAtMs);
  check('console: storm centered w/ verified heading',
        c.storm.state === 'centered' && c.storm.sector === 'E5' &&
        c.storm.heading === 'S' && c.storm.headingProvisional === false);
  const sealed = deriveConsole({ ...src, sandstorm: { dimensions: {} } });
  check('console: missing dim -> storm sealed, never borrows the other dim',
        sealed.storm.state === 'sealed');
}

// ---- me poller failure routing ------------------------------------------------
{
  check('pollers: ME_STALE_MS matches the chip threshold', ME_STALE_MS === 25000);
  const flush = () => new Promise((r) => setTimeout(r, 0));
  const calls = [];
  const applyFix = (kind, payload) => calls.push([kind, payload]);

  // Network/server failure (non-401): engine must get the quiet signal.
  globalThis.fetch = () => Promise.reject(new Error('net down'));
  let p = createPollers('hagga', applyFix);
  p.startMe();
  await flush();
  p.stop();
  check('pollers: /me network failure routes to me-quiet',
        calls.length === 1 && calls[0][0] === 'me-quiet' && !!calls[0][1]);

  // 401: session gone, layer clears via authenticated:false (not me-quiet).
  calls.length = 0;
  globalThis.fetch = () =>
    Promise.resolve({ ok: false, status: 401, json: async () => ({}) });
  p = createPollers('hagga', applyFix);
  p.startMe();
  await flush();
  p.stop();
  check('pollers: /me 401 routes to authenticated:false',
        calls.length === 1 && calls[0][0] === 'me' &&
        calls[0][1].authenticated === false);

  // Success: plain me fix.
  calls.length = 0;
  globalThis.fetch = () => Promise.resolve({
    ok: true, status: 200,
    json: async () => ({ authenticated: true, available: true }),
  });
  p = createPollers('hagga', applyFix);
  p.startMe();
  await flush();
  p.stop();
  check('pollers: /me success routes to a me fix',
        calls.length === 1 && calls[0][0] === 'me' &&
        calls[0][1].authenticated === true);
  delete globalThis.fetch;
}

// ---- wormProximity ---------------------------------------------------------------
{
  const step = 1000 / 9;
  const self = { nx: 500, ny: 500 };
  const worms = [
    { nx: 500, ny: 500 - step * 0.5, sector: 'E5', threat: 'breaching' },
    { nx: 500 + step * 5, ny: 500, sector: 'E9', threat: 'submerged' },
  ];
  const p = wormProximity(self, worms, step);
  check('proximity: nearest worm wins, danger tier, closing flag',
        p && p.tier === 'danger' && p.sector === 'E5' && p.closing === true &&
        p.dir === 'N' && near(p.distSectors, 0.5, 0.01));
  const far = wormProximity(self, [worms[1]], step);
  check('proximity: distant worm -> clear tier',
        far && far.tier === 'clear' && far.closing === false);
  check('proximity: no worms -> null', wormProximity(self, [], step) === null);
  check('proximity: no self -> null', wormProximity(null, worms, step) === null);
}

// ---- worm audio -------------------------------------------------------------------
{
  let pings = 0;
  const fakeCtx = () => ({
    state: 'running', currentTime: 0, destination: {},
    resume() {}, close() {},
    createOscillator: () => ({
      connect() {}, type: '', start() { pings += 1; }, stop() {},
      frequency: { setValueAtTime() {}, exponentialRampToValueAtTime() {} },
    }),
    createGain: () => ({
      connect() {},
      gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} },
    }),
  });
  const audio = createWormAudio({ contextFactory: fakeCtx });
  audio.notifyDanger(true);
  check('audio: muted by default, danger does not ping', pings === 0);
  audio.notifyDanger(false);
  audio.setEnabled(true);
  check('audio: enabling while calm does not ping', pings === 0);
  audio.notifyDanger(true);
  check('audio: pings on danger transition', pings === 1);
  audio.notifyDanger(true);
  check('audio: sustained danger does not re-ping', pings === 1);
  audio.notifyDanger(false);
  audio.notifyDanger(true);
  check('audio: re-arms after danger clears', pings === 2);
  audio.setEnabled(false);
  audio.notifyDanger(false);
  audio.notifyDanger(true);
  check('audio: disabled stays silent', pings === 2);
  audio.setEnabled(true);
  check('audio: enabling while already in danger pings once', pings === 3);
  audio.destroy();
}

// ---- SSE client -------------------------------------------------------------------
{
  check('stream: backoff doubles to cap',
        backoffDelay(0) === 1000 && backoffDelay(1) === 2000 &&
        backoffDelay(10) === BACKOFF_MAX_MS);
  check('stream: url without and with resume id',
        streamUrl('hagga') === '/portal/maps/hagga/stream' &&
        streamUrl('hagga', '7') === '/portal/maps/hagga/stream?last_event_id=7');

  class FakeES {
    static CLOSED = 2;
    static instances = [];
    constructor(url) {
      this.url = url; this.readyState = 1; this.listeners = {};
      this.closedByClient = false;
      FakeES.instances.push(this);
    }
    addEventListener(type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    }
    close() { this.closedByClient = true; this.readyState = 2; }
    emit(type, data, id) {
      (this.listeners[type] || []).forEach((fn) => fn({ data, lastEventId: id || '' }));
    }
  }
  const timers = [];
  const setT = (fn, ms) => { const t = { fn, ms, cleared: false }; timers.push(t); return t; };
  const clearT = (t) => { if (t) t.cleared = true; };
  const fireTimer = (ms) => {
    const i = timers.findIndex((t) => !t.cleared && !t.fired && t.ms === ms);
    if (i === -1) return false;
    timers[i].fired = true;
    timers[i].fn();
    return true;
  };

  let fakeNow = 0;
  const fixes = [];
  const statuses = [];
  const s = liveStream('hagga', (kind, payload) => fixes.push([kind, payload]), {
    EventSourceClass: FakeES,
    setTimeoutFn: setT,
    clearTimeoutFn: clearT,
    nowFn: () => fakeNow,
    onStatus: (st) => statuses.push(st),
  });
  const es0 = FakeES.instances[0];
  check('stream: first connect has no resume id',
        FakeES.instances.length === 1 && es0.url === '/portal/maps/hagga/stream');
  es0.emit('worms', '{"dimensions":{"1":{"worms":[]}}}', '3');
  check('stream: event -> parsed onFix + up status',
        fixes.length === 1 && fixes[0][0] === 'worms' &&
        fixes[0][1].dimensions['1'].worms.length === 0 &&
        statuses.join(',') === 'up');
  es0.emit('players', 'not json {');
  check('stream: malformed JSON dropped, no crash', fixes.length === 1);

  // Fatal error (non-200 / hard close): manual backoff reconnect w/ resume id.
  es0.readyState = 2;
  es0.onerror();
  check('stream: fatal error -> down status', statuses.join(',') === 'up,down');
  check('stream: reconnect scheduled at base backoff', fireTimer(1000));
  const es1 = FakeES.instances[1];
  check('stream: reconnect carries last_event_id',
        FakeES.instances.length === 2 &&
        es1.url === '/portal/maps/hagga/stream?last_event_id=3');
  es1.emit('spice', '{"dimensions":{}}', '4');
  check('stream: recovery flips up again',
        statuses.join(',') === 'up,down,up' && fixes.length === 2);

  // Flap: connection died shortly after its snapshot — backoff must GROW
  // (an event alone must not reset it; see STABLE_RESET_MS).
  es1.readyState = 2;
  es1.onerror();
  check('stream: post-snapshot flap does not reset backoff', fireTimer(2000));
  const es2 = FakeES.instances[2];
  check('stream: flap reconnect still carries last_event_id',
        FakeES.instances.length === 3 &&
        es2.url === '/portal/maps/hagga/stream?last_event_id=4');
  // Stable past STABLE_RESET_MS before the next event: backoff resets to base.
  fakeNow += STABLE_RESET_MS + 1000;
  es2.emit('worms', '{"dimensions":{}}', '5');
  es2.readyState = 2;
  es2.onerror();
  check('stream: stable stream resets backoff to base', fireTimer(1000));
  const es3 = FakeES.instances[3];
  es3.emit('spice', '{"dimensions":{}}', '6');

  // Non-fatal error (browser auto-reconnect in flight): down, no manual ES.
  es3.readyState = 0;
  es3.onerror();
  check('stream: transient error -> down, no manual reconnect',
        statuses[statuses.length - 1] === 'down' && FakeES.instances.length === 4);

  s.close();
  check('stream: close() closes the source', es3.closedByClient === true);
  es3.emit('worms', '{"dimensions":{}}', '9');
  check('stream: closed stream ignores late events', fixes.length === 4);
}

// ---- result -----------------------------------------------------------------------
if (failures) {
  console.error('\nsmoke: ' + failures + ' failure(s)');
  process.exit(1);
}
console.log('\nsmoke: all checks passed');
