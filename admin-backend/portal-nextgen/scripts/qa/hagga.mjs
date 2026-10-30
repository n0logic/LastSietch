import { writeFileSync, mkdirSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { launch, open, toHolo, heading, OUT } from './lib.mjs';
mkdirSync(OUT, { recursive: true });
const R = {};
const shot = async (page, name, clip) => { const b = await page.screenshot({ path: `${OUT}/${name}.png`, clip }); return createHash('sha1').update(b).digest('hex').slice(0, 10); };
const H = (page) => heading(page).then((s) => s && +s.match(/Heading (\d+)/)[1]);
const needle = (page) => page.evaluate(() => document.querySelector('[aria-label^="Heading"] svg')?.style.transform);
const press = (page, sel) => page.evaluate((q) => document.querySelector(q).click(), sel);
const stage = { x: 410, y: 291, w: 572, h: 571 };
const orbit = async (page, dx, dy) => { await page.mouse.move(700, 580); await page.mouse.down({ button: 'right' }); await page.mouse.move(700 + dx, 580 + dy, { steps: 12 }); await page.mouse.up({ button: 'right' }); await page.waitForTimeout(1500); };
const key = async (page, k, n = 1) => { for (let i = 0; i < n; i++) { await page.keyboard.press(k); await page.waitForTimeout(500); } await page.waitForTimeout(700); };
const maxIdx = (page) => page.evaluate(() => window.__maxIdx);
const mem = (page) => page.evaluate(() => performance.memory ? Math.round(performance.memory.usedJSHeapSize / 1048576) + ' MB used / ' + Math.round(performance.memory.totalJSHeapSize / 1048576) + ' MB total' : 'n/a');
const fps = (page) => page.evaluate(() => new Promise((res) => { let n = 0; const t0 = performance.now(); (function f() { n++; if (performance.now() - t0 < 3000) requestAnimationFrame(f); else res(+(n / 3).toFixed(1)); })(); }));
const b = await launch();

// ---- Hagga main run
{
  const { page, log } = await open(b, '/maps/hagga');
  await page.waitForTimeout(3000);
  await page.mouse.move(700, 580); await page.waitForTimeout(2500);
  await shot(page, 'hagga-2d-same-view');
  await toHolo(page, 18000);
  await page.mouse.move(1300, 120);
  R.heapAfterLoad = await mem(page); R.fps = await fps(page); R.maxIndexCount = await maxIdx(page);
  R.defaultHeading = await H(page);
  await shot(page, 'hagga-holo-default');
  const base = await page.screenshot();
  // markers: default placement vs after a layer-set change that forces a marker rebuild
  await shot(page, 'hagga-markers-before-rebuild');
  await page.getByRole('button', { name: /^none$/i }).first().click(); await page.waitForTimeout(800);
  await page.getByRole('button', { name: /^all$/i }).first().click(); await page.waitForTimeout(3000);
  await shot(page, 'hagga-markers-after-rebuild');
  await page.getByRole('button', { name: /^default$/i }).first().click(); await page.waitForTimeout(2500);

  // rotation
  R.rot = {};
  for (const [n, name] of [[2, 'east'], [2, 'south'], [2, 'west']]) {
    await key(page, 'e', n);
    R.rot[name] = { heading: await H(page), needle: await needle(page) };
    await shot(page, `hagga-rot-${name}`);
  }
  await key(page, 'e', 1); R.rot.afterFullTurn = { heading: await H(page), needle: await needle(page) };
  await key(page, 'q', 1); R.rot.qBack = await H(page);
  await press(page, '[aria-label="Reset to north up"]'); await page.waitForTimeout(1500);
  R.rot.afterN = await H(page); const afterN = await page.screenshot();
  R.rot.nRestoresDefault = createHash('sha1').update(afterN).digest('hex').slice(0, 10) === createHash('sha1').update(base).digest('hex').slice(0, 10);
  // dial click steps 45
  await press(page, '[aria-label^="Heading"]'); await page.waitForTimeout(900); R.rot.dialClick = await H(page);
  await press(page, '[aria-label="Reset to north up"]'); await page.waitForTimeout(1500);

  // tilt (0.3 deg/px: default 35)
  await orbit(page, 0, -50); R.tiltLowHash = await shot(page, 'hagga-tilt-low');
  await orbit(page, 0, -500); R.tiltLowOverdragHash = await shot(page, '_tmp-low2');
  await press(page, '[aria-label="Reset to north up"]'); await page.waitForTimeout(1500);
  await orbit(page, 0, 150); R.tiltHighHash = await shot(page, 'hagga-tilt-high');
  await orbit(page, 0, 500); R.tiltHighOverdragHash = await shot(page, '_tmp-high2');
  // right-drag horizontal changes heading
  await press(page, '[aria-label="Reset to north up"]'); await page.waitForTimeout(1500);
  await orbit(page, -225, 0); R.dragHeading = await H(page); // -225px * 0.4 -> heading +90
  // NW zoom
  await press(page, '[aria-label="Reset to north up"]'); await page.waitForTimeout(1500);
  await page.mouse.move(480, 360); for (let i = 0; i < 10; i++) { await page.mouse.wheel(0, -300); await page.waitForTimeout(250); }
  await orbit(page, 0, -50); await page.waitForTimeout(2000);
  await shot(page, 'hagga-nw-zoom');
  // reset: tilt+heading+zoom then N, then home
  await press(page, '[aria-label="Reset to north up"]'); await page.waitForTimeout(1500);
  await key(page, 'e', 3); await orbit(page, 0, 120);
  await press(page, '[aria-label="Reset to north up"]'); await page.waitForTimeout(1500);
  R.afterN = { heading: await H(page) }; await shot(page, 'hagga-after-reset-N');
  await key(page, 'e', 3); await orbit(page, 0, -60);
  await press(page, '[aria-label="Reset view"]'); await page.waitForTimeout(2000);
  R.afterHome = { heading: await H(page) }; const hs = await shot(page, 'hagga-after-reset-home');
  R.afterHome.matchesDefaultHash = hs === createHash('sha1').update(base).digest('hex').slice(0, 10);
  await shot(page, 'hagga-after-reset');
  R.hagga = { console: log.console.filter((l) => !/\/(me|stream|waypoints|vehicles)\b|api\/dune\/vehicles|Failed to load resource/.test(l)), errors: log.errors,
    net: [...new Set(log.net.filter((u) => /^\/(terrain|assets|api|portal)\//.test(u) || /amtal/i.test(u)))] };
  await page.context().close();
}
writeFileSync(`${OUT}/_results-hagga.json`, JSON.stringify(R, null, 1));
console.log(JSON.stringify(R, null, 1));
await b.close();
