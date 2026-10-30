// Holo anchor parallax: every overlay anchor (marker points, cluster discs, spice
// pips) must sit on the DRAWN relief, so icons stay put on the ground when the
// camera tilts, rotates, zooms or pans. For each anchor as uploaded to the GPU we
// raycast the terrain mesh straight down at the same (nx, ny) and compare the two
// projected screen points across camera poses. Fails over MAX_PX.
// Deep Desert replays the captured live payloads (SPICE_DIR) so spice pips exist.
// Usage: QA_OUT=<dir> PW_DIR=<dir> node parallax.mjs [map ...]
import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { launch, open, toHolo, BASE } from './lib.mjs';
const OUT = process.env.QA_OUT || '.';
const DIR = process.env.SPICE_DIR || process.env.HOME + '/dd-re/spice-fix';
const MAX_PX = 2;
mkdirSync(OUT, { recursive: true });
const maps = process.argv.slice(2).length ? process.argv.slice(2) : ['deep-desert', 'hagga', 'arrakeen', 'harko-village'];
const R = { fail: [] };
const b = await launch();

async function boot(m) {
  const s = await open(b, '/', {});
  const { page } = s;
  if (m === 'deep-desert' && existsSync(DIR + '/dd_data.json')) {
    const data = readFileSync(DIR + '/dd_data.json', 'utf8'), live = readFileSync(DIR + '/dd_live.json', 'utf8');
    await page.route('**/portal/maps/deep-desert/data*', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: data }));
    await page.route('**/portal/maps/deep-desert/live*', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: live }));
  }
  await page.evaluate((k) => localStorage.setItem('map_layers_' + k, '{}'), m);
  await page.goto(`${BASE}/maps/${m}?hour=12`, { waitUntil: 'load' });
  await page.waitForTimeout(3000);
  if (!(await page.evaluate(() => !!document.querySelector('.holo-canvas.live')))) await toHolo(page, 16000);
  await page.waitForFunction(() => document.querySelector('.holo-canvas')?.__holoQa, null, { timeout: 60000 });
  await page.waitForTimeout(8000);   // real relief + re-drape
  return s;
}

// Surface Y per anchor is camera-independent: raycast once, keep in the page.
const prep = (page) => page.evaluate(() => {
  const qa = document.querySelector('.holo-canvas').__holoQa;
  const a = qa.anchors(40);
  window.__px = {};
  for (const fam of Object.keys(a)) window.__px[fam] = a[fam].map((p) => ({ p, sy: qa.surfaceY(p[0], p[2]) })).filter((e) => e.sy != null);
  return Object.fromEntries(Object.keys(window.__px).map((f) => [f, window.__px[f].length]));
});
const measure = (page) => page.evaluate(() => {
  const qa = document.querySelector('.holo-canvas').__holoQa;
  const c = document.querySelector('.holo-canvas');
  const out = {};
  for (const fam of Object.keys(window.__px)) {
    const d = [];
    for (const e of window.__px[fam]) {
      const A = qa.toScreen(e.p[0], e.p[1], e.p[2]), G = qa.toScreen(e.p[0], e.sy, e.p[2]);
      if (A.x < 0 || A.y < 0 || A.x > c.clientWidth || A.y > c.clientHeight) continue;   // on screen only
      d.push(Math.hypot(A.x - G.x, A.y - G.y));
    }
    d.sort((x, y) => x - y);
    out[fam] = d.length ? { n: d.length, p95: +d[Math.floor(d.length * 0.95)].toFixed(2), max: +d[d.length - 1].toFixed(2) } : { n: 0 };
  }
  return out;
});

for (const m of maps) {
  const r = (R[m] = { poses: {} });
  const { page, ctx, log } = await boot(m);
  r.anchors = await prep(page);
  const box = await page.evaluate(() => { const q = document.querySelector('.holo-canvas').getBoundingClientRect(); return { x: q.x + q.width / 2, y: q.y + q.height / 2 }; });
  const settle = () => page.waitForTimeout(1600);
  const drag = async (button, dx, dy) => {
    await page.mouse.move(box.x, box.y); await page.mouse.down({ button });
    for (let i = 1; i <= 10; i++) await page.mouse.move(box.x + dx * i / 10, box.y + dy * i / 10);
    await page.mouse.up({ button }); await settle();
  };
  const pose = async (name) => { r.poses[name] = await measure(page); };
  await pose('default');
  await drag('right', 0, -200); await pose('tilt-low');
  await drag('right', 0, 400); await pose('tilt-high');
  await page.keyboard.press('e'); await page.keyboard.press('e'); await settle(); await pose('rotated-90');
  await page.mouse.move(box.x, box.y);
  for (let i = 0; i < 6; i++) { await page.mouse.wheel(0, -300); await page.waitForTimeout(200); }
  await settle(); await pose('zoomed');
  await drag('left', 120, 80); await pose('panned');
  await drag('right', 0, -300); await pose('zoomed-tilt-low');
  for (const [p, fams] of Object.entries(r.poses)) for (const [f, v] of Object.entries(fams)) {
    if (v.n && v.max > MAX_PX) R.fail.push(`${m} ${p} ${f}: anchor ${v.max}px off the drawn relief (p95 ${v.p95})`);
  }
  r.errors = log.errors;
  if (log.errors.length) R.fail.push(`${m}: page errors ${log.errors.join(' | ')}`);
  await ctx.close();
}
await b.close();
R.pass = R.fail.length === 0;
writeFileSync(`${OUT}/_parallax-${maps.join('+')}.json`, JSON.stringify(R, null, 1));
console.log(JSON.stringify(R, null, 1));
process.exit(R.pass ? 0 : 1);
