// Node smoke for the map engine's DOM-free logic (npm run smoke). Covers the
// M1 math/derivation modules plus the M2 additions: worm audio transitions,
// worm proximity, and the SSE client (via injected EventSource/timer seams).
// 2.26.0 adds the holo terrain source/cache logic (terrain.js takes THREE
// injected, so it runs headless against real three with a stubbed fetch).
// DOM rendering (drawMe/drawWaypoints/engine) is browser-QA territory.

import { readFileSync, existsSync } from 'node:fs';
import * as THREE from 'three';

import {
  createView, setZoom, panToNormalized, screenToNormalized,
  setOrbit, stepHeading, orbitLerp, rotateScreenDelta, ORBIT_DEFAULT, TILT_MIN, TILT_MAX,
  attachGestures,
} from '../src/lib/map/transform.js';
import { sectorCenter } from '../src/lib/map/grid.js';
import { headingToCompass, compass, wormLabel } from '../src/lib/map/overlays.js';
import { createReckoner } from '../src/lib/map/reckon.js';
import {
  deriveConsole, createPollers, mediumsForDim, ME_STALE_MS,
} from '../src/lib/map/pollers.js';
import { wormProximity } from '../src/lib/map/me.js';
import { createWormAudio } from '../src/lib/map/audio.js';
import {
  liveStream, streamUrl, backoffDelay, BACKOFF_MAX_MS, STABLE_RESET_MS,
} from '../src/lib/map/stream.js';
import {
  createTerrain, terrainSourceFor, surfaceFor,
} from '../src/lib/map/holo/terrain.js';
import { forcedLayout, forcedRelief } from '../src/lib/map/holo/debug.js';
import { createHoloMaterial } from '../src/lib/map/holo/material.js';
import { createCamera } from '../src/lib/map/holo/camera.js';
import atlas from '../src/lib/map/holo/atlas.js';
import {
  rectFor, isDenseCat, isMidCat, lodClass, MID_ZOOM_START, MID_ZOOM_FULL, iconFraction, rankFor, drawRank, FALLBACK_RECT,
  ICON_ZOOM_START, ICON_ZOOM_FULL, ICON_BUDGET,
} from '../src/lib/map/holo/icon-atlas.js';
import { nearestMarker } from '../src/lib/map/markers.js';
import { lightFor, holoLight, sunDir, lerpWeights, hourOverride, readMode, saveMode, TOD_KEY, RELIGHT_MS } from '../src/lib/map/holo/light.js';
import {
  clusterLevel, buildClusters, discPx, badgeGlyphs, zoomForBounds, isClusterCat, markerVisible,
  CLUSTER_OFF_ZOOM, CLUSTER_MIN, CLUSTER_CAP,
} from '../src/lib/map/holo/cluster.js';

import {
  buildStones, cycle, stoneStatus, bandFor, stoneBands, chooseView, shouldArrive, mapHref, overmapToArt4096,
  cardModel, anchorStoneId, worldUrl, travelBadge,
} from '../src/lib/world/data.js';

import { todWeights, todPreset, resolveHour, blendLook } from '../src/lib/world/tod.js';
import { createFx, FX_CAPS } from '../src/lib/map/sandtable/fx.js';
import { stormFromLive, stormsFromLive, STORM_STONES_MAP } from '../src/lib/world/data.js';
import {
  stormFootprint, positionAt, isOver, clearsInSec, fmtClears, stormLabel, hitFootprint, STORM_HOLD_S, STORM_STALE_MS,
} from '../src/lib/map/stormFootprint.js';

import { solveRadii, overlaps, baseRadius, DISC } from '../src/lib/world/layout.js';

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

// ---- transform: holo orbit ----------------------------------------------------
{
  const v = createView(1000);
  check('orbit: default view is north up at 35 deg',
        v.azimuth === 0 && v.tilt === 35 && ORBIT_DEFAULT.azimuth === 0 && ORBIT_DEFAULT.tilt === 35);
  setOrbit(v, 405, 5);
  check('orbit: azimuth wraps, tilt clamps low', v.azimuth === 45 && v.tilt === TILT_MIN && TILT_MIN === 20);
  setOrbit(v, -90, 95);
  check('orbit: negative azimuth wraps, tilt clamps high', v.azimuth === 270 && v.tilt === TILT_MAX && TILT_MAX === 80);
  setOrbit(v, NaN, undefined);
  check('orbit: non-finite input resets to the default', v.azimuth === 0 && v.tilt === 35);
  check('orbit: Q/E steps snap to 45 deg and wrap',
        stepHeading(0, 1) === 45 && stepHeading(0, -1) === 315 && stepHeading(315, 1) === 0 &&
        stepHeading(30, 1) === 45 && stepHeading(30, -1) === 0 && stepHeading(90, -1) === 45);
  const mid = orbitLerp({ azimuth: 350, tilt: 60 }, { azimuth: 10, tilt: 20 }, 0.5);
  check('orbit: lerp takes the short way round', near(mid.azimuth, 0) && near(mid.tilt, 40));
  const back = orbitLerp({ azimuth: 200, tilt: 70 }, ORBIT_DEFAULT, 1);
  check('orbit: reset lands exactly on north up at 35 deg', near(back.azimuth % 360, 0) && back.tilt === 35);
  setOrbit(v, 0, 35);
  const d0 = rotateScreenDelta(v, 12, -7);
  check('orbit: pan delta is identity at heading 0', d0.dx === 12 && d0.dy === -7);
  setOrbit(v, 90, 35);
  const d90 = rotateScreenDelta(v, 10, 0);
  check('orbit: facing east, a rightward drag pans the board south', near(d90.dx, 0) && near(d90.dy, 10));
}

// ---- gestures: orbit routing on the engine view ---------------------------------
{
  function fakeTarget() {
    const ls = {};
    return {
      ls,
      addEventListener(t, fn) { (ls[t] = ls[t] || []).push(fn); },
      removeEventListener(t, fn) { ls[t] = (ls[t] || []).filter((f) => f !== fn); },
      fire(t, e) { (ls[t] || []).forEach((fn) => fn(Object.assign({ preventDefault() {} }, e))); },
    };
  }
  function rig(rotate) {
    const vp = Object.assign(fakeTarget(), {
      setPointerCapture() {}, classList: { add() {}, remove() {} },
      getBoundingClientRect: () => ({ left: 0, top: 0 }),
    });
    const v = createView(1000);
    v.display = 500;
    setZoom(v, 4, 250, 250);
    const steps = [];
    rigs.push(null);
    const detach = attachGestures(vp, v, {
      onTransform() {}, onInteract() {}, onHover() {}, onLeave() {}, onClick() {},
      onStep(d) { steps.push(d); },
    }, { rotate });
    rigs[rigs.length - 1] = detach;
    return { vp, v, steps, detach };
  }
  const rigs = [];
  // Real fingers report alternately in small steps; move both that way.
  function twoFinger(vp, from, to, n) {
    for (let i = 1; i <= n; i++) {
      const t = i / n;
      for (let k = 0; k < 2; k++) {
        vp.fire('pointermove', P(k + 1, from[k][0] + (to[k][0] - from[k][0]) * t,
                                 from[k][1] + (to[k][1] - from[k][1]) * t));
      }
    }
  }
  const hadWindow = 'window' in globalThis;
  const prevWindow = globalThis.window;
  const win = fakeTarget();
  globalThis.window = win;
  const P = (id, x, y, more) => Object.assign({ pointerId: id, clientX: x, clientY: y, pointerType: 'touch', button: 0 }, more);

  // Mouse right-drag and Ctrl+drag orbit; left-drag still pans.
  let r = rig(true);
  r.vp.fire('pointerdown', P(1, 200, 200, { pointerType: 'mouse', button: 2 }));
  r.vp.fire('pointermove', P(1, 300, 250, { pointerType: 'mouse' }));
  r.vp.fire('pointerup', P(1, 300, 250, { pointerType: 'mouse' }));
  check('gestures: right-drag rotates (x) and tilts (y)', near(r.v.azimuth, 320) && near(r.v.tilt, 50));
  const pan0 = [r.v.panX, r.v.panY];
  r.vp.fire('pointerdown', P(1, 200, 200, { pointerType: 'mouse', button: 0, ctrlKey: true }));
  r.vp.fire('pointermove', P(1, 200, 100, { pointerType: 'mouse' }));
  r.vp.fire('pointerup', P(1, 200, 100, { pointerType: 'mouse' }));
  check('gestures: Ctrl+drag orbits without panning', near(r.v.tilt, 20) && r.v.panX === pan0[0] && r.v.panY === pan0[1]);
  setOrbit(r.v, 90, 35);
  const before = [r.v.panX, r.v.panY];
  r.vp.fire('pointerdown', P(1, 200, 200));
  r.vp.fire('pointermove', P(1, 240, 200));
  r.vp.fire('pointerup', P(1, 240, 200));
  check('gestures: one-finger pan follows the rotated view (east-facing: x drag -> board y)',
        near(r.v.panX, before[0]) && near(r.v.panY, before[1] + 40));

  // Two fingers: twist rotates, parallel vertical drag tilts, spread zooms.
  r = rig(true);
  r.vp.fire('pointerdown', P(1, 200, 250));
  r.vp.fire('pointerdown', P(2, 300, 250));
  // Turn the finger line 40 deg clockwise about its midpoint (in steps, so
  // the turn passes the 10 deg twist threshold on the way).
  for (let i = 1; i <= 20; i++) {
    const a = (i / 20) * 40 * Math.PI / 180;
    r.vp.fire('pointermove', P(1, 250 - Math.cos(a) * 50, 250 - Math.sin(a) * 50));
    r.vp.fire('pointermove', P(2, 250 + Math.cos(a) * 50, 250 + Math.sin(a) * 50));
  }
  check('gestures: two-finger twist rotates the heading (' + r.v.azimuth.toFixed(1) + ')',
        r.v.azimuth > 320 && r.v.azimuth < 335);
  r.vp.fire('pointerup', P(1, 0, 0));
  r.vp.fire('pointerup', P(2, 0, 0));
  r = rig(true);
  const zp = [r.v.zoom, r.v.panX, r.v.panY];
  r.vp.fire('pointerdown', P(1, 200, 250));
  r.vp.fire('pointerdown', P(2, 300, 250));
 twoFinger(r.vp, [[200, 250], [300, 250]], [[200, 290], [300, 290]], 10);
  check('gestures: two-finger vertical drag tilts, no pan or zoom',
        near(r.v.tilt, 35 + 40 * 0.3) && r.v.zoom === zp[0] && r.v.panX === zp[1] && r.v.panY === zp[2]);
  r.vp.fire('pointerup', P(1, 0, 0));
  r.vp.fire('pointerup', P(2, 0, 0));
  r = rig(true);
  r.vp.fire('pointerdown', P(1, 200, 250));
  r.vp.fire('pointerdown', P(2, 300, 250));
  twoFinger(r.vp, [[200, 250], [300, 250]], [[150, 250], [350, 250]], 10);
  check('gestures: pinch still zooms with rotation on', r.v.zoom > 4 && r.v.azimuth === 0 && r.v.tilt === 35);
  r.vp.fire('pointerup', P(1, 0, 0));
  r.vp.fire('pointerup', P(2, 0, 0));

  // Keyboard steps reach the engine; typing in a field does not.
  win.fire('keydown', { key: 'q', target: { tagName: 'DIV' } });
  win.fire('keydown', { key: 'E', target: { tagName: 'DIV' } });
  win.fire('keydown', { key: 'e', target: { tagName: 'INPUT' } });
  check('gestures: Q/E step the heading, ignored while typing',
        r.steps.length === 2 && r.steps[0] === -1 && r.steps[1] === 1);
  rigs.forEach((d) => d());
  check('gestures: detach removes the key listener', (win.ls.keydown || []).length === 0);

  // Carved (no rotate): right-drag is a plain V1 pan, heading never moves.
  r = rig(false);
  const cp = [r.v.panX, r.v.panY];
  r.vp.fire('pointerdown', P(1, 200, 200, { pointerType: 'mouse', button: 2 }));
  r.vp.fire('pointermove', P(1, 230, 210, { pointerType: 'mouse' }));
  r.vp.fire('pointerup', P(1, 230, 210, { pointerType: 'mouse' }));
  check('gestures: carved board ignores orbit (pans, heading stays 0)',
        r.v.azimuth === 0 && r.v.tilt === 35 && r.v.panX === cp[0] + 30 && r.v.panY === cp[1] + 10 &&
        !(win.ls.keydown || []).length && !(r.vp.ls.contextmenu || []).length);
  r.detach();

  if (hadWindow) globalThis.window = prevWindow; else delete globalThis.window;
}

// ---- holo camera: projector at any heading/tilt -----------------------------------
{
  const el = { clientWidth: 800, clientHeight: 600 };
  const cam = createCamera(THREE, { view: 1000, el });
  cam.setView({ zoom: 1, panX: 0, panY: 0 });
  const p0 = cam.camera.position.clone();
  cam.setView({ zoom: 1, panX: 0, panY: 0, azimuth: 0, tilt: 35 });
  const e = 35 * Math.PI / 180, dist = 1000 * 1.95;
  check('camera: default orbit equals the old fixed lean',
        p0.equals(cam.camera.position) && near(p0.x, 0) &&
        near(p0.y, dist * Math.sin(e), 1e-6) && near(p0.z, dist * Math.cos(e), 1e-6));
  let worst = 0, missing = 0;
  const pts = [[500, 500], [300, 420], [620, 560], [450, 610]];
  [[0, 35], [45, 35], [90, 60], [180, 20], [270, 80], [333, 50]].forEach(([az, tilt]) => {
    cam.setView({ zoom: 2, panX: -200, panY: -150, azimuth: az, tilt });
    pts.forEach(([nx, ny]) => {
      const s = cam.projector.project(nx, ny);
      const n = cam.projector.unproject(s.screenX, s.screenY);
      if (!n) { missing++; return; }
      worst = Math.max(worst, Math.hypot(n.nx - nx, n.ny - ny));
    });
  });
  check('camera: project/unproject round-trips at every heading and tilt (err ' + worst.toExponential(1) + ')',
        missing === 0 && worst < 1e-3);
  cam.setView({ zoom: 1, panX: 0, panY: 0, azimuth: 90, tilt: 35 });
  const right = cam.projector.unproject(520, 300), left = cam.projector.unproject(280, 300);
  check('camera: facing east, screen right is board south',
        right && left && right.ny > left.ny && Math.abs(right.nx - left.nx) < Math.abs(right.ny - left.ny));
  cam.setView({ zoom: 1, panX: 0, panY: 0, azimuth: 0, tilt: 5 });
  check('camera: tilt is clamped to 20 even if the view slips through',
        near(cam.camera.position.y, dist * Math.sin(20 * Math.PI / 180), 1e-6));

  // Relief-aware projector (QA 2.26.0 bug 2): markers are drawn at heightAt, so a
  // click on a raised marker must resolve to that marker, not the sand behind it.
  const mesa = (nx, ny) => 80 * Math.exp(-((nx - 450) ** 2 + (ny - 480) ** 2) / (2 * 60 * 60));
  const rc = createCamera(THREE, { view: 1000, el, heightAt: mesa });
  let rWorst = 0, rMissing = 0, flatWorst = 0;
  [[0, 35], [90, 60], [180, 20], [270, 80], [135, 59]].forEach(([az, tilt]) => {
    rc.setView({ zoom: 2, panX: -100, panY: -80, azimuth: az, tilt });
    cam.setView({ zoom: 2, panX: -100, panY: -80, azimuth: az, tilt });
    [[450, 480], [470, 470], [430, 500]].forEach(([nx, ny]) => {
      const s = rc.projector.project(nx, ny);
      const n = rc.projector.unproject(s.screenX, s.screenY);
      if (!n) { rMissing++; return; }
      // Ground truth = the first surface crossing along that pixel's ray (a point
      // on a mesa's far slope is hidden at low tilt; the right answer is the face
      // in front of it), found by a fine brute-force march.
      const rr = new THREE.Raycaster();
      rr.setFromCamera(new THREE.Vector2((s.screenX / 800) * 2 - 1, -((s.screenY / 600) * 2 - 1)), rc.camera);
      const o = rr.ray.origin, d = rr.ray.direction;
      let gx = null, gy = null;
      for (let t = 0; t < 8000; t += 0.05) {
        const x = o.x + d.x * t + 500, z = o.z + d.z * t + 500;
        if (o.y + d.y * t <= mesa(x, z)) { gx = x; gy = z; break; }
      }
      if (gx === null) { rMissing++; return; }
      rWorst = Math.max(rWorst, Math.hypot(n.nx - gx, n.ny - gy));
      const f = cam.projector.unproject(s.screenX, s.screenY);   // the old flat-plane answer
      if (f) flatWorst = Math.max(flatWorst, Math.hypot(f.nx - nx, f.ny - ny));
    });
  });
  check('camera: relief-aware unproject finds the first surface hit (err ' + rWorst.toFixed(3) + ', flat plane was ' + flatWorst.toFixed(0) + ')',
        rMissing === 0 && rWorst < 0.5 && flatWorst > 10);
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

// ---- storm card: reader state only, never a timing guess (2.26.1) ----------------
{
  const now = Date.parse('2026-10-07T12:00:00Z');
  const card = (info) => deriveConsole({ now, meta: {}, instance: { dim: 1 }, sandstorm: { dimensions: { 1: info } } }).storm;
  const ago = (m) => new Date(now - m * 60000).toISOString();
  const a = card({ storm_state: 'active', storm_sector: 'E5', heading_yaw: 90, ends_utc: '2026-10-07T12:04:30Z', next_eta_utc: '2026-10-07T13:00:00Z' });
  check('storm card: active shows the sector and clears-in from ends_utc',
        a.state === 'centered' && a.sector === 'E5' && a.endsUtc === '2026-10-07T12:04:30Z');
  const l = card({ storm_state: 'active', storm_scanned_utc: ago(1), lifetime_remaining: 120 });
  check('storm card: active without ends_utc derives it from lifetime_remaining', l.state === 'centered' && l.sector === null &&
        l.endsUtc === new Date(now - 60000 + 120000).toISOString());
  const c = card({ storm_state: 'clear', storm_sector: 'E5', storm_scanned_utc: ago(1), next_eta_utc: '2026-10-07T13:00:00Z' });
  check('storm card: clear is the ETA countdown even with a fresh sector', c.state === 'eta' && c.etaUtc === '2026-10-07T13:00:00Z');
  const u = card({ storm_state: 'unknown', last_spawn_utc: ago(1), next_eta_utc: '2026-10-07T13:00:00Z' });
  const p = card({ last_spawn_utc: ago(1), next_eta_utc: '2026-10-07T13:00:00Z' });
  check('storm card: unknown or pre-v2 with only a recent spawn never says sweeping', u.state === 'eta' && p.state === 'eta');
  const v1 = card({ storm_sector: 'D4', storm_scanned_utc: ago(2) });
  check('storm card: pre-v2 relay with a fresh scanned sector is still on the board', v1.state === 'centered' && v1.sector === 'D4');
}

// ---- per-instance medium spice sites --------------------------------------------
{
  // DD cal: /live carries each dim's ram_mediums in world cm; the /data union
  // mixes PvE + PvP. Projection must match map_model.normalize (0.1 rounding).
  const cal = { originX: -1270000, spanX: 2438400, originY: -1270000, spanY: 2438400, flipY: false };
  const meta = { cal, view: 1000, spice_mediums: [[1, 1, 'I1', true], [2, 2, 'I1', false], [3, 3, 'I1', false]] };
  const spice = { dimensions: {
    0: { ram_mediums: [{ sector: 'd4', x: -229000, y: 126600, active: true }, { sector: 'D3', x: -635400, y: 228200, active: false }] },
    1: { ram_mediums: [{ sector: 'D3', x: -635400, y: 228200, active: false }] },
  } };
  const pve = mediumsForDim(meta, spice, 0);
  check('mediums: per-dim sites projected like the backend',
        pve.length === 2 && pve[0][0] === 426.9 && pve[0][1] === 572.8 && pve[0][2] === 'D4' && pve[0][3] === true &&
        pve[1][0] === 260.3 && pve[1][1] === 614.4);
  const pvp = mediumsForDim(meta, spice, 1);
  check('mediums: other dim keeps its own sites and states', pvp.length === 1 && pvp[0][3] === false);
  check('mediums: no /live for the dim -> /data union fallback', mediumsForDim(meta, null, 0).length === 3 &&
        mediumsForDim(meta, { dimensions: { 0: {} } }, 0).length === 3);
  const c = deriveConsole({ meta, spice, instance: { dim: 0 }, now: 0 });
  check('console: medium counts are the selected instance only', c.spice.mediumTotal === 2 && c.spice.mediumActive === 1);
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

// ---- holo terrain: source selection ---------------------------------------------
{
  const dd = { key: 'deep-desert', layout: { id: 4, confidence: 0.9, source: 'match' } };
  const hg = terrainSourceFor({ key: 'hagga', grid: null }, { base: '/portal', tier: 'high' });
  check('terrain src: hagga -> static bake in /terrain/hagga/, id 0',
        hg && hg.base === '/portal/terrain/hagga/' && hg.layout.id === 0 && hg.drape === true);
  check('terrain src: hagga is true scale (boost 1.0) and 511 seg on high tier',
        hg.reliefBoost === 1 && hg.segMax === 511 && hg.lowDensity === false);
  const hgMid = terrainSourceFor({ key: 'hagga' }, { base: '', tier: 'mid' });
  const hgLow = terrainSourceFor({ key: 'hagga' }, { base: '', tier: 'low' });
  check('terrain src: hagga mid tier caps at 383, low tier is low density',
        hgMid.segMax === 383 && hgLow.lowDensity === true && hgMid.base === '/terrain/hagga/');
  const d = terrainSourceFor(dd, { base: '/portal', tier: 'high' });
  check('terrain src: deep-desert -> scene.layout from /terrain/, boost 2, 511 seg on high, bake-only drape',
        d && d.base === '/portal/terrain/' && d.layout === dd.layout &&
        d.reliefBoost === 2 && d.segMax === 511 && d.lowDensity === false &&
        d.drape === true && d.bakeDrapeOnly === true);
  const ddMid = terrainSourceFor(dd, { base: '', tier: 'mid' });
  const ddLow = terrainSourceFor(dd, { base: '', tier: 'low' });
  check('terrain src: deep-desert mid tier caps at 383, low tier is low density',
        ddMid.segMax === 383 && ddMid.lowDensity === false && ddLow.lowDensity === true);
  check('terrain src: deep-desert without a layout stays seeded',
        terrainSourceFor({ key: 'deep-desert', layout: null }, {}) === null);
  const ak = terrainSourceFor({ key: 'arrakeen' }, { base: '' });
  const hv = terrainSourceFor({ key: 'harko-village' }, { base: '' });
  check('terrain src: hubs load their diorama bake, drape, and never ask for a -2048 backdrop',
        ak && ak.base === '/terrain/arrakeen/' && ak.drape && ak.smallVariant === false &&
        hv && hv.base === '/terrain/harko-village/' && hv.layout.id === 0 &&
        surfaceFor(ak, { meta: {}, base: '/terrain/arrakeen/', v: '' },
                   { type: 'image', src: '/admin/static/img/maps/arrakeen-map.webp?v=3' }, true).drapeUrl
          === '/admin/static/img/maps/arrakeen-map.webp?v=3');
  check('terrain src: unknown and keyless scenes stay seeded',
        terrainSourceFor({ key: 'harko' }, {}) === null &&
        terrainSourceFor({ grid: { cols: 9, rows: 9 } }, {}) === null);
  const forcedDd = terrainSourceFor(dd, { forcedLayout: 7 });
  const forcedHg = terrainSourceFor({ key: 'hagga' }, { forcedLayout: 7 });
  check('terrain src: forced layout applies on DD, ignored on hagga',
        forcedDd.layout.id === 7 && forcedDd.layout.source === 'debug' &&
        forcedHg.layout.id === 0 && forcedHg.base === '/terrain/hagga/');
  check('terrain src: ?holoRelief overrides the boost on every source',
        terrainSourceFor({ key: 'hagga' }, { forcedRelief: 1.5 }).reliefBoost === 1.5 &&
        terrainSourceFor(dd, { forcedRelief: 3 }).reliefBoost === 3);
}

// ---- holo debug: ?holoLayout is DD-only, ?holoRelief everywhere --------------------
{
  const hadWindow = 'window' in globalThis;
  const prevWindow = globalThis.window;
  globalThis.window = { location: { href: 'https://x/maps/hagga?inst=pve?holoLayout=3&holoRelief=1.5' } };
  check('debug: ?holoLayout ignored off DD',
        forcedLayout('hagga') === null && forcedLayout('arrakeen') === null);
  check('debug: ?holoLayout applies on DD (and keyless for the chip)',
        forcedLayout('deep-desert') === 3 && forcedLayout() === 3);
  check('debug: ?holoRelief parses on any map', forcedRelief() === 1.5);
  if (hadWindow) globalThis.window = prevWindow; else delete globalThis.window;
}

// ---- holo terrain: base + id cache, surfaces ---------------------------------------
{
  // Two tiny bakes that both call themselves layout 0: they must never collide.
  function bake(w, value, extra) {
    const buf = new ArrayBuffer(w * w * 2);
    const dv = new DataView(buf);
    for (let i = 0; i < w * w; i++) dv.setUint16(i * 2, value, true);
    return {
      manifest: { bakeStamp: 42, layouts: [{ id: 0, bin: 'b.bin', meta: 'b.json' }] },
      bin: buf,
      meta: Object.assign({ grid: [w, w], zMinCm: 0, zMaxCm: 65535, floorCm: 0,
                            worldSpan: [1000000, 1000000], heightScale: 1 }, extra || {}),
    };
  }
  const files = {};
  function host(base, b) {
    files[base + 'manifest.json'] = b.manifest;
    files[base + 'b.bin'] = b.bin;
    files[base + 'b.json'] = b.meta;
  }
  host('/c/terrain/', bake(4, 10000));
  host('/c/terrain/hagga/', bake(4, 40000, { normalMap: 'n.webp' }));
  const fetched = [];
  const prevFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    fetched.push(url);
    const body = files[url.split('?')[0]];
    if (body === undefined) return { ok: false, status: 404 };
    return {
      ok: true, status: 200,
      json: async () => body,
      arrayBuffer: async () => body,
    };
  };
  const warn = console.warn;
  console.warn = () => {};

  const mat = new THREE.MeshBasicMaterial();
  const t1 = createTerrain(THREE, { view: 1000, material: mat });
  const seeded = t1.heightAt(500, 500);
  const ddSrc = terrainSourceFor({ key: 'deep-desert', layout: { id: 0, source: 'match' } }, { base: '/c' });
  const hgSrc = terrainSourceFor({ key: 'hagga' }, { base: '/c', tier: 'high' });

  check('terrain cache: DD layout 0 loads', await t1.applyLayout(ddSrc.layout, ddSrc) === true);
  const ddH = t1.heightAt(500, 500);
  check('terrain cache: same base+id again is a no-op',
        await t1.applyLayout(ddSrc.layout, ddSrc) === false);
  check('terrain cache: Hagga layout 0 is a different bake (no collision)',
        await t1.applyLayout(hgSrc.layout, hgSrc) === true && t1.heightAt(500, 500) !== ddH);
  check('terrain cache: heights follow each bake (DD x2 boost, Hagga true scale)',
        near(ddH, 10000 / 1000 * 2, 1e-6) && near(t1.heightAt(500, 500), 40000 / 1000, 1e-6) &&
        ddH !== seeded);
  check('terrain cache: active() names the Hagga bake + bakeStamp',
        t1.active() && t1.active().base === '/c/terrain/hagga/' && t1.active().v === '?v=42');
  const n1 = fetched.length;
  const t2 = createTerrain(THREE, { view: 1000, material: mat });
  await t2.applyLayout(hgSrc.layout, hgSrc);
  check('terrain cache: a new viewer reuses the decoded raster (no refetch)',
        fetched.length === n1 && near(t2.heightAt(500, 500), 40000 / 1000, 1e-6));

  // Failure: missing bake keeps the seeded relief, warns once, no retry storm.
  const badSrc = terrainSourceFor({ key: 'hagga' }, { base: '/missing' });
  const t3 = createTerrain(THREE, { view: 1000, material: mat });
  check('terrain fail: missing hagga bake falls back to seeded',
        await t3.applyLayout(badSrc.layout, badSrc) === false &&
        t3.heightAt(500, 500) === seeded && t3.active() === null);
  const n2 = fetched.length;
  await t3.applyLayout(badSrc.layout, badSrc);
  check('terrain fail: a failed key is not refetched by the same viewer', fetched.length === n2);
  const t4 = createTerrain(THREE, { view: 1000, material: mat });
  await t4.applyLayout(badSrc.layout, badSrc);
  check('terrain fail: failures are not cached across viewers', fetched.length > n2);

  // Surfaces: normal map only when meta names one; drape only for Hagga + image.
  const img = { type: 'image', src: '/assets/HaggaBasin.webp', w: 8192, h: 8192 };
  const sHg = surfaceFor(hgSrc, t1.active(), img, false);
  check('surface: hagga gets normal map (versioned) + full drape',
        sHg.normalUrl === '/c/terrain/hagga/n.webp?v=42' && sHg.drapeUrl === '/assets/HaggaBasin.webp');
  check('surface: small devices get the 2048 drape',
        surfaceFor(hgSrc, t1.active(), img, true).drapeUrl === '/assets/HaggaBasin-2048.webp');
  const t5 = createTerrain(THREE, { view: 1000, material: mat });
  await t5.applyLayout(ddSrc.layout, ddSrc);
  const sDd = surfaceFor(ddSrc, t5.active(), img, false);
  check('surface: a DD bake without meta.normalMap / meta.drape gets neither, never the 2D backdrop',
        sDd.normalUrl === null && sDd.drapeUrl === null &&
        surfaceFor(ddSrc, t5.active(), { type: 'image', src: '/admin/static/img/maps/dd-islands/dd-layout-0.webp' }, false)
          .drapeUrl === null);
  const sSeed = surfaceFor(hgSrc, null, img, false);
  check('surface: seeded fallback gets neither', sSeed.normalUrl === null && sSeed.drapeUrl === null);
  check('surface: no image backdrop -> no drape',
        surfaceFor(hgSrc, t1.active(), { type: 'sand' }, false).drapeUrl === null);

  // The shipped Hagga bake: true-scale heights stay in the DD visual band, and
  // the 1024 raster meshes at 511 segments on high, 95 when low density.
  const dir = new URL('../static/terrain/hagga/', import.meta.url);
  const man = JSON.parse(readFileSync(new URL('manifest.json', dir), 'utf8'));
  const realMeta = JSON.parse(readFileSync(new URL('hagga.json', dir), 'utf8'));
  const realBin = readFileSync(new URL('hagga.bin', dir));
  files['/r/terrain/hagga/manifest.json'] = man;
  files['/r/terrain/hagga/hagga.bin'] = realBin.buffer.slice(realBin.byteOffset, realBin.byteOffset + realBin.byteLength);
  files['/r/terrain/hagga/hagga.json'] = realMeta;
  const realSrc = terrainSourceFor({ key: 'hagga' }, { base: '/r', tier: 'high' });
  const t6 = createTerrain(THREE, { view: 1000, material: mat });
  check('hagga bake: loads through the static source', await t6.applyLayout(realSrc.layout, realSrc) === true);
  let peak = 0;
  for (let y = 0; y <= 1000; y += 10) {
    for (let x = 0; x <= 1000; x += 10) peak = Math.max(peak, t6.heightAt(x, y));
  }
  const expectPeak = (realMeta.zMaxCm - realMeta.floorCm) / (realMeta.worldSpan[0] / 1000) * realMeta.heightScale;
  check('hagga bake: true-scale peak within the raster range (' + peak.toFixed(1) + ' board units)',
        peak > 10 && peak <= expectPeak + 1e-6 && peak < 120);
  const side = realMeta.grid[0] > 512 ? 512 : realMeta.grid[0];
  check('hagga bake: high tier mesh is min(grid-1, 511) segments',
        t6.mesh.geometry.attributes.position.count === side * side);
  const t7 = createTerrain(THREE, { view: 1000, material: mat });
  await t7.applyLayout(realSrc.layout, terrainSourceFor({ key: 'hagga' }, { base: '/r', tier: 'low' }));
  check('hagga bake: low tier mesh is 95 segments',
        t7.mesh.geometry.attributes.position.count === 96 * 96);

  // The shipped DD bakes (2.26.0 full detail): 1024 rasters with a normal map each,
  // no coverage mask, hologram look (no drape), 511 segments on the high tier.
  const ddDir = new URL('../static/terrain/', import.meta.url);
  const ddMan = JSON.parse(readFileSync(new URL('manifest.json', ddDir), 'utf8'));
  check('dd bake: manifest has layouts 0..11 at 1024',
        JSON.stringify(ddMan.layouts.map((l) => l.id)) === JSON.stringify([...Array(12).keys()]) &&
        ddMan.raster[0] === 1024 && ddMan.raster[1] === 1024);
  const ddMetas = ddMan.layouts.map((l) => JSON.parse(readFileSync(new URL(l.meta, ddDir), 'utf8')));
  check('dd bake: every layout names its normal map + drape and carries no mask/coverage key',
        ddMetas.every((m, i) => m.normalMap === 'layout_' + i + '_normal.webp' && m.grid[0] === 1024 &&
                      m.drape === 'layout_' + i + '_drape.webp' && typeof m.drapeGain === 'number' &&
                      !('mask' in m) && !('coverage' in m) && !('islands' in m)));
  files['/d/terrain/manifest.json'] = ddMan;
  for (const l of ddMan.layouts.filter((x) => x.id === 6)) {
    const b = readFileSync(new URL(l.bin, ddDir));
    files['/d/terrain/' + l.bin] = b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength);
    files['/d/terrain/' + l.meta] = ddMetas[6];
  }
  const ddReal = terrainSourceFor({ key: 'deep-desert', layout: { id: 6, source: 'match' } }, { base: '/d', tier: 'high' });
  const t8 = createTerrain(THREE, { view: 1000, material: mat });
  check('dd bake: layout 6 loads', await t8.applyLayout(ddReal.layout, ddReal) === true);
  check('dd bake: high tier mesh is 511 segments',
        t8.mesh.geometry.attributes.position.count === 512 * 512);
  let ddPeak = 0;
  for (let y = 0; y <= 1000; y += 5) {
    for (let x = 0; x <= 1000; x += 5) ddPeak = Math.max(ddPeak, t8.heightAt(x, y));
  }
  check('dd bake: boosted peak (clipped Shield Wall) in the seeded band (' + ddPeak.toFixed(1) + ' board units)',
        ddPeak > 30 && ddPeak < 80);
  const sDdReal = surfaceFor(ddReal, t8.active(), img, false);
  check('dd bake: normal map + its own drape (versioned, not the backdrop), relief = heightScale x boost',
        sDdReal.normalUrl === '/d/terrain/layout_6_normal.webp?v=' + ddMan.bakeStamp &&
        sDdReal.drapeUrl === '/d/terrain/layout_6_drape.webp?v=' + ddMan.bakeStamp &&
        sDdReal.drapeGain === ddMetas[6].drapeGain && t8.active().relief === ddMetas[6].heightScale * 2);
  check('dd bake: small tier takes the 2048 phone drape, every layout ships one',
        surfaceFor(ddReal, t8.active(), img, true).drapeUrl === '/d/terrain/layout_6_drape-2048.webp?v=' + ddMan.bakeStamp &&
        ddMetas.every((m, i) => m.drapeSmall === `layout_${i}_drape-2048.webp`));
  const hm = createHoloMaterial(THREE, {
    view: 1000, tokens: { accent: new THREE.Color(1, 0.6, 0.2), accentBright: new THREE.Color(1, 0.8, 0.4), bgDeep: new THREE.Color(0, 0, 0) },
  });
  hm.setGrid(true);
  check('dd drape keeps the sector grid: uGrid on and the drape branch mixes the grid lines in',
        hm.material.uniforms.uGrid.value === 1 &&
        /if \(uHasDrape > 0\.5\) \{[\s\S]*?lit = mix\(lit, uAccent, g \* [0-9.]+\);[\s\S]*?return;/.test(hm.material.fragmentShader));
  hm.dispose();
  const ddForced = terrainSourceFor({ key: 'deep-desert', layout: null }, { base: '/d', forcedLayout: 6, tier: 'high' });
  check('dd bake: ?holoLayout source resolves to the same bake',
        ddForced.base === '/d/terrain/' && ddForced.layout.id === 6 && ddForced.layout.source === 'debug');

  [t1, t2, t3, t4, t5, t6, t7, t8].forEach((t) => t.dispose());
  console.warn = warn;
  globalThis.fetch = prevFetch;
}

// ---- sand table (world data layer) -------------------------------------------------
{
  const W = (n) => JSON.parse(readFileSync(new URL('../static/world-data/' + n, import.meta.url), 'utf8'));
  const places = W('overmap-places.json'), mini = W('overmap-miniatures.json');
  const stones = buildStones(places, mini);
  const byId = Object.fromEntries(stones.map((s) => [s.id, s]));
  check('world: 23 locations become 23 stones', stones.length === 23);
  check('world: only the four map locations are live',
        stones.filter((s) => s.live).map((s) => s.key).sort().join() === 'arrakeen,deep-desert,hagga,harko-village');
  check('world: Hagga is one stone with 2 facets (Habbanya, Amtal), DD has 2',
        byId['hagga-basin'].facets.map((f) => f.key).join() === 'habbanya,amtal' && byId['deep-desert'].facets.length === 2);
  check('world: place -> stone keeps the game position', byId['hagga-basin'].x === places.locations.find((l) => l.id === 'hagga-basin').position.x);
  check('world: stones sorted west to east', stones.every((s, i) => i === 0 || stones[i - 1].x <= s.x));
  const p = overmapToArt4096(byId['hagga-basin'].x, byId['hagga-basin'].y);
  check('world: blueprint transform puts Hagga at (1927,463)', Math.abs(p[0] - 1927) < 2 && Math.abs(p[1] - 463) < 2);
  const pd = overmapToArt4096(byId['deep-desert'].x, byId['deep-desert'].y);
  const pa = overmapToArt4096(byId['arrakeen'].x, byId['arrakeen'].y);
  check('world: blueprint transform puts DD (1708,81) and Arrakeen (2402,444)',
        Math.abs(pd[0] - 1708) < 2 && Math.abs(pd[1] - 81) < 2 && Math.abs(pa[0] - 2402) < 2 && Math.abs(pa[1] - 444) < 2);

  const status = { available: true, maps: [
    { name: 'Hagga Basin', up: true, on_demand: false, blocked: false },
    { name: 'Deep Desert', up: false, on_demand: false, blocked: false },
    { name: 'Arrakeen', up: false, on_demand: true, blocked: false },
    { name: 'Harko Village', up: true, on_demand: true, blocked: false },
  ] };
  check('status: up', stoneStatus(byId['hagga-basin'], status) === 'up');
  check('status: a down core map is down', stoneStatus(byId['deep-desert'], status) === 'down');
  check('status: a down hub is on-demand, never down', stoneStatus(byId['arrakeen'], status) === 'on-demand');
  check('status: an up hub is up', stoneStatus(byId['harko-village'], status) === 'up');
  check('status: engraved markers carry no status', stoneStatus(byId['wind-pass'], status) === null);
  check('status: missing or failed status reads unknown', stoneStatus(byId['hagga-basin'], null) === 'unknown');

  check('bands: absent -> no band', bandFor(null, 'hagga', 'habbanya') === null);
  check('bands: 404 body / error object -> no marks',
        stoneBands(byId['hagga-basin'], { ok: false, error: 'x' }).every((b) => b.band === null));
  check('bands: numbers are never marks', bandFor({ 'hagga.habbanya': 5 }, 'hagga', 'habbanya') === null);
  check('bands: unknown enum dropped', bandFor({ 'hagga.habbanya': 'crowded' }, 'hagga', 'habbanya') === null);
  check('bands: enums map to 0-3 marks',
        bandFor({ 'hagga.habbanya': 'surging' }, 'hagga', 'habbanya').marks === 3 && bandFor({ 'hagga.habbanya': 'quiet' }, 'hagga', 'habbanya').marks === 0);
  check('bands: Amtal is two-state', bandFor({ 'hagga.amtal': 'busy' }, 'hagga', 'amtal') === null
        && bandFor({ 'hagga.amtal': 'active' }, 'hagga', 'amtal').marks === 1);
  check('bands: no Hagga aggregate is ever read', bandFor({ hagga: 'busy' }, 'hagga', 'habbanya') === null);
  check('bands: engraved markers never get a band', stoneBands(byId['wind-pass'], { 'wind-pass': 'busy' }).length === 0);
  const m = cardModel(byId['hagga-basin'], status, null);
  check('card: no bands drawn without the endpoint', m.facets.every((f) => f.band === null) && m.statusText === 'Online');
  check('card: hub down reads Available on travel', cardModel(byId['arrakeen'], status, null).statusText === 'Available on travel');

  check('keys: cycle goes west to east and wraps',
        cycle(stones, stones[0].id, 1) === stones[1].id && cycle(stones, stones[22].id, 1) === stones[0].id && cycle(stones, stones[0].id, -1) === stones[22].id);
  check('keys: cycle from nothing starts at the ends', cycle(stones, null, 1) === stones[0].id && cycle(stones, null, -1) === stones[22].id);
  check('open: Hagga facet picks the instance', mapHref('', byId['hagga-basin'], 'amtal') === '/maps/hagga?inst=amtal');
  check('open: hub has no instance, markers have no map',
        mapHref('', byId['arrakeen'], null) === '/maps/arrakeen' && mapHref('', byId['wind-pass'], null) === null);
  check('anchor: viewer map name -> stone', anchorStoneId(stones, 'Hagga Basin') === 'hagga-basin' && anchorStoneId(stones, null) === null);

  const hi = { tier: 'high', reducedMotion: false, coarse: false };
  check('view: high tier + webgl2 -> 3d', chooseView({ quality: hi, webgl2: true }) === '3d');
  check('view: reduced motion -> chart', chooseView({ quality: { ...hi, reducedMotion: true }, webgl2: true }) === 'chart');
  check('view: low tier -> chart', chooseView({ quality: { ...hi, tier: 'low' }, webgl2: true }) === 'chart');
  check('view: phone (coarse) -> chart', chooseView({ quality: { ...hi, coarse: true }, webgl2: true }) === 'chart');
  check('view: no webgl2 -> chart even when forced', chooseView({ quality: hi, webgl2: false, forced: '3d' }) === 'chart');
  const store = { v: null, getItem() { return this.v; }, setItem(k, x) { this.v = x; } };
  check('arrival: first visit plays, then not', shouldArrive({ reducedMotion: false, storage: store }) === true
        && (store.setItem('ls-world-arrived', '1'), shouldArrive({ reducedMotion: false, storage: store }) === false));
  check('arrival: reduced motion never plays', shouldArrive({ reducedMotion: true, storage: { getItem: () => null } }) === false);
  check('badge: a mapped on-demand row shows the glyph and the On travel caption',
        stoneStatus(byId['arrakeen'], status) === 'on-demand' && travelBadge(stoneStatus(byId['arrakeen'], status)).caption === 'On travel');
  check('badge: up, down and unmapped locations show neither',
        travelBadge(stoneStatus(byId['hagga-basin'], status)) === null && travelBadge(stoneStatus(byId['deep-desert'], status)) === null
        && travelBadge(stoneStatus(byId['arrakeen-spaceport'], status)) === null && travelBadge(stoneStatus(byId['the-old-quarry'], { available: true, maps: [{ name: 'The Old Quarry', up: false, on_demand: true }] })) === null);
  check('tod: hours map to presets', todPreset(6.5) === 'dawn' && todPreset(12) === 'day' && todPreset(18.5) === 'dusk'
        && todPreset(22) === 'night' && todPreset(2) === 'night' && todPreset(4.9) === 'night');
  check('tod: boundaries are 50/50 and weights always sum to 1',
        Math.abs(todWeights(8).day - 0.5) < 1e-9 && Math.abs(todWeights(8).dawn - 0.5) < 1e-9 && Math.abs(todWeights(20).night - 0.5) < 1e-9
        && [0, 4.9, 5, 5.1, 7.9, 8.2, 16.8, 17.2, 19.9, 20.2, 23.9].every((h) => Math.abs(Object.values(todWeights(h)).reduce((a, b) => a + b, 0) - 1) < 1e-9));
  check('tod: blend is about 30 minutes wide', todWeights(7.7).dawn === 1 && todWeights(8.3).day === 1 && todWeights(7.9).dawn > 0.5 && todWeights(7.9).dawn < 1);
  check('tod: night wraps midnight', todWeights(23).night === 1 && todWeights(1).night === 1 && todWeights(5).night === 0.5);
  check('tod: override wins, manual presets fix the hour, auto reads the clock',
        resolveHour('auto', 14.5) === 14.5 && todPreset(resolveHour('dusk', NaN)) === 'dusk' && todPreset(resolveHour('night', NaN)) === 'night'
        && resolveHour('auto', NaN, new Date(2026, 9, 6, 9, 30)) === 9.5);
  check('tod: night is dimmer, bluer and fiery; the blend stays between presets',
        blendLook(todWeights(23)).intensity < blendLook(todWeights(12)).intensity && blendLook(todWeights(23)).color[2] > blendLook(todWeights(23)).color[0]
        && blendLook(todWeights(23)).fire > blendLook(todWeights(12)).fire && blendLook(todWeights(8)).exposure > 0.8);
  const radii = solveRadii(stones);
  check('layout: after the solver no two discs overlap (positions untouched)', overlaps(stones, radii, 0).length === 0);
  check('layout: discs keep a gap and never drop below the floor',
        overlaps(stones, radii, DISC.gap - 1e-6).length === 0 && Object.values(radii).every((r) => r >= DISC.floor - 1e-9));
  check('layout: the solver only shrinks (never grows past the base)', stones.every((s) => radii[s.id] <= baseRadius(s) + 1e-9));
  check('layout: the Harko / Neo Carthag Arena pair separates by shrinking, Harko stays the larger',
        radii['harko-village'] > radii['neo-carthag-arena'] && radii['harko-village'] >= 0.2);
  check('layout: solving is deterministic', JSON.stringify(solveRadii(stones)) === JSON.stringify(radii));
  check('assets: world urls carry the bakeStamp', worldUrl('', 'overmap-terrace.glb', '123') === '/world-data/overmap-terrace.glb?v=123');
}

// ---- holo marker icons: atlas coverage, fallback, LOD, hit-test ---------------------
{
  const maps = Object.keys(atlas.refs);
  check('icons: atlas covers all four maps',
        ['hagga', 'deep-desert', 'arrakeen', 'harko-village'].every((k) => maps.includes(k)));
  let missing = [], badUv = [], noPng = [];
  maps.forEach((k) => atlas.refs[k].forEach((n) => {
    const r = rectFor(n);
    if (!r) { missing.push(k + ':' + n); return; }
    if (!(r[0] >= 0 && r[1] >= 0 && r[2] <= 1 && r[3] <= 1 && r[2] > r[0] && r[3] > r[1])) badUv.push(n);
  }));
  Object.keys(atlas.rects).forEach((n) => {
    if (!existsSync(new URL('../../static/img/dune-icons/' + n + '.png', import.meta.url))) noPng.push(n);
  });
  check('icons: every type_icons entry across the maps has an atlas rect' + (missing.length ? ' (missing ' + missing + ')' : ''), missing.length === 0);
  check('icons: every rect is a valid in-bounds UV box', badUv.length === 0);
  check('icons: every cell traces to a shipped in-game icon PNG', noPng.length === 0);
  check('icons: atlas image is shipped',
        existsSync(new URL('../static/' + atlas.image, import.meta.url)) && /-v\d+\.webp$/.test(atlas.image));
  const cells = Object.values(atlas.rects).concat([FALLBACK_RECT]).map((r) => r.join());
  check('icons: no two types share a cell, fallback cell is separate', new Set(cells).size === cells.length);
  check('icons: unresolved type (null/unknown) gets no rect -> category dot',
        rectFor(null) === null && rectFor(undefined) === null && rectFor('T_UI_Nope_D') === null);

  check('icons: resource layers are LOD-thinned, POIs are not',
        ['ore', 'salvage', 'flora'].every(isDenseCat) &&
        ['poi', 'hazard', 'enemy', 'other', 'district', 'vendor', 'service'].every((c) => !isDenseCat(c)));
  check('icons: camps/hazards are thinned too, but earlier than resources',
        ['enemy', 'hazard'].every(isMidCat) && lodClass('ore') === 1 && lodClass('enemy') === 2 && lodClass('poi') === 0 &&
        iconFraction(2.5, 'high', 800, MID_ZOOM_START, MID_ZOOM_FULL) > iconFraction(2.5, 'high', 16673) &&
        iconFraction(MID_ZOOM_FULL, 'high', 800, MID_ZOOM_START, MID_ZOOM_FULL) === 1);
  check('icons: zoomed out = all dots (fraction 0) up to the start zoom',
        iconFraction(1, 'high', 16673) === 0 && iconFraction(ICON_ZOOM_START, 'high', 16673) === 0);
  check('icons: fraction grows monotonically with zoom',
        [1, 2.5, 3, 4, 5, 6, 8].map((z) => iconFraction(z, 'high', 16673)).every((f, i, a) => i === 0 || f >= a[i - 1]));
  check('icons: full zoom-in shows every node as an icon (high tier)',
        iconFraction(ICON_ZOOM_FULL, 'high', 16673) === 1 && iconFraction(8, 'high', 16673) === 1);
  check('icons: low tier caps the icon count (400 x zoom^2)',
        near(iconFraction(3, 'low', 16673), ICON_BUDGET.low * 9 / 16673, 1e-9) &&
        iconFraction(3, 'low', 16673) < iconFraction(3, 'high', 16673) + 1e-12 &&
        iconFraction(ICON_ZOOM_FULL, 'low', 16673) * 16673 <= ICON_BUDGET.low * ICON_ZOOM_FULL ** 2 + 1e-6);
  check('icons: tiny hub (no dense layer) is not capped', iconFraction(8, 'low', 0) === 1);
  const ranks = Array.from({ length: 20000 }, (_, i) => rankFor(i));
  const under = ranks.filter((r) => r < 0.25).length / ranks.length;
  check('icons: node rank is stable and uniform (share under 0.25 = ' + under.toFixed(3) + ')',
        ranks.every((r) => r >= 0 && r < 1) && rankFor(123) === rankFor(123) && Math.abs(under - 0.25) < 0.02);
  check('icons: POIs draw above resource nodes', drawRank('poi') > drawRank('ore') && drawRank('vendor') > drawRank('poi'));

  // Hit-test: draw order and sprite size never change which marker a click resolves
  // (the engine matches against marker data through the relief-aware projector).
  const el = { clientWidth: 800, clientHeight: 600 };
  const mesa = (nx, ny) => 60 * Math.exp(-((nx - 500) ** 2 + (ny - 500) ** 2) / (2 * 80 * 80));
  const hc = createCamera(THREE, { view: 1000, el, heightAt: mesa });
  hc.setView({ zoom: 4, panX: 0, panY: 0, azimuth: 30, tilt: 45 });
  const catIndex = { 2: 'poi', 6: 'ore' };
  const mk = [[500, 500, 2, 0, 'Cave'], [506, 503, 6, 1], [492, 497, 6, 1], [800, 800, 6, 1]];
  let resolved = 0;
  mk.slice(0, 3).forEach((m) => {
    const s = hc.projector.project(m[0], m[1]);
    const n = hc.projector.unproject(s.screenX, s.screenY);
    const hit = n && nearestMarker(mk, catIndex, {}, n.nx, n.ny, 14 / 4 / 0.8);
    if (hit === m) resolved++;
  });
  check('icons: click on each raised marker resolves that marker (' + resolved + '/3)', resolved === 3);
  const hs = hc.projector.project(500, 500), hn = hc.projector.unproject(hs.screenX, hs.screenY);
  check('icons: a hidden type no longer wins the hit',
        nearestMarker(mk, catIndex, { 0: true }, hn.nx, hn.ny, 5) !== mk[0]);
}


// ---- holo time of day: presets, shared toggle key, sun side ------------------------------
{
  const P = (h) => lightFor('auto', h).preset;
  check('holo tod: ?hour -> preset (6 dawn, 12 day, 18 dusk, 23 night, 3 night)',
        P(6) === 'dawn' && P(12) === 'day' && P(18) === 'dusk' && P(23) === 'night' && P(3) === 'night');
  check('holo tod: manual choices pin their own hour', ['dawn', 'day', 'dusk', 'night'].every((m) => lightFor(m, NaN).preset === m));
  check('holo tod: Auto follows the viewer clock', lightFor('auto', NaN, new Date(2026, 0, 1, 18, 40)).preset === 'dusk' &&
        lightFor('auto', NaN, new Date(2026, 0, 1, 2, 0)).preset === 'night');
  check('holo tod: ?hour parses from the query string', hourOverride('?hour=18') === 18 && Number.isNaN(hourOverride('?x=1')));
  const w = lightFor('auto', 8).weights;
  check('holo tod: the 30-minute blend at a boundary mixes both presets (08:00 = half dawn, half day)',
        near(w.dawn, 0.5, 1e-6) && near(w.day, 0.5, 1e-6) && near(lightFor('auto', 8.4).weights.day, 1, 1e-9));
  const sx = (h) => holoLight(lightFor('auto', h).weights).sunDir;
  check('holo tod: dawn sun is in the east, dusk in the west, day from the south, all above the horizon',
        sx(6.5)[0] > 0.5 && sx(18.5)[0] < -0.5 && sx(12.5)[2] > 0.5 && [6.5, 12.5, 18.5, 23].every((h) => sx(h)[1] > 0));
  check('holo tod: sunDir is a unit vector with az 90 = +x (east) and az 0 = -z (north)',
        near(Math.hypot(...sunDir(123, 40)), 1, 1e-9) && near(sunDir(90, 0)[0], 1, 1e-9) && near(sunDir(0, 0)[2], -1, 1e-9));
  // cliff shading: a face looking east (normal +x) is lit at dawn, an unlit west face; dusk flips it
  const lam = (n, d) => Math.max(0, n[0] * d[0] + n[1] * d[1] + n[2] * d[2]);
  const east = [0.8, 0.6, 0], west = [-0.8, 0.6, 0];
  check('holo tod: cliffs shade from the correct side (east face lit at dawn, west face lit at dusk)',
        lam(east, sx(6.5)) > lam(west, sx(6.5)) + 0.3 && lam(west, sx(18.5)) > lam(east, sx(18.5)) + 0.3);
  const lum = (c) => c[0] * 0.3 + c[1] * 0.6 + c[2] * 0.1;
  const day = holoLight(lightFor('day', NaN).weights), night = holoLight(lightFor('night', NaN).weights);
  check('holo tod: night is dim cool moonlight (key and ambient far under day, blue over red)',
        lum(night.sun) < lum(day.sun) * 0.45 && lum(night.ambient) < lum(day.ambient) && night.sun[2] > night.sun[0] && night.ambient[2] > night.ambient[0] && night.night > 0.99 && day.night < 0.01);
  const h = (x) => holoLight(lightFor('auto', x).weights);
  check('holo tod: hologram branch only shifts a subtle tint (within 0.2 of white, bluer at night)',
        h(12).tint.every((c) => Math.abs(c - 1) < 0.2) && h(23).tint.every((c) => Math.abs(c - 1) < 0.2) && h(23).tint[2] > h(23).tint[0]);
  const mid = lerpWeights(lightFor('day', NaN).weights, lightFor('night', NaN).weights, 0.5);
  check('holo tod: eased weights stay a distribution', near(mid.day + mid.night + mid.dawn + mid.dusk, 1, 1e-9) && RELIGHT_MS === 20000);
  // shared toggle key
  const store = {};
  globalThis.localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
  check('holo tod: the toggle shares the Sand Table storage key',
        TOD_KEY === 'ls-world-tod' && readFileSync(new URL('../src/routes/world/+page.svelte', import.meta.url), 'utf8').includes("'" + TOD_KEY + "'"));
  saveMode('dusk');
  check('holo tod: a mode saved by either view reads back; junk falls back to auto',
        store[TOD_KEY] === 'dusk' && readMode() === 'dusk' && (store[TOD_KEY] = 'nope', readMode() === 'auto'));
  delete globalThis.localStorage;
}

// ---- holo clusters: formation, splitting, conservation, POIs, hit target ------------------
{
  // a seeded field of blobs: three types, dense at the centres, some stragglers
  let seed = 7; const rnd = () => (seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296;
  const items = [];
  for (let b = 0; b < 40; b++) {
    const cx = 60 + rnd() * 880, cy = 60 + rnd() * 880, type = b % 3, n = 20 + Math.floor(rnd() * 120);
    for (let i = 0; i < n; i++) items.push({ x: cx + (rnd() - 0.5) * 70, y: cy + (rnd() - 0.5) * 70, key: type, k: items.length });
  }
  for (let i = 0; i < 400; i++) items.push({ x: rnd() * 1000, y: rnd() * 1000, key: i % 3, k: items.length });
  const disp = 800;
  const zooms = [1, 1.4, 2, 2.8, 4, 5.9, 6, 8];
  let level = 0; const res = [];
  zooms.forEach((z) => { level = clusterLevel(z, disp, level); res.push({ z, level, r: buildClusters(items, level, 1000, 'high') }); });
  check('cluster: zoom 1 folds most dense nodes into a handful of clusters',
        res[0].r.clusters.length > 5 && res[0].r.clusters.length < 400 && res[0].r.singles.length < items.length * 0.5);
  check('cluster: counts are conserved at every zoom (sum of counts + singles = markers)',
        res.every(({ r }) => r.clusters.reduce((a, c) => a + c.count, 0) + r.singles.length === items.length &&
                             r.clusters.every((c) => c.count === c.ks.length && c.count >= CLUSTER_MIN)));
  check('cluster: every marker lands exactly once',
        res.every(({ r }) => new Set(r.clusters.flatMap((c) => c.ks).concat(r.singles)).size === items.length));
  check('cluster: level never drops as zoom grows, and zoom >= ' + CLUSTER_OFF_ZOOM + ' turns clustering off',
        res.every((e, i) => i === 0 || e.level >= res[i - 1].level || e.level === 0) && res.find((e) => e.z === 6).level === 0 && res.find((e) => e.z === 8).r.clusters.length === 0 &&
        res.find((e) => e.z === 8).r.singles.length === items.length);
  const live = res.filter((e) => e.level > 0);
  check('cluster: folded markers only decrease with zoom (clusters split, never merge)',
        live.every((e, i) => i === 0 || e.r.singles.length >= live[i - 1].r.singles.length));
  check('cluster: each cluster at a deeper level sits inside one cluster of the level above (nesting)',
        live.every((e, i) => i === 0 || e.level === live[i - 1].level || e.r.clusters.every((c) => {
          const parent = new Set(live[i - 1].r.clusters.flatMap((p) => p.ks));
          return c.ks.every((k) => parent.has(k));
        })));
  check('cluster: small zoom changes do not flip the level (hysteresis)',
        clusterLevel(2.02, disp, clusterLevel(2, disp, 0)) === clusterLevel(2, disp, 0) &&
        [1.9, 1.95, 2, 2.05, 2.1].every((z) => clusterLevel(z, disp, clusterLevel(2, disp, 0)) === clusterLevel(2, disp, 0)));
  check('cluster: a marker alone in its cell or type stays individual',
        buildClusters([{ x: 10, y: 10, key: 1, k: 0 }, { x: 11, y: 11, key: 2, k: 1 }, { x: 12, y: 12, key: 3, k: 2 }], 3, 1000, 'high').clusters.length === 0);
  const low = buildClusters(items, 5, 1000, 'low');
  check('cluster: the tier cap keeps the biggest, the rest stay individual, still conserved',
        low.clusters.length <= CLUSTER_CAP.low && CLUSTER_CAP.low < CLUSTER_CAP.high &&
        low.clusters.reduce((a, c) => a + c.count, 0) + low.singles.length === items.length);
  // disc size and badge
  check('cluster: disc size grows with the log of the count and is clamped',
        discPx(3) < discPx(30) && discPx(30) < discPx(300) && discPx(1) >= 24 && discPx(1e6) <= 48 &&
        near(discPx(32) - discPx(16), discPx(16) - discPx(8), 1e-9));
  check('cluster: badge reads the exact count under 1000, 1.2k style above',
        badgeGlyphs(7).text === '7' && badgeGlyphs(412).text === '412' && badgeGlyphs(1234).text === '1.2k' &&
        badgeGlyphs(2000).text === '2k' && badgeGlyphs(15800).text === '16k' && badgeGlyphs(412).glyphs.join() === '4,1,2' && badgeGlyphs(1234).glyphs.length === 4);
  // POIs never cluster, hidden layers drop out
  check('cluster: POIs never cluster (poi, vendor, service, district, hub, other); resource/camp/hazard layers do',
        ['poi', 'vendor', 'service', 'district', 'other'].every((c) => !isClusterCat(c)) &&
        ['ore', 'salvage', 'flora', 'enemy', 'hazard'].every(isClusterCat));
  const ci = { 1: 'ore', 2: 'poi', 3: 'enemy' };
  const mk = [[1, 1, 1, 0], [2, 2, 2, 5, 'Cave'], [3, 3, 3, 9], [4, 4, 1, 0]];
  check('cluster: hidden layers (by category or by type) are excluded from the visible set',
        mk.filter((m) => markerVisible(m, ci, {})).length === 4 &&
        mk.filter((m) => markerVisible(m, ci, { ore: true })).length === 2 &&
        mk.filter((m) => markerVisible(m, ci, { 9: true })).length === 3 &&
        mk.filter((m) => markerVisible(m, ci, { poi: true, ore: true, enemy: true })).length === 0);
  // click -> zoom target
  const big = res[0].r.clusters.slice().sort((a, b) => b.count - a.count)[0];
  const zt = zoomForBounds(big, 1, 1000);
  const before = clusterLevel(1, disp, 0), after = clusterLevel(zt, disp, before);
  check('cluster: click zooms at least 2x toward the cluster, within the camera limits, and it splits there',
        zt >= 2 && zt <= 8 && (after === 0 || after > before) &&
        buildClusters(items, after, 1000, 'high').clusters.every((c) => !(c.x === big.x && c.y === big.y && c.count === big.count)) &&
        zoomForBounds({ minX: 0, maxX: 1, minY: 0, maxY: 1 }, 7, 1000) === 8);
  // hit-test: the cluster disc is hit at the screen position the relief-aware projector gives it
  const el = { clientWidth: 800, clientHeight: 600 };
  const mesa = (nx, ny) => 80 * Math.exp(-((nx - 500) ** 2 + (ny - 500) ** 2) / (2 * 90 * 90));
  const cam = createCamera(THREE, { view: 1000, el, heightAt: mesa });
  cam.setView({ zoom: 2, panX: 0, panY: 0, azimuth: 40, tilt: 50 });
  const s = cam.projector.project(500, 500), back = cam.projector.unproject(s.screenX, s.screenY);
  check('cluster: the projected centre of a cluster on a raised mesa maps back onto it (relief-aware, ' + Math.hypot(back.nx - 500, back.ny - 500).toFixed(2) + ' units)',
        back && Math.hypot(back.nx - 500, back.ny - 500) < 2);
}

// ---- sand table FX: presence per location, reduced motion, tier caps, storm hook ----------
{
  const W = (n) => JSON.parse(readFileSync(new URL('../static/world-data/' + n, import.meta.url), 'utf8'));
  const fxData = W('overmap-locations-fx.json'), mini = W('overmap-miniatures.json');
  const stones = { byId: new Map() };
  for (const [id, l] of Object.entries(mini.locations)) {
    const a = l.anchorCm, ms = 12;
    stones.byId.set(id, { ms, anchor: { x: a.x * 1e-4, y: 0.4, z: a.y * 1e-4 }, cxu: a.x * 1e-4, czu: a.y * 1e-4, r: 1.2, plateY: 0.4 });
  }
  const toScene = (p) => [p[0] * 1e-2, 0.5, p[1] * 1e-2];
  const make = (o) => {
    const fx = createFx(THREE, o);
    fx.build(fxData, (id) => { const e = stones.byId.get(id); return e ? { x: e.anchor.x, y: e.anchor.y, z: e.anchor.z, s: e.ms, fxScale: 1 } : null; });
    fx.buildExtras({ fxData, stones, mini, vehicleData: W('overmap-vehicles.json') });
    fx.travel(mini.travelLines, toScene);
    return fx;
  };
  const hi = make({ hi: true, reducedMotion: false });
  const lo = make({ hi: false, reducedMotion: false });
  const rd = make({ hi: true, reducedMotion: true });
  const hn = hi.info().names;
  check('fx: darkness planes at Wind Pass and Testing Station 24', hn['darkness:wind-pass'] === 1 && hn['darkness:testing-station-no-24'] === 1);
  check('fx: primrose field instanced from the bake (3x10 + 3x8 grids = 54 flowers, one draw)', hn.primrose === 1 && hi.info().primroseInstances === 54);
  check('fx: sandbike plus three ornithopters ride the game BP_MeshFollowingSpline paths', hi.info().vehicles.filter((k) => k === 'sandbike').length === 1
        && hi.info().vehicles.filter((k) => k === 'ornithopter').length === 3);
  check('fx: ambient haze and storm sand exist on the high tier only', hn.haze === 1 && hn['storm-sand'] === 1 && !lo.info().names.haze && !lo.info().names['storm-sand']);
  check('fx: low tier caps (no haze, no ornithopters, half particles, fewer octaves)',
        FX_CAPS.low.haze === 0 && !lo.info().vehicles.includes('ornithopter') && lo.info().vehicles.includes('sandbike')
        && FX_CAPS.low.particleMul <= 0.5 && FX_CAPS.low.octaves < FX_CAPS.hi.octaves && lo.info().systems < hi.info().systems);
  check('fx: no storm until the hook is called, null and eta clear it',
        hi.info().stormOn === false && (hi.setStorm({ state: 'eta', etaMs: 1 }), hi.info().stormOn === false)
        && (hi.setStorm({ state: 'sweeping', nx: 500, ny: 400, radius: 200, headingDeg: 30 }), hi.info().stormOn === true)
        && (hi.setStorm(null), hi.info().stormOn === false));
  check('fx: storm survives a missing centre (whole-stone drift)', (hi.setStorm({ state: 'centered' }), hi.info().stormOn === true));
  const vd = W('overmap-vehicles.json');
  check('fx: vehicle data has the 4 game actors with Hermite splines and loop times',
        vd.actors.length === 4 && vd.actors.filter((a) => a.kind === 'sandbike').length === 1 && vd.actors.every((a) => a.points.length >= 5 && a.loopSec > 0));
  check('fx: darkness has a vertical veil too', !!hn['darkness-veil:wind-pass'] && !!hn['darkness-veil:testing-station-no-24']);
  const bike = (f) => f.info().vehiclePos.sandbike.join(',');
  const b0 = bike(hi), r0 = bike(rd);
  hi.tick(40); rd.tick(40);
  check('fx: vehicles move with motion on, hold still with reduced motion', bike(hi) !== b0 && bike(rd) === r0);
  rd.setStorm({ state: 'sweeping', nx: 500, ny: 400, radius: 200, headingDeg: 30 });
  check('fx: reduced motion still shows a static storm frame', rd.info().stormOn === true && rd.info().reduced === true);
  const live = (extra) => ({ sandstorm: { dimensions: { 1: Object.assign({ next_eta_utc: '2026-10-07T00:00:00Z' }, extra) } } });
  const now = Date.parse('2026-10-06T12:00:00Z');
  check('storm hook: eta only, absent feed, unavailable feed -> nothing drawn',
        stormFromLive(live({}), now) === null && stormFromLive(null, now) === null && stormFromLive({ sandstorm: { available: false, dimensions: {} } }, now) === null);
  check('storm hook: fresh scanned position = centered with geometry',
        (() => {
          const c = stormFromLive(live({ storm_sector: 'E8', storm_scanned_utc: '2026-10-06T11:55:00Z', center_nx: 788.1, center_ny: 554.8, radius_nr: 184.5, heading_yaw: 36.8 }), now);
          return c && c.state === 'centered' && c.nx === 788.1 && c.headingDeg === 36.8;
        })());
  check('storm hook: no timing-only storms (recent spawn, fresh scan without a centre, storm_state clear -> nothing)',
        stormFromLive(live({ last_spawn_utc: '2026-10-06T11:58:30Z' }), now) === null &&
        stormFromLive(live({ storm_sector: 'E8', storm_scanned_utc: '2026-10-06T11:55:00Z' }), now) === null &&
        stormFromLive(live({ storm_state: 'clear', storm_sector: 'E8', storm_scanned_utc: '2026-10-06T11:55:00Z', center_nx: 788.1, center_ny: 554.8 }), now) === null);
  check('storm hook: a stale scan (>20 min) and an old spawn fall back to nothing',
        stormFromLive(live({ storm_sector: 'E8', storm_scanned_utc: '2026-10-06T11:00:00Z', last_spawn_utc: '2026-10-06T10:00:00Z' }), now) === null);
  // Hagga: one stone, three sietches, each storming on its own; all of them draw on the stone.
  const scan = '2026-10-06T11:55:00Z';
  const hlive = { sandstorm: { dimensions: {
    0: { storm_state: 'active', storm_scanned_utc: scan, center_nx: 200, center_ny: 300, radius_nr: 120, heading_yaw: 10 },
    1: { storm_state: 'clear', storm_scanned_utc: scan, center_nx: 400, center_ny: 400 },
    2: { storm_state: 'active', storm_scanned_utc: scan, center_nx: 700, center_ny: 600, radius_nr: 150, heading_yaw: 200 },
  } } };
  const hs = stormsFromLive(hlive, now);
  check('storm hook: every centered dimension comes back, clear ones never', hs.length === 2 && hs[0].dim === '0' && hs[1].dim === '2' && hs[1].headingDeg === 200);
  check('storm hook: stormFromLive is the first centered storm', stormFromLive(hlive, now).nx === 200 && stormsFromLive(null, now).length === 0);
  check('storm stones: Deep Desert and Hagga, each on its own live map', STORM_STONES_MAP['deep-desert'] === 'deep-desert' && STORM_STONES_MAP['hagga-basin'] === 'hagga');
  const mk = createFx(THREE, { hi: true, reducedMotion: false });
  mk.build(fxData, () => null);
  mk.buildExtras({ fxData, stones, mini, vehicleData: W('overmap-vehicles.json'), stormStones: Object.keys(STORM_STONES_MAP) });
  check('fx: a storm layer per storm stone', mk.info().names.storm === 2 && mk.info().names['storm-sand'] === 2);
  mk.setStorm(hs, 'hagga-basin');
  const hm = mk.group.children.filter((o) => o.userData.fx === 'storm');
  const hu = hm.find((o) => o.visible);
  check('fx: two Hagga storms draw together on the Hagga stone only, DD stays clear',
        mk.info().stormOn === true && hm.filter((o) => o.visible).length === 1 && hu.material.uniforms.uN.value === 2
        && hu.material.uniforms.uBlob.value[0].x < 0.5 && hu.material.uniforms.uBlob.value[1].x > 0.5);
  mk.setStorm([], 'hagga-basin');
  check('fx: an empty list clears the stone', mk.info().stormOn === false && hu.visible === false);
  mk.setStorm({ state: 'centered', nx: 450, ny: 450 });
  check('fx: setStorm without a stone still means the Deep Desert', mk.info().stormOn === true && hu.visible === false);
  [hi, lo, rd, mk].forEach((f) => f.dispose());
}

// ---- storm footprint (reader v2): zones, exact route motion, hover, clears-in ----------
{
  // Shape of a real relay entry after the backend projection (DD PvE, live probe
  // 2026-10-07 17:27Z: level 2, speed 5000 cm/s, life 175.6/777.6 s, 5 zones with the
  // core duplicated), projected with the DD calibration (span 2438400, VIEW 1000).
  const V = 1000, SPAN = 2438400, O = -1270000;
  const b = (x) => (x - O) / SPAN * V, s = (cm) => cm / SPAN * V;
  const scanned = Date.parse('2026-10-07T17:27:00Z');
  const z = (name, dmg, x, y, ax, ay) => ({ name, damage: dmg, nx: b(x), ny: b(y), ax_nr: s(ax), ay_nr: s(ay), yaw: 161.4 });
  const info = {
    storm_state: 'active', storm_scanned_utc: '2026-10-07T17:27:00Z', center_nx: b(-1037696), center_ny: b(-748331),
    heading_yaw: 161.4, radius_nr: s(500000), lifetime_remaining: 175.6, lifetime_total: 777.6,
    ends_utc: '2026-10-07T17:29:55Z',
    start_nx: b(1768400), start_ny: b(-1690676), end_nx: b(-1870000), end_ny: b(-468826),
    zones: [z('core', true, -987785, -765092, 202500, 450000), z('gustfront', false, -1078648, -734578, 360000, 500000),
      z('stormwall', true, -1021770, -753679, 210000, 415000), z('core', true, -987785, -765092, 202500, 450000),
      z('top', true, -1085474, -732286, 210000, 350000)],
  };
  const fp = stormFootprint(info);
  check('footprint: duplicate core dropped, gust front first, 3 damage zones',
        fp && fp.zones.length === 4 && fp.zones[0].name === 'gustfront' && fp.zones.filter((q) => q.damage).length === 3);
  check('footprint: clear, no zones, no centre -> null (circle fallback / nothing)',
        stormFootprint({ ...info, storm_state: 'clear' }) === null && stormFootprint({ ...info, zones: [] }) === null &&
        stormFootprint({ ...info, center_nx: null }) === null && stormFootprint(null) === null);
  const p0 = positionAt(fp, scanned, false);
  check('footprint: anchored on the scanned centre at scan time (no jump on a poll)',
        Math.abs(p0.x - info.center_nx) < 1e-9 && Math.abs(p0.y - info.center_ny) < 1e-9 && p0.estimated === false);
  // Route speed: |route| / (lifetime_total - hold) = 5000 cm/s in board units.
  const p60 = positionAt(fp, scanned + 60000, false);
  const moved = Math.hypot(p60.x - p0.x, p60.y - p0.y) / V * SPAN;
  check('footprint: glides along the route at the game speed (~5000 cm/s)', Math.abs(moved / 60 - 5000) < 70);
  const head = Math.atan2(p60.y - p0.y, p60.x - p0.x) * 180 / Math.PI;
  check('footprint: glide direction = the route = heading_yaw (verified arrow convention)', Math.abs(head - 161.4) < 0.5);
  check('footprint: reduced motion stays at the latest known position',
        (() => { const r = positionAt(fp, scanned + 60000, true); return r.x === info.center_nx && r.y === info.center_ny; })());
  // Clamps at the route end and freezes after STORM_STALE_MS.
  const late = positionAt(fp, scanned + 3600000, false), stale = positionAt(fp, scanned + STORM_STALE_MS, false);
  check('footprint: clamps at the route end and freezes when stale',
        Math.abs(late.x - info.end_nx) < 0.5 && Math.abs(late.y - info.end_ny) < 0.5 && late.x === stale.x && late.estimated === true);
  // The 10 s spawn hold: a storm scanned 4 s after spawn does not move for 6 s.
  const young = stormFootprint({ ...info, lifetime_remaining: info.lifetime_total - 4 });
  const y5 = positionAt(young, scanned + 5000, false), y9 = positionAt(young, scanned + 9000, false);
  check('footprint: honours the ' + STORM_HOLD_S + ' s spawn hold',
        y5.x === young.cx && y5.y === young.cy && y9.x !== young.cx);
  check('footprint: clears-in from ends_utc, m:ss, over at the end',
        clearsInSec(fp, scanned) === 175 && fmtClears(175) === '2:55' && fmtClears(5) === '0:05' &&
        stormLabel(fp, scanned) === 'Sandstorm, clears in 2:55' && !isOver(fp, scanned) && isOver(fp, scanned + 176000) &&
        !/—/.test(stormLabel(fp, scanned)));
  check('footprint: ends_utc falls back to scan + lifetime_remaining',
        (() => { const f = stormFootprint({ ...info, ends_utc: undefined }); return clearsInSec(f, scanned) === 176; })());
  const zc = fp.zones.find((q) => q.name === 'core');
  check('footprint hover: inside the core hits, far outside misses, nothing after it clears',
        hitFootprint(fp, p0.x + zc.dx, p0.y + zc.dy, scanned, false) &&
        !hitFootprint(fp, p0.x + 300, p0.y, scanned, false) &&
        !hitFootprint(fp, p0.x + zc.dx, p0.y + zc.dy, scanned + 200000, false));
  // Ellipse orientation, on the core alone (the gust front reaches ~4 km ahead, so
  // the whole footprint would also hit there): 2.03 km along the heading, 4.5 km across.
  const core = stormFootprint({ ...info, zones: [info.zones[0]] });
  const hd = 161.4 * Math.PI / 180, ux = Math.cos(hd), uy = Math.sin(hd);
  const cx = core.cx + core.zones[0].dx, cy = core.cy + core.zones[0].dy, along = s(190000), across = s(430000);
  check('footprint hover: ellipse is narrow along the heading, wide across it',
        hitFootprint(core, cx + ux * along, cy + uy * along, scanned, false) &&
        !hitFootprint(core, cx + ux * across, cy + uy * across, scanned, false) &&
        hitFootprint(core, cx - uy * across, cy + ux * across, scanned, false) &&
        hitFootprint(fp, cx + ux * across, cy + uy * across, scanned, false));
  // Hagga (Kulon, level 0): same code, smaller footprint.
  const HS = 812800, HO = -457200, hb = (x) => (x - HO) / HS * V, hs = (cm) => cm / HS * V;
  const hz = (name, dmg, x, y, ax, ay) => ({ name, damage: dmg, nx: hb(x), ny: hb(y), ax_nr: hs(ax), ay_nr: hs(ay), yaw: -149.9 });
  const hk = stormFootprint({ storm_state: 'active', storm_scanned_utc: '2026-10-07T17:27:00Z', center_nx: hb(-125623), center_ny: hb(-47839),
    heading_yaw: -149.9, lifetime_remaining: 145.9, lifetime_total: 336.5,
    start_nx: hb(655600), start_ny: hb(404617), end_nx: hb(-757200), end_ny: hb(-413625),
    zones: [hz('core', true, -95250, -30248, 135000, 270000), hz('gustfront', false, -150545, -62273, 240000, 300000)] });
  const h60 = positionAt(hk, scanned + 60000, false);
  check('footprint: Hagga storm glides at the same 5000 cm/s on the Hagga calibration',
        Math.abs(Math.hypot(h60.x - hk.cx, h60.y - hk.cy) / V * HS / 60 - 5000) < 70);
  // The REAL /live entry captured read-only on <web-host> 2026-10-07 19:56Z (Kulon, level 0,
  // entering from the east edge): the model must agree with the relay's own numbers.
  const real = stormFootprint({"storm_state":"active","storm_scanned_utc":"2026-10-07T19:55:57+00:00","center_nx":1341.5,"center_ny":-87.1,"heading_yaw":169.6,"radius_nr":615.2,"start_nx":1369.1,"start_ny":-92.1,"end_nx":-369.1,"end_ny":225.5,"lifetime_remaining":282.6,"lifetime_total":297.2,"ends_utc":"2026-10-07T20:00:39+00:00","level":0,"move_speed":5000.0,"zones":[{"name":"core","damage":true,"nx":1384.0,"ny":-94.9,"ax_nr":166.1,"ay_nr":332.2,"yaw":169.6},{"name":"gustfront","damage":false,"nx":1306.7,"ny":-80.7,"ax_nr":295.3,"ay_nr":369.1,"yaw":169.6},{"name":"stormwall","damage":true,"nx":1355.1,"ny":-89.6,"ax_nr":172.2,"ay_nr":306.3,"yaw":169.6},{"name":"top","damage":true,"nx":1300.8,"ny":-79.7,"ax_nr":172.2,"ay_nr":258.4,"yaw":169.6}]});
  const rt = Date.parse('2026-10-07T19:55:57+00:00');
  const rp = positionAt(real, rt, false), r60 = positionAt(real, rt + 60000, false), rEnd = positionAt(real, rt + 282600, false);
  check('footprint (real 19:56Z payload): 4 zones, route speed = 5000 cm/s, glide along heading, ends at the route end',
        real.zones.length === 4 && Math.abs(real.motion.speed * 812800 / 1000 - 5000) < 5 &&
        Math.abs(Math.atan2(r60.y - rp.y, r60.x - rp.x) * 180 / Math.PI - 169.6) < 0.1 &&
        Math.hypot(rEnd.x - real.route.ex, rEnd.y - real.route.ey) < 6 && stormLabel(real, rt) === 'Sandstorm, clears in 4:42');
  // reckon.js follow mode: samples the position function; reduced motion places once.
  const seen = [];
  const rk = createReckoner({ reducedMotion: true });
  rk.followFix('storm', { position: () => ({ x: 1, y: 2, estimated: false }), untilMs: Date.now() + 1e6,
    apply: (x, y, e) => seen.push([x, y, e]) });
  check('reckon follow: reduced motion applies exactly once', seen.length === 1 && seen[0][0] === 1 && seen[0][2] === false);
  rk.destroy();
}

// ---- result -----------------------------------------------------------------------
if (failures) {
  console.error('\nsmoke: ' + failures + ' failure(s)');
  process.exit(1);
}
console.log('\nsmoke: all checks passed');
