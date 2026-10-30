// Deep Desert spice replay: feeds captured /data + /live payloads (the 2.26.0 live
// bug: 85 mediums, 5 candidates, active Large at I5) into the local review preview
// and checks the holo spice layer draws every field where the 2D board does, above
// the terrain, and visibly (pixel diff with the spice layers on vs off).
// Usage: SPICE_DIR=~/dd-re/spice-fix QA_OUT=<dir> PW_DIR=<dir> node spice-replay.mjs [tag]
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { launch, open, toHolo } from './lib.mjs';
const DIR = process.env.SPICE_DIR || process.env.HOME + '/dd-re/spice-fix';
const OUT = process.env.QA_OUT || DIR;
const tag = process.argv[2] || 'run';
mkdirSync(OUT, { recursive: true });
const dataTxt = readFileSync(DIR + '/dd_data.json', 'utf8');
const liveTxt = readFileSync(DIR + '/dd_live.json', 'utf8');
const data = JSON.parse(dataTxt);
const live = JSON.parse(liveTxt);

const R = { tag, fail: [] };
const fail = (m) => { R.fail.push(m); };

async function boot(b, o) {
  const s = await open(b, '/', o);
  const { page } = s;
  await page.route('**/portal/maps/deep-desert/data*', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: dataTxt }));
  await page.route('**/portal/maps/deep-desert/live*', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: o.liveBody || liveTxt }));
  // every layer on (mediums default OFF on DD; the owner had them on)
  await page.evaluate(() => { localStorage.setItem('map_layers_deep-desert', '{}'); localStorage.removeItem('ls-world-tod'); });
  await page.goto('http://127.0.0.1:4173/maps/deep-desert?inst=pvp' + (o.hour != null ? '&hour=' + o.hour : ''), { waitUntil: 'load' });
  await page.waitForTimeout(3000);
  if (!(await page.evaluate(() => !!document.querySelector('.holo-canvas.live')))) await toHolo(page, 16000);
  await page.waitForFunction(() => { const c = document.querySelector('.holo-canvas'); return c && c.__holoQa && c.__holoQa.spice().specs > 0; }, null, { timeout: 60000 });
  await page.waitForTimeout(6000);   // real layout raster + drape
  await page.mouse.move(1500, 80);
  await page.waitForTimeout(800);
  return s;
}

const info = (page) => page.evaluate(() => {
  const qa = document.querySelector('.holo-canvas').__holoQa;
  const s = qa.spice();
  const box = document.querySelector('.holo-canvas').getBoundingClientRect();
  s.box = { x: box.x, y: box.y, w: box.width, h: box.height };
  s.fields.forEach((f) => {
    const p = qa.project(f.nx, f.ny);
    f.sx = p.screenX; f.sy = p.screenY;
    // tallest relief within ~one column width: the ridge that could bury it
    let top = -1e9;
    for (let dy = -8; dy <= 8; dy += 2) for (let dx = -8; dx <= 8; dx += 2) top = Math.max(top, qa.heightAt(f.nx + dx, f.ny + dy));
    f.ridge = top - f.ground;
  });
  return s;
});

// Pixel diff between two screenshots, sampled in a window around each field.
async function diffAt(page, a, b, pts, rad) {
  return page.evaluate(async ({ a, b, pts, rad }) => {
    async function px(b64) {
      const bm = await createImageBitmap(await (await fetch('data:image/png;base64,' + b64)).blob());
      const c = new OffscreenCanvas(bm.width, bm.height); const x = c.getContext('2d'); x.drawImage(bm, 0, 0);
      return x.getImageData(0, 0, bm.width, bm.height);
    }
    const A = await px(a), B = await px(b);
    return pts.map((p) => {
      let n = 0;
      for (let y = Math.round(p.y - rad); y <= p.y + rad; y++) for (let x = Math.round(p.x - rad); x <= p.x + rad; x++) {
        if (x < 0 || y < 0 || x >= A.width || y >= A.height) continue;
        const i = (y * A.width + x) * 4;
        const d = Math.abs(A.data[i] - B.data[i]) + Math.abs(A.data[i + 1] - B.data[i + 1]) + Math.abs(A.data[i + 2] - B.data[i + 2]);
        if (d > 40) n++;
      }
      return n;
    });
  }, { a: a.toString('base64'), b: b.toString('base64'), pts, rad });
}

const toggle = async (page, name) => { await page.getByRole('button', { name }).first().click(); await page.waitForTimeout(1200); };

const b = await launch();

// 1) static pass (reduced motion = no pulse/plume drift, so the diff is the layer alone)
{
  const { page, ctx, log } = await boot(b, { reducedMotion: 'reduce', hour: 12 });
  const s = await info(page);
  // PvP (dim 1) only: its own medium sites and blows, not the /data PvE+PvP union
  const pvpMeds = live.spice.dimensions['1'].ram_mediums;
  // a candidate that is blowing now is drawn once, as the active Large
  const actF = live.spice.dimensions['1'].ram_active_fields;
  const idleCand = Object.values(data.spice_candidate_coords).filter(([x, y]) => !actF.some((f) => Math.abs(f.nx - x) < 1 && Math.abs(f.ny - y) < 1));
  const want = { candidate: idleCand.length, medium: pvpMeds.filter((m) => !m.active).length,
    erupting: pvpMeds.filter((m) => m.active).length, active: live.spice.dimensions['1'].ram_active_fields.length };
  const got = {}; s.fields.forEach((f) => { got[f.kind] = (got[f.kind] || 0) + 1; });
  R.counts = { want, got, specs: s.specs, pips: s.pips, patches: s.patches, bubbles: s.bubbles, blows: s.blows };
  for (const k of Object.keys(want)) if (got[k] !== want[k]) fail(`kind ${k}: ${got[k] || 0} specs, want ${want[k]}`);
  if (s.pips !== s.specs) fail(`drew ${s.pips} of ${s.specs} spice pips (truncated)`);
  if (s.patches !== s.specs) fail(`drew ${s.patches} of ${s.specs} field patches (truncated)`);
  if (s.blows.large !== want.active) fail(`${s.blows.large} large blows, want ${want.active}`);
  if (s.blows.medium !== want.erupting) fail(`${s.blows.medium} medium blows, want ${want.erupting}`);
  if (!(s.bubbles > 0)) fail('no bubbles over the idle fields');
  // positions: every field at exactly the board's coords
  const cal = data.cal;
  const proj = (x, y) => [Math.round((x - cal.originX) / cal.spanX * 10000) / 10, Math.round((y - cal.originY) / cal.spanY * 10000) / 10];
  const board = [...Object.values(data.spice_candidate_coords), ...pvpMeds.map((m) => proj(m.x, m.y)),
    ...live.spice.dimensions['1'].ram_active_fields.map((f) => [f.nx, f.ny])];
  const off = board.map(([x, y]) => Math.min(...s.fields.map((f) => Math.hypot(f.nx - x, f.ny - y))));
  R.maxBoardOffset = Math.max(...off);
  if (R.maxBoardOffset > 3) fail(`a field sits ${R.maxBoardOffset.toFixed(1)} units off its board position`);
  // projected onto the canvas
  const outside = s.fields.filter((f) => f.sx < 0 || f.sy < 0 || f.sx > s.box.w || f.sy > s.box.h);
  if (outside.length) fail(`${outside.length} fields project off the canvas at default zoom`);
  const big = s.fields.find((f) => f.kind === 'active');
  if (!big || Math.hypot(big.nx - 479.2, big.ny - 104.2) > 1) fail('active Large not at I5 (479.2,104.2)');
  R.ridgeMax = Math.max(...s.fields.map((f) => f.ridge));
  R.fieldsShot = s.fields.map((f) => ({ k: f.kind, nx: f.nx, ny: f.ny, sx: Math.round(f.sx), sy: Math.round(f.sy), ridge: +f.ridge.toFixed(1) }));
  const on = await page.screenshot({ path: `${OUT}/${tag}-holo-static.png` });
  await toggle(page, /Large fields/); await toggle(page, /Medium fields/);
  await page.mouse.move(1500, 80); await page.waitForTimeout(800);
  const offShot = await page.screenshot({ path: `${OUT}/${tag}-holo-static-off.png` });
  const pts = s.fields.map((f) => ({ x: s.box.x + f.sx, y: s.box.y + f.sy }));
  const d = await diffAt(page, on, offShot, pts, 7);
  const vis = {}; const tot = {};
  s.fields.forEach((f, i) => { tot[f.kind] = (tot[f.kind] || 0) + 1; if (d[i] >= 6) vis[f.kind] = (vis[f.kind] || 0) + 1; });
  R.visible = { vis, tot };
  for (const k of Object.keys(tot)) if ((vis[k] || 0) < tot[k]) fail(`only ${vis[k] || 0}/${tot[k]} ${k} fields visibly drawn`);
  // hover still reports a spice field (under the active Large)
  await toggle(page, /Large fields/); await toggle(page, /Medium fields/);
  const act = s.fields.find((f) => f.kind === 'medium');
  await page.mouse.move(s.box.x + act.sx, s.box.y + act.sy); await page.waitForTimeout(600);
  R.hover = await page.evaluate(() => document.querySelector('.tooltip')?.textContent.trim() || null);
  if (!/spice/i.test(R.hover || '')) fail('hover on a medium field shows no spice tooltip: ' + R.hover);
  R.errors = log.errors;
  if (log.errors.length) fail('page errors: ' + log.errors.join(' | '));
  await ctx.close();
}

// 1b) worms: the PvP worm (H3) must draw a visible blip/ripple from above.
// Two static loads, with and without the worm feed, diffed at the worm.
{
  const noWorms = JSON.stringify({ ...live, worms: { dimensions: {} } });
  const A = await boot(b, { reducedMotion: 'reduce', hour: 12 });
  const w = await A.page.evaluate(() => {
    const q = document.querySelector('.holo-canvas').__holoQa, c = document.querySelector('.holo-canvas').getBoundingClientRect();
    const wi = q.worms();
    return { n: wi.blips, pts: wi.at.map((p) => { const s = q.project(p[0], p[2]); return { x: c.x + s.screenX, y: c.y + s.screenY }; }) };
  });
  const shotA = await A.page.screenshot({ path: `${OUT}/${tag}-worms-on.png` });
  const B = await boot(b, { reducedMotion: 'reduce', hour: 12, liveBody: noWorms });
  const shotB = await B.page.screenshot();
  const d = w.pts.length ? await diffAt(A.page, shotA, shotB, w.pts, 9) : [];
  R.worms = { blips: w.n, want: live.worms.dimensions['1'].worms.length, diff: d };
  if (w.n !== R.worms.want) fail(`${w.n} worm blips, want ${R.worms.want}`);
  if (!d.length || d.some((n) => n < 6)) fail('worm blip not visible from above: ' + JSON.stringify(d));
  await A.ctx.close(); await B.ctx.close();
}

// 2) motion screenshots: default zoom at dawn / day / night, and a close-up of the blow
if (!process.env.SKIP_SHOTS) for (const [name, hour] of [['dawn', 6], ['day', 12], ['night', 23]]) {
  const { page, ctx } = await boot(b, { hour });
  await page.screenshot({ path: `${OUT}/${tag}-holo-${name}.png` });
  if (name === 'day') {
    const s = await info(page);
    const blow = s.fields.find((f) => f.kind === 'active');
    await page.mouse.move(s.box.x + blow.sx, s.box.y + blow.sy + 40);
    for (let i = 0; i < 6; i++) { await page.mouse.wheel(0, -300); await page.waitForTimeout(250); }
    await page.waitForTimeout(2500);
    await page.mouse.move(1500, 80); await page.waitForTimeout(500);
    await page.screenshot({ path: `${OUT}/${tag}-holo-blow-close.png` });
    await page.getByRole('button', { name: /^board$/i }).first().click(); await page.waitForTimeout(3000);
    await page.screenshot({ path: `${OUT}/${tag}-board.png` });
  }
  await ctx.close();
}
await b.close();
R.pass = R.fail.length === 0;
writeFileSync(`${OUT}/${tag}-spice-replay.json`, JSON.stringify(R, null, 1));
console.log(JSON.stringify({ pass: R.pass, fail: R.fail, counts: R.counts, visible: R.visible, ridgeMax: R.ridgeMax, maxBoardOffset: R.maxBoardOffset, hover: R.hover, worms: R.worms }, null, 1));
process.exit(R.pass ? 0 : 1);
