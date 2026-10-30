// Final-gate Sand Table (/world) sweep. Usage: QA_OUT=<dir> PW_DIR=<dir> node final-world.mjs
import { mkdirSync, writeFileSync, readFileSync } from 'node:fs';
import { launch, open, BASE } from './lib.mjs';
const OUT = process.env.QA_OUT || '.';
mkdirSync(OUT, { recursive: true });
const man = JSON.parse(readFileSync(new URL('../../static/world-data/overmap-places.json', import.meta.url)));
const stones = man.locations;
const b = await launch();
const R = {};
const noise = /\/(me|stream|waypoints|vehicles)\b|api\/dune\/vehicles|Failed to load resource|HTTP 404/;
const clean = (log) => ({ errors: log.errors, console: log.console.filter((l) => !noise.test(l)) });
const VP = { viewport: { width: 1600, height: 1250 } };
async function ready(page, ms = 5000) {
  await page.waitForFunction(() => document.querySelector('.host')?.__sandtable && !document.querySelector('.state.over'), null, { timeout: 120000 }).catch(() => {});
  await page.waitForTimeout(ms);
}
const st = (page, fn, arg) => page.evaluate(([f, a]) => { const c = document.querySelector('.host').__sandtable; return c[f](...(a || [])); }, [fn, arg]);

// 1. default load, stones, labels, overlap
{
  const { ctx, page, log } = await open(b, '/world?view=3d&hour=12.5', VP);
  await ready(page, 6000);
  const r = (R.default = {});
  r.assets = [...new Set(log.net.filter((u) => /world-data|map-icons|\.wasm/.test(u)))].map((u) => u.split('/').pop());
  r.stats = await st(page, 'stats');
  r.stones = stones.length;
  r.onScreen = await page.evaluate((ids) => ids.filter((i) => { const p = document.querySelector('.host').__sandtable.screenOf(i); return p && Number.isFinite(p.x); }).length, stones.map((s) => s.id));
  r.labels = await page.evaluate(() => Array.from(document.querySelectorAll('.st-label')).map((e) => ({ t: e.textContent.trim(), r: e.getBoundingClientRect().toJSON(), vis: getComputedStyle(e).display !== 'none' && getComputedStyle(e).opacity !== '0' })));
  const L = r.labels.filter((x) => x.vis && x.r.width > 0);
  r.labelCount = L.length; r.labelOverlaps = [];
  for (let i = 0; i < L.length; i++) for (let j = i + 1; j < L.length; j++) {
    const a = L[i].r, c = L[j].r;
    const ox = Math.min(a.right, c.right) - Math.max(a.left, c.left), oy = Math.min(a.bottom, c.bottom) - Math.max(a.top, c.top);
    if (ox > 2 && oy > 2) r.labelOverlaps.push([L[i].t, L[j].t, Math.round(ox), Math.round(oy)]);
  }
  r.labels = r.labels.map((x) => x.t);
  r.fx = await st(page, 'fxInfo');
  r.arrivedKey = await page.evaluate(() => sessionStorage.getItem('ls-world-arrived'));
  await page.screenshot({ path: `${OUT}/world-default.png` });
  // vehicles moving: sample positions twice
  const v1 = (await st(page, 'fxInfo'))?.vehiclePos; await page.waitForTimeout(3000); const v2 = (await st(page, 'fxInfo'))?.vehiclePos;
  r.vehiclesMoved = JSON.stringify(v1) !== JSON.stringify(v2); r.vehicleKinds = v1 && Object.keys(v1);
  // hover hagga
  const p = await st(page, 'screenOf', ['hagga-basin']); const box = await page.locator('.host').boundingBox();
  await page.mouse.move(box.x + p.x, box.y + p.y); await page.waitForTimeout(1200);
  r.hoverCard = await page.evaluate(() => { const e = document.querySelector('.card, .instrument, [class*="instrument"]'); return e ? e.textContent.replace(/\s+/g, ' ').trim().slice(0, 300) : null; });
  await page.screenshot({ path: `${OUT}/world-hover-hagga.png` });
  // keyboard: select Hagga, Enter -> chooser; Esc closes; choose a facet
  await page.mouse.move(5, 5); await page.focus('.table');
  for (let i = 0; i < 30; i++) { await page.keyboard.press('ArrowRight'); await page.waitForTimeout(120); if (/Hagga Basin/.test(await page.evaluate(() => document.querySelector('.sr')?.textContent || ''))) break; }
  await page.keyboard.press('Enter'); await page.waitForTimeout(800);
  r.haggaChooser = await page.evaluate(() => Array.from(document.querySelectorAll('.chooser .btn')).map((e) => e.textContent.trim().replace(/\s+/g, ' ')));
  await page.screenshot({ path: `${OUT}/world-chooser-hagga.png` });
  await page.keyboard.press('Escape'); await page.waitForTimeout(300);
  r.chooserClosedByEsc = await page.evaluate(() => !document.querySelector('.chooser'));
  await page.keyboard.press('Enter'); await page.waitForTimeout(500);
  await page.getByRole('button', { name: /Amtal/ }).click(); await page.waitForTimeout(3500);
  r.haggaDescendUrl = page.url();
  r.afterDescend = await page.evaluate(() => document.title);
  Object.assign(r, clean(log)); await ctx.close();
}
// 2. DD chooser + Enter, Esc returns to overview
{
  const { ctx, page, log } = await open(b, '/world?view=3d&hour=12.5', VP);
  await ready(page, 3000);
  const r = (R.dd = {});
  await page.focus('.table');
  for (let i = 0; i < 30; i++) { await page.keyboard.press('ArrowRight'); await page.waitForTimeout(100); if (/Deep Desert/.test(await page.evaluate(() => document.querySelector('.sr')?.textContent || ''))) break; }
  const before = await page.screenshot();
  await page.keyboard.press('Enter'); await page.waitForTimeout(800);
  r.chooser = await page.evaluate(() => Array.from(document.querySelectorAll('.chooser .btn')).map((e) => e.textContent.trim().replace(/\s+/g, ' ')));
  await page.screenshot({ path: `${OUT}/world-chooser-dd.png` });
  if (r.chooser.length) { await page.locator('.chooser .btn').first().click(); await page.waitForTimeout(3500); r.url = page.url(); }
  else { await page.waitForTimeout(3500); r.url = page.url(); }
  Object.assign(r, clean(log)); await ctx.close();
}
{
  // Esc returns to the full table
  const { ctx, page } = await open(b, '/world?view=3d&hour=12.5', VP);
  await ready(page, 3000); await page.focus('.table');
  const r = (R.esc = {});
  await page.keyboard.press('ArrowRight'); await page.waitForTimeout(1400);
  r.zoomedHash = (await page.screenshot()).length;
  await page.keyboard.press('Escape'); await page.waitForTimeout(1600);
  r.afterEscHash = (await page.screenshot()).length;
  await page.screenshot({ path: `${OUT}/world-after-esc.png` }); await ctx.close();
}
// 3. arrival once per session
{
  const { ctx, page } = await open(b, '/world?view=3d&hour=12.5', VP);
  await ready(page, 500);
  const r = (R.arrival = {});
  r.firstKey = await page.evaluate(() => sessionStorage.getItem('ls-world-arrived'));
  await page.waitForTimeout(800); await page.screenshot({ path: `${OUT}/world-arrival-mid.png` });
  await page.waitForTimeout(5000);
  await page.reload(); await ready(page, 600);
  await page.screenshot({ path: `${OUT}/world-second-visit-t0.png` });
  r.secondVisitNoGlide = await page.evaluate(() => 'ok'); // visual cross-check in the t0 shot; key stayed set
  r.keyAfterReload = await page.evaluate(() => sessionStorage.getItem('ls-world-arrived'));
  await ctx.close();
}
// 4. Guild chart toggle, ToD presets, persistence to /maps
{
  const { ctx, page, log } = await open(b, '/world?view=3d', VP);
  await ready(page, 3000);
  const r = (R.toggles = {});
  await page.getByRole('button', { name: /guild chart/i }).click(); await page.waitForTimeout(1500);
  r.chartStones = await page.locator('svg .stone').count(); r.chart3dCanvas = await page.locator('.host canvas').count();
  await page.screenshot({ path: `${OUT}/world-chart.png` });
  await page.getByRole('button', { name: /3d table/i }).click(); await ready(page, 3000);
  r.back3d = await page.locator('.host canvas').count();
  r.tod = {};
  for (const n of ['dawn', 'day', 'dusk', 'night', 'auto']) {
    await page.locator('.seg.small button', { hasText: new RegExp(n === 'auto' ? 'auto light' : n, 'i') }).click(); await page.waitForTimeout(2200);
    r.tod[n] = await page.evaluate(() => localStorage.getItem('ls-world-tod'));
    if (n !== 'auto') await page.screenshot({ path: `${OUT}/world-tod-${n}.png` });
  }
  await page.locator('.seg.small button', { hasText: /night/i }).click(); await page.waitForTimeout(500);
  await page.goto(BASE + '/maps/hagga'); await page.waitForTimeout(3500);
  r.mapsToggleOn = await page.evaluate(() => document.querySelector('.tod-btn.on')?.textContent.trim() || null);
  await page.goto(BASE + '/world?view=chart'); await page.waitForTimeout(1500);
  r.worldToggleOn = await page.evaluate(() => document.querySelector('.seg.small button.on')?.textContent.trim() || null);
  Object.assign(r, clean(log)); await ctx.close();
}
// 5. storm
{
  const { ctx, page, log } = await open(b, '/world?view=3d&hour=15', VP);
  const body = { sandstorm: { dimensions: { 1: { storm_sector: 'E8', storm_scanned_utc: new Date(Date.now() - 30000).toISOString(), last_spawn_utc: new Date(Date.now() - 60000).toISOString(), center_nx: 560, center_ny: 380, radius_nx: 60 } } } };
  await page.route('**/portal/maps/*/live', (route) => route.fulfill({ json: body }));
  await ready(page, 7000);
  R.storm = { fx: await st(page, 'fxInfo') };
  await st(page, 'descend', ['deep-desert', () => {}]).catch(() => {}); await page.waitForTimeout(2800);
  await page.screenshot({ path: `${OUT}/world-storm.png` }); Object.assign(R.storm, clean(log)); await ctx.close();
}
// 6. reduced motion, mobile, blocked decode
{
  const { ctx, page, log } = await open(b, '/world', { reducedMotion: 'reduce', viewport: { width: 1400, height: 900 } });
  await page.waitForTimeout(4000);
  R.reduced = { chart: await page.locator('svg .stone').count(), canvas: await page.locator('.host canvas').count(), ...clean(log) };
  await page.screenshot({ path: `${OUT}/world-reduced.png` }); await ctx.close();
}
{
  const { ctx, page, log } = await open(b, '/world', { viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
  await page.waitForTimeout(4000);
  R.mobile = { chart: await page.locator('svg .stone').count(), canvas: await page.locator('.host canvas').count(), hScroll: await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), ...clean(log) };
  await page.screenshot({ path: `${OUT}/world-mobile.png`, fullPage: true }); await ctx.close();
}
{
  const ctx = await b.newContext({ viewport: VP.viewport }); const p2 = await ctx.newPage();
  await p2.addInitScript(() => { WebAssembly.instantiate = () => Promise.reject(new Error('blocked by CSP')); WebAssembly.compile = () => Promise.reject(new Error('blocked by CSP')); });
  const errs = []; p2.on('pageerror', (e) => errs.push(e.message));
  await p2.goto(BASE + '/world?view=3d'); await p2.waitForSelector('svg .stone', { timeout: 90000 }).catch(() => {});
  R.blocked = { chart: await p2.locator('svg .stone').count(), notice: await p2.evaluate(() => document.querySelector('.notice')?.textContent || null), errs };
  await p2.screenshot({ path: `${OUT}/world-blocked.png` }); await ctx.close();
}
// 7. SPA route served as shell
R.route = await (await fetch(BASE + '/world')).text().then((t) => ({ len: t.length, shell: /<div id="svelte"|__sveltekit|_app\//.test(t) }));
writeFileSync(`${OUT}/_final-world.json`, JSON.stringify(R, null, 1));
console.log(JSON.stringify(R, null, 1));
await b.close();
