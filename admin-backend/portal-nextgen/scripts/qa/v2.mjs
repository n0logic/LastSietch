// Re-verification of the 2.26.0 relief fixes: first-load marker placement, relief-aware hit-test, orbit ease range, storm.
import { writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { launch, open, toHolo, heading, OUT, SUF } from './lib.mjs';
const R = {}; const tmp = process.env.PW_DIR; const storm = process.env.LASTSIETCH_PREVIEW_STORM === '1';
const b = await launch();
const { page, log } = await open(b, '/maps/hagga');
await page.waitForTimeout(2500);
await toHolo(page, 18000);
const press = (sel) => page.evaluate((q) => document.querySelector(q).click(), sel);
const N = () => press('[aria-label="Reset to north up"]');
const tip = () => page.evaluate(() => document.querySelector('.tooltip')?.textContent?.trim() || null);
const orbit = async (dx, dy) => { await page.mouse.move(700, 580); await page.mouse.down({ button: 'right' }); await page.mouse.move(700 + dx, 580 + dy, { steps: 12 }); await page.mouse.up({ button: 'right' }); await page.waitForTimeout(1500); };
const keys = async (k, n) => { for (let i = 0; i < n; i++) { await page.keyboard.press(k); await page.waitForTimeout(500); } await page.waitForTimeout(800); };
await page.mouse.move(1300, 150); await page.waitForTimeout(600);
await page.screenshot({ path: `${OUT}/hagga-holo-default${SUF}.png` });
const HITS_ONLY = process.env.HITS_ONLY === '1';
if (storm && !HITS_ONLY) {
  await page.screenshot({ path: `${OUT}/hagga-storm-default${SUF}.png` });
  await orbit(0, -50); await page.mouse.move(1300, 150); await page.screenshot({ path: `${OUT}/hagga-storm-tilt20${SUF}.png` });
  await N(); await page.waitForTimeout(1500);
} else {
if (!HITS_ONLY) {
// cliffs close-up, still without touching layers
await page.mouse.move(700, 470); for (let i = 0; i < 8; i++) { await page.mouse.wheel(0, -300); await page.waitForTimeout(250); }
await page.waitForTimeout(2500); await page.mouse.move(1300, 150); await page.waitForTimeout(500);
await page.screenshot({ path: `${OUT}/hagga-cliffs-closeup${SUF}.png` });
await press('[aria-label="Reset view"]'); await page.waitForTimeout(2000);

// ease range sampling
const sample = (act) => page.evaluate(async (a) => { const vals = []; const rd = () => { const d = document.querySelector('[aria-label^="Heading"]'); return +d.getAttribute('aria-label').match(/Heading (\d+)/)[1]; };
  let stop = false; const loop = () => { vals.push(rd()); if (!stop) requestAnimationFrame(loop); }; requestAnimationFrame(loop);
  vals.push(rd()); a(); await new Promise((r) => setTimeout(r, 900)); stop = true; return vals; }, null).catch(() => null);
const ease = async (name, fire, lo, hi) => {
  const vals = await page.evaluate(async ({ fire }) => { const vals = []; const rd = () => +document.querySelector('[aria-label^="Heading"]').getAttribute('aria-label').match(/Heading (\d+)/)[1];
    let stop = false; const loop = () => { vals.push(rd()); if (!stop) requestAnimationFrame(loop); }; requestAnimationFrame(loop);
    window.dispatchEvent(new KeyboardEvent('keydown', { key: fire })); await new Promise((r) => setTimeout(r, 900)); stop = true; return vals; }, { fire });
  R.ease = R.ease || {}; R.ease[name] = { min: Math.min(...vals), max: Math.max(...vals), frames: vals.length, outOfRange: vals.filter((v) => v < lo || v > hi).length, sample: vals.filter((_, i) => i % 3 === 0).slice(0, 14) };
};
for (let i = 0; i < 3; i++) { await ease('E_from_0_run' + i, 'e', 0, 45); await N(); await page.waitForTimeout(1200); }
await keys('e', 2); await ease('N_from_90', 'e', 90, 135); // sanity on another step
await N(); await page.waitForTimeout(1200);

}
// hit tests
const layersOnly = async (name) => { await page.getByRole('button', { name: /^none$/i }).first().click(); await page.waitForTimeout(500); if (name) { await page.getByText(name, { exact: true }).first().click(); await page.waitForTimeout(2200); } };
const find = async (tag, name) => { await page.mouse.move(1300, 150); await page.waitForTimeout(400); const on = `${tmp}/${tag}-on.png`, off = `${tmp}/${tag}-off.png`;
  await layersOnly(name); await page.mouse.move(1300, 150); await page.waitForTimeout(400); await page.screenshot({ path: on });
  await layersOnly(null); await page.mouse.move(1300, 150); await page.waitForTimeout(600); await page.screenshot({ path: off });
  await layersOnly(name); return JSON.parse(execFileSync('python3', ['find_markers.py', on, off]).toString()); };
R.hits = [];
const views = [['h0_t35', async () => {}], ['h135_t59', async () => { await keys('e', 3); await orbit(0, 80); }], ['h270_t20', async () => { await keys('e', 6); await orbit(0, -50); }]];
for (const [vn, setup] of views) {
  await N(); await page.waitForTimeout(1500); await setup();
  for (const [set, expect] of [['CHOAM Exchange', /CHOAM/], ['Cave', /Cave/], ['Exploration Point Of Interest', /Exploration/]]) {
    let pts; try { pts = await find(`${vn}-${set.slice(0, 4)}`, set); } catch (e) { R.hits.push({ view: vn, set, error: String(e).slice(0, 80) }); continue; }
    pts = pts.filter((p) => p[2] <= 14).slice(0, 12); let hit = 0, tol = 0; const miss = [];
    const named = set.startsWith('Expl');   // named POIs show their own name, not the type label
    const ok = (t) => t && (named || expect.test(t));
    for (const [x, y] of pts) { await page.mouse.move(x, y); await page.waitForTimeout(350); const t = await tip();
      if (ok(t)) { hit++; tol++; continue; }
      let near = false; for (const [dx, dy] of [[-5, 0], [5, 0], [0, -5], [0, 5]]) { await page.mouse.move(x + dx, y + dy); await page.waitForTimeout(250); if (ok(await tip())) { near = true; break; } }
      if (near) tol++; else miss.push({ x, y, tip: t }); }
    R.hits.push({ view: vn, set, found: pts.length, hitExact: hit, hitWithin5px: tol, miss: pts.length - tol, missDetail: miss.slice(0, 4) });
    if (set === 'Cave') { await page.mouse.move(1300, 150); await page.waitForTimeout(500); await page.screenshot({ path: `${OUT}/hit-${vn}${SUF}.png` }); }
  }
}
// popup anchor check: click one cave marker at h270_t20
}
R.console = log.console.filter((l) => !/\/(me|stream|waypoints|vehicles)\b|Failed to load resource/.test(l)); R.errors = log.errors;
writeFileSync(`${OUT}/_results-v2${storm ? '-storm' : ''}${SUF}.json`, JSON.stringify(R, null, 1));
console.log(JSON.stringify(R, null, 1));
await b.close();
