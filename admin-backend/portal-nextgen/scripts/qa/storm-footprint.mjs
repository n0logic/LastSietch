// Reader v2 storm footprint replay (2.26.2). Replays /portal/maps/{deep-desert,hagga}/live
// with the storms the live probe read on 2026-10-07 17:27Z (~/dd-re/storm-probe-20261007T1727Z.txt),
// shaped exactly like the relay entry after the backend projection (map_model calibration,
// VIEW 1000), re-timed so each storm is mid-life now. Checks the holo + board draw the zone
// ellipses (no circle), the dashed route, the glide along the route, reduced motion, the
// hover label, a cleared/expired storm drawing nothing; screenshots both maps, day + night.
// Usage: QA_OUT=<dir> PW_DIR=<dir> [QA_BASE=http://127.0.0.1:4175] node storm-footprint.mjs
import { writeFileSync, mkdirSync, readFileSync } from 'node:fs';
import { launch, open, toHolo as toHolo1, BASE } from './lib.mjs';

const OUT = process.env.QA_OUT || '.';
mkdirSync(OUT, { recursive: true });

const pressed = (page, name) => page.evaluate((n) => [...document.querySelectorAll('.viewer-btn')]
  .some((b) => b.textContent.trim().toLowerCase() === n && b.getAttribute('aria-pressed') === 'true'), name);
async function toBoard(page) {
  // the page promotes itself to the holo once WebGL is up; switch only after that settles
  for (let i = 0; i < 20 && !(await pressed(page, 'holo table')); i++) await page.waitForTimeout(500);
  for (let i = 0; i < 3; i++) {
    await page.getByRole('button', { name: /^board$/i }).first().click();
    await page.waitForTimeout(2500);
    if ((await pressed(page, 'board')) && !(await page.evaluate(() => !!document.querySelector('.holo-canvas.live')))) return;
  }
}
async function toHolo(page, ms) {
  for (let i = 0; i < 3; i++) {
    if ((await pressed(page, 'holo table')) && (await page.evaluate(() => !!document.querySelector('.holo-canvas.live')))) return;
    if (!(await pressed(page, 'holo table'))) await toHolo1(page, ms);
    else await page.waitForTimeout(ms / 3);
  }
}

// ---- payloads ----------------------------------------------------------------------------
const VIEW = 1000;
const CAL = { 'deep-desert': { o: -1270000, span: 2438400 }, hagga: { o: -457200, span: 812800 } };
const PROBE = {
  dd0: { center: [-1037696, -748331], yaw: 161.4, life: [175.6, 777.6], path: [[1768400, -1690676], [-1870000, -468826]],
    zones: [['core', 1, -987785, -765092, 202500, 450000], ['gustfront', 0, -1078648, -734578, 360000, 500000],
      ['stormwall', 1, -1021770, -753679, 210000, 415000], ['core', 1, -987785, -765092, 202500, 450000],
      ['top', 1, -1085474, -732286, 210000, 350000]] },
  dd1: { center: [848003, 299248], yaw: -156.1, life: [594.6, 806.0], path: [[1768400, 707474], [-1870000, -906275]],
    zones: [['core', 1, 896132, 320594, 202500, 450000], ['gustfront', 0, 808513, 281733, 360000, 500000],
      ['stormwall', 1, 863360, 306059, 210000, 415000], ['core', 1, 896132, 320594, 202500, 450000],
      ['top', 1, 801931, 278814, 210000, 350000]] },
  // Captured on Kulon (dim 1) before it closed 2026-10-28; replayed on Amtal (dim 2).
  kulon: { center: [-125623, -47839], yaw: -149.9, life: [145.9, 336.5], path: [[655600, 404617], [-757200, -413625]],
    zones: [['core', 1, -95250, -30248, 135000, 270000], ['gustfront', 0, -150545, -62273, 240000, 300000],
      ['stormwall', 1, -115931, -42226, 140000, 249000], ['core', 1, -95250, -30248, 135000, 270000],
      ['top', 1, -154699, -64678, 140000, 210000]] },
};
const iso = (ms) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, '+00:00');

// One relay dim entry as /live serves it (reader v2 relay + backend _sandstorm_overlay).
function entry(map, s, o = {}) {
  const c = CAL[map], px = (x) => Math.round((x - c.o) / c.span * VIEW * 10) / 10, sc = (v) => Math.round(v / c.span * VIEW * 10) / 10;
  const now = Date.now(), scanned = now - (o.scanAgoS || 20) * 1000;
  // remaining at scan: probe value, unless the case wants the storm already over
  const left = o.over ? 5 : s.life[0] + (o.scanAgoS || 20);
  return {
    label: 'x', last_spawn_utc: iso(scanned - (s.life[1] - left) * 1000), next_eta_utc: iso(now + 3000000),
    mean_interval_min: 60, confidence: 0.9, samples: 30,
    storm_state: 'active', center_x: s.center[0], center_y: s.center[1], radius: 500000, heading_yaw: s.yaw,
    stage: 2, storm_sector: map === 'hagga' ? '' : 'H1', storm_scanned_utc: iso(scanned),
    start_x: s.path[0][0], start_y: s.path[0][1], end_x: s.path[1][0], end_y: s.path[1][1],
    level: map === 'hagga' ? 0 : 2, scale: [1, 1, 1], move_speed: 5000,
    lifetime_remaining: left, lifetime_total: s.life[1], ends_utc: iso(scanned + left * 1000),
    storm_active_since_utc: iso(scanned - 60000),
    center_nx: px(s.center[0]), center_ny: px(s.center[1]), radius_nr: sc(500000),
    start_nx: px(s.path[0][0]), start_ny: px(s.path[0][1]), end_nx: px(s.path[1][0]), end_ny: px(s.path[1][1]),
    zones: s.zones.map(([name, dmg, x, y, ax, ay]) => ({ name, damage: !!dmg, nx: px(x), ny: px(y), ax_nr: sc(ax), ay_nr: sc(ay), yaw: s.yaw })),
  };
}
function live(dims) {
  return JSON.stringify({ spice: { dimensions: {} }, worms: { dimensions: {} }, sandstorm: { available: true, generated_utc: iso(Date.now()), dimensions: dims } });
}
// The PvP storm moved (with its route) so its centre sits mid-board: close-ups zoom on it.
function centred(p, map, at) {
  const c = CAL[map], tx = at[0] - p.center[0], ty = at[1] - p.center[1];
  return { ...p, center: [p.center[0] + tx, p.center[1] + ty], path: p.path.map(([x, y]) => [x + tx, y + ty]),
    zones: p.zones.map(([n, d, x, y, ax, ay]) => [n, d, x + tx, y + ty, ax, ay]) };
}
const DD_MID = () => live({ 1: entry('deep-desert', centred(PROBE.dd1, 'deep-desert', [-50800, -50800])) });
const HAGGA_MID = () => live({ 2: entry('hagga', centred(PROBE.kulon, 'hagga', [-50800, -50800])) });
const DD = () => live({ 0: entry('deep-desert', PROBE.dd0), 1: entry('deep-desert', PROBE.dd1) });
// Dim 1 (closed Kulon) carries a stray storm on purpose: no tab may draw it.
const HAGGA = () => live({ 0: { storm_state: 'clear', storm_scanned_utc: iso(Date.now() - 20000) }, 1: entry('hagga', PROBE.kulon), 2: entry('hagga', PROBE.kulon) });

// ---- harness -----------------------------------------------------------------------------
const R = { fail: [], shots: [] };
const want = (ok, msg) => { if (!ok) R.fail.push(msg); };
const b = await launch();

async function page(map, query, body, o = {}) {
  const { page: pg, ctx, log } = await open(b, '/', { reducedMotion: o.reduced ? 'reduce' : 'no-preference' });
  // One payload per page: every poll re-serves the SAME scan, as the relay does between
  // reader ticks (a re-timed fixture per poll would re-anchor the storm and fake a jump).
  const fixed = typeof body === 'function' ? body() : body;
  await pg.route(`**/portal/maps/${map}/live*`, (r) => r.fulfill({ status: 200, contentType: 'application/json', body: fixed }));
  await pg.goto(`${BASE}/maps/${map}${query}`, { waitUntil: 'load' });
  await pg.waitForTimeout(3500);
  if (o.board) await toBoard(pg);
  return { pg, ctx, log };
}
const holoStorm = (pg) => pg.evaluate(() => document.querySelector('.holo-canvas')?.__holoQa?.storm?.() || null);
const board = async (pg, expect = true) => {
  // the storm layer lands with the board's first /live poll; poll redraws rebuild it
  if (expect) await pg.waitForSelector('.map-storm-track[transform]', { timeout: 15000 }).catch(() => {});
  return boardNow(pg);
};
const boardNow = (pg) => pg.evaluate(() => {
  const l = document.querySelector('.map-storm-layer');
  const t = document.querySelector('.map-storm-track');
  return { layer: !!l, footprint: !!document.querySelector('.map-storm-layer--footprint'),
    zones: document.querySelectorAll('.map-storm-zone').length, damage: document.querySelectorAll('.map-storm-zone--damage').length,
    gust: document.querySelectorAll('.map-storm-zone--gust').length, circle: !!document.querySelector('.map-storm-circle'),
    route: !!document.querySelector('.map-storm-route'), transform: t ? t.getAttribute('transform') : null,
    estimated: t ? t.classList.contains('is-extrapolated') : null };
});
const shot = async (pg, name) => { await pg.mouse.move(1590, 990); await pg.waitForTimeout(400); await pg.screenshot({ path: `${OUT}/${name}.png` }); R.shots.push(`${OUT}/${name}.png`); };
const tr = (t) => (t || '').match(/translate\(([-\d.]+),([-\d.]+)\)/)?.slice(1).map(Number) || null;

const ONLY = process.env.QA_ONLY || '';
const VIS_MIN = { frac: Number(process.env.QA_VIS_FRAC || 0.0045), mean: Number(process.env.QA_VIS_MEAN || 40) };
if (ONLY !== 'vis') {
// 1. DD board (PvP, mid-board storm): footprint, no circle, route, glide, hover.
{
  const { pg, ctx, log } = await page('deep-desert', '?inst=pvp&hour=12', DD, { board: true });
  const a = await board(pg);
  R.ddBoard = a;
  want(a.footprint && a.zones === 4 && a.damage === 3 && a.gust === 1 && !a.circle && a.route, 'dd board: 4 deduped zone ellipses + route, no circle');
  await shot(pg, 'dd-board-day');
  await pg.waitForTimeout(4000);
  const b2 = await board(pg);
  const p1 = tr(a.transform), p2 = tr(b2.transform);
  const d = p1 && p2 ? [p2[0] - p1[0], p2[1] - p1[1]] : null;
  R.ddBoardGlide = { from: p1, to: p2 };
  want(d && Math.hypot(d[0], d[1]) > 0.2 && Math.abs(Math.atan2(d[1], d[0]) * 180 / Math.PI - (-156.1)) < 3,
    'dd board: footprint glides along the route heading between polls');
  want(b2.estimated === false, 'dd board: a fresh exact glide is not marked as an estimate');
  // hover inside the damage core: markers inside the storm win by design, so sample a
  // grid over the core's middle until a point lands on bare storm
  const r0 = await pg.evaluate(() => { const e = document.querySelector('.map-storm-zone--damage'); const r = e.getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; });
  let label = null;
  hover: for (let i = 1; i < 6; i++) {
    for (let j = 1; j < 6; j++) {
      await pg.mouse.move(r0[0] + r0[2] * (0.25 + i * 0.1), r0[1] + r0[3] * (0.25 + j * 0.1)); await pg.waitForTimeout(150);
      const t = await pg.evaluate(() => document.querySelector('.tooltip')?.textContent?.trim() || null);
      if (t && /^Sandstorm/.test(t)) { label = t; break hover; }
    }
  }
  R.ddHover = label;
  want(!!label && /^Sandstorm, clears in \d+:\d\d$/.test(label), 'dd hover: "Sandstorm, clears in m:ss" inside the footprint');
  if (label) await pg.screenshot({ path: `${OUT}/dd-board-hover.png` }), R.shots.push(`${OUT}/dd-board-hover.png`);
  R.ddBoardErrors = log.errors;
  await ctx.close();
}

// 2. DD holo day / night / close-up, and the west-edge PvE storm (render-and-clip).
for (const [q, name] of [['?inst=pvp&hour=12', 'dd-holo-day'], ['?inst=pvp&hour=23', 'dd-holo-night'], ['?inst=pve&hour=12', 'dd-holo-pve-edge']]) {
  const { pg, ctx, log } = await page('deep-desert', q, DD);
  await toHolo(pg, 15000);
  await pg.waitForFunction(() => document.querySelector('.holo-canvas')?.__holoQa?.storm, null, { timeout: 60000 });
  await pg.waitForTimeout(2500);
  const s1 = await holoStorm(pg);
  R[name] = s1;
  want(s1 && s1.visible && s1.mode === 2 && s1.zones === 4 && s1.route, `${name}: holo footprint mode (2) with 4 zones + route`);
  await shot(pg, name);
  if (name === 'dd-holo-day') {
    await pg.waitForTimeout(4000);
    const s2 = await holoStorm(pg);
    const d = [s2.pos[0] - s1.pos[0], s2.pos[1] - s1.pos[1]];
    R.ddHoloGlide = { from: s1.pos, to: s2.pos };
    want(Math.hypot(d[0], d[1]) > 0.2 && Math.abs(Math.atan2(d[1], d[0]) * 180 / Math.PI - (-156.1)) < 3, 'dd holo: footprint glides along the route');
  }
  R[name + '-errors'] = log.errors;
  await ctx.close();
}

// 2b. Close-ups (storm centred so the zoom lands on it): DD day/night, Hagga day, board.
// Hagga storms cross the 8 km frame at ~6 board units/s, gone from a centred zoom within
// the ~50 s the holo takes to mount: its close-ups are shot under reduced motion (static).
for (const [map, q, body, name, holo, reduced] of [['deep-desert', '?inst=pvp&hour=12', DD_MID, 'dd-holo-close-day', 1, 0],
  ['deep-desert', '?inst=pvp&hour=23', DD_MID, 'dd-holo-close-night', 1, 0], ['hagga', '?inst=amtal&hour=12', HAGGA_MID, 'hagga-holo-close-day', 1, 1],
  ['hagga', '?inst=amtal&hour=23', HAGGA_MID, 'hagga-holo-close-night', 1, 1],
  ['deep-desert', '?inst=pvp&hour=12', DD_MID, 'dd-board-close', 0, 0], ['hagga', '?inst=amtal&hour=12', HAGGA_MID, 'hagga-board-close', 0, 1]]) {
  const { pg, ctx } = await page(map, q, body, { board: !holo, reduced: !!reduced });
  if (holo) {
    await toHolo(pg, 15000);
    await pg.waitForFunction(() => document.querySelector('.holo-canvas')?.__holoQa?.storm, null, { timeout: 60000 });
  } else await board(pg);
  for (let i = 0; i < 3; i++) { await pg.evaluate(() => document.querySelector('[aria-label="Zoom in"]')?.click()); await pg.waitForTimeout(700); }
  await pg.waitForTimeout(2500);
  await shot(pg, name);
  await ctx.close();
}

// 3. Reduced motion: static footprint at the latest known position (holo + board).
{
  const { pg, ctx } = await page('deep-desert', '?inst=pvp&hour=12', DD, { reduced: true, board: true });
  const a1 = await board(pg);
  await pg.waitForTimeout(3500);
  const a2 = await board(pg);
  R.reducedBoard = [a1.transform, a2.transform];
  want(a1.footprint && a1.transform === a2.transform, 'reduced motion: board footprint is static');
  await toHolo(pg, 15000);
  await pg.waitForFunction(() => document.querySelector('.holo-canvas')?.__holoQa?.storm, null, { timeout: 60000 });
  await pg.waitForTimeout(2000);
  const h1 = await holoStorm(pg);
  await pg.waitForTimeout(3500);
  const h2 = await holoStorm(pg);
  R.reducedHolo = [h1 && h1.pos, h2 && h2.pos];
  want(h1 && h1.mode === 2 && h1.pos[0] === h2.pos[0] && h1.pos[1] === h2.pos[1], 'reduced motion: holo footprint is static');
  await shot(pg, 'dd-holo-reduced');
  await ctx.close();
}

// 4. Hagga (Amtal storm on the Amtal tab; clear sietches draw nothing): board + holo day/night.
{
  const { pg, ctx, log } = await page('hagga', '?inst=amtal&hour=12', HAGGA_MID, { board: true });
  const a = await board(pg);
  R.haggaBoard = a;
  want(a.footprint && a.zones === 4 && !a.circle && a.route, 'hagga board: Amtal footprint, no circle');
  await shot(pg, 'hagga-board-day');
  await toHolo(pg, 15000);
  await pg.waitForFunction(() => document.querySelector('.holo-canvas')?.__holoQa?.storm, null, { timeout: 60000 });
  await pg.waitForTimeout(2500);
  R.haggaHolo = await holoStorm(pg);
  want(R.haggaHolo && R.haggaHolo.mode === 2 && R.haggaHolo.zones === 4, 'hagga holo: footprint mode with 4 zones');
  await shot(pg, 'hagga-holo-day');
  R.haggaErrors = log.errors;
  await ctx.close();
}
{
  const { pg, ctx } = await page('hagga', '?inst=amtal&hour=23', HAGGA_MID);
  await toHolo(pg, 15000);
  await pg.waitForFunction(() => document.querySelector('.holo-canvas')?.__holoQa?.storm, null, { timeout: 60000 });
  await pg.waitForTimeout(2500);
  await shot(pg, 'hagga-holo-night');
  await ctx.close();
}
{
  // Habbanya tab: its world is clear -> nothing, even though Amtal and the closed Kulon dim have storms.
  const { pg, ctx } = await page('hagga', '?inst=habbanya&hour=12', HAGGA, { board: true });
  await pg.waitForTimeout(4000);
  const a = await boardNow(pg);
  R.haggaHabbanya = a;
  want(!a.layer, 'hagga: an Amtal or stray dim 1 storm never draws on the Habbanya tab (clear)');
  await ctx.close();
}

// 5. A storm whose lifetime already ran out before the next poll draws nothing.
{
  const over = live({ 1: entry('deep-desert', PROBE.dd1, { over: true, scanAgoS: 40 }) });
  const { pg, ctx } = await page('deep-desert', '?inst=pvp&hour=12', over, { board: true });
  await pg.waitForTimeout(4000);
  const a = await boardNow(pg);
  await toHolo(pg, 15000);
  await pg.waitForFunction(() => document.querySelector('.holo-canvas')?.__holoQa?.storm, null, { timeout: 60000 });
  await pg.waitForTimeout(2000);
  const h = await holoStorm(pg);
  R.expired = { board: a.layer, holo: h && h.visible };
  want(!a.layer && h && !h.visible, 'expired storm (ends_utc passed): nothing on board or holo');
  await ctx.close();
}

// 6. No zones (pre-v2 relay): the v1 circle fallback still works.
{
  const e = entry('deep-desert', PROBE.dd1); delete e.zones;
  const { pg, ctx } = await page('deep-desert', '?inst=pvp&hour=12', live({ 1: e }), { board: true });
  const a = await board(pg);
  await toHolo(pg, 15000);
  await pg.waitForFunction(() => document.querySelector('.holo-canvas')?.__holoQa?.storm, null, { timeout: 60000 });
  await pg.waitForTimeout(2000);
  const h = await holoStorm(pg);
  R.noZones = { circle: a.circle, footprint: a.footprint, holoMode: h && h.mode };
  want(a.circle && !a.footprint && h && h.mode === 1, 'no zones: v1 circle on board and holo');
  await ctx.close();
}

// 8. The REAL /live entry captured on <web-host> 2026-10-07 19:56Z (Kulon, entering from the
// east edge; replayed on Amtal's dim 2 since Kulon closed), re-timed only (scanned 100 s ago, so the glide has carried it onto the board).
{
  const real = JSON.parse(readFileSync(new URL('./storm-real-20261007T195557Z.json', import.meta.url)))['1'];
  const shift = Date.now() - 100000 - Date.parse(real.storm_scanned_utc);
  const sh = (t) => iso(Date.parse(t) + shift);
  const e = { ...real, storm_scanned_utc: sh(real.storm_scanned_utc), ends_utc: sh(real.ends_utc) };
  const body = live({ 0: { storm_state: 'clear' }, 2: e });
  const { pg, ctx } = await page('hagga', '?inst=amtal&hour=12', body, { board: true });
  const a = await board(pg);
  R.realBoard = { footprint: a.footprint, zones: a.zones, circle: a.circle, route: a.route };
  want(a.footprint && a.zones === 4 && !a.circle && a.route, 'real 19:56Z payload: board footprint with 4 zones + route');
  await shot(pg, 'hagga-real-live-board');
  await toHolo(pg, 15000);
  await pg.waitForFunction(() => document.querySelector('.holo-canvas')?.__holoQa?.storm, null, { timeout: 60000 });
  await pg.waitForTimeout(2000);
  R.realHolo = await holoStorm(pg);
  want(R.realHolo && R.realHolo.mode === 2 && R.realHolo.zones === 4, 'real 19:56Z payload: holo footprint');
  await shot(pg, 'hagga-real-live-holo');
  await ctx.close();
}
}   // end of the non-visibility sections

// 7. Visibility guard (owner 2.26.2: the footprint must not regress to invisible). Thresholds
// mutation-checked 2026-10-07: a faint variant (alpha 0.18, thin pale edge) fails all four
// cases (DD on the changed share, Hagga on the mean); the real look passes all four. At the
// default zoom, reduced motion (static frame, no lightning), the holo canvas with the storm
// vs the same view with the world clear: share of pixels that changed by > 60 (sum of
// |dRGB|) and their mean change, decoded in-page on a 2D canvas (no image deps).
async function holoShot(map, q, body) {
  const { pg, ctx } = await page(map, q, body, { reduced: true });
  await toHolo(pg, 15000);
  await pg.waitForFunction(() => document.querySelector('.holo-canvas')?.__holoQa?.storm, null, { timeout: 60000 });
  await pg.waitForTimeout(3500);
  await pg.mouse.move(1590, 990);
  await pg.waitForTimeout(500);
  const box = await pg.evaluate(() => { const r = document.querySelector('.holo-canvas').getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height }; });
  const buf = await pg.screenshot({ clip: box });
  return { buf, pg, ctx };
}
const CLEAR = (map) => live(map === 'hagga'
  ? { 0: { storm_state: 'clear' }, 2: { storm_state: 'clear' } }
  : { 0: { storm_state: 'clear' }, 1: { storm_state: 'clear' } });
R.visibility = {};
for (const [map, inst, body] of [['deep-desert', 'pvp', DD_MID], ['hagga', 'amtal', HAGGA_MID]]) {
  for (const hour of [12, 23]) {
    const q = `?inst=${inst}&hour=${hour}`;
    const s1 = await holoShot(map, q, body);
    const s0 = await holoShot(map, q, CLEAR(map));
    const m = await s1.pg.evaluate(async ([a, c]) => {
      const load = (src) => new Promise((ok) => { const i = new Image(); i.onload = () => ok(i); i.src = src; });
      const [ia, ic] = await Promise.all([load(a), load(c)]);
      const w = Math.min(ia.width, ic.width), h = Math.min(ia.height, ic.height);
      const px = (img) => { const cv = document.createElement('canvas'); cv.width = w; cv.height = h; const g = cv.getContext('2d'); g.drawImage(img, 0, 0); return g.getImageData(0, 0, w, h).data; };
      const A = px(ia), C = px(ic);
      let n = 0, sum = 0;
      for (let i = 0; i < A.length; i += 4) {
        const d = Math.abs(A[i] - C[i]) + Math.abs(A[i + 1] - C[i + 1]) + Math.abs(A[i + 2] - C[i + 2]);
        if (d > 60) { n++; sum += d / 3; }
      }
      return { frac: n / (w * h), mean: n ? sum / n : 0 };
    }, ['data:image/png;base64,' + s1.buf.toString('base64'), 'data:image/png;base64,' + s0.buf.toString('base64')]);
    const key = `${map}-${hour === 12 ? 'day' : 'night'}`;
    R.visibility[key] = { frac: +m.frac.toFixed(4), mean: +m.mean.toFixed(1) };
    writeFileSync(`${OUT}/vis-${key}-storm.png`, s1.buf); writeFileSync(`${OUT}/vis-${key}-clear.png`, s0.buf);
    want(m.frac >= VIS_MIN.frac && m.mean >= VIS_MIN.mean,
      `visibility ${key}: footprint changed ${(m.frac * 100).toFixed(2)}% of the holo at mean ${m.mean.toFixed(0)} (min ${VIS_MIN.frac * 100}% / ${VIS_MIN.mean})`);
    await s1.ctx.close(); await s0.ctx.close();
  }
}

await b.close();
const errs = Object.entries(R).filter(([k, v]) => /errors/i.test(k) && Array.isArray(v) && v.length);
if (errs.length) R.fail.push('page errors: ' + JSON.stringify(errs));
R.pass = R.fail.length === 0;
writeFileSync(`${OUT}/_storm-footprint.json`, JSON.stringify(R, null, 1));
console.log(JSON.stringify(R, null, 1));
process.exit(R.pass ? 0 : 1);
