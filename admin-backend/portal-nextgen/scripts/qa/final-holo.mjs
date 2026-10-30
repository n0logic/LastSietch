// Final-gate War-Table sweep: per map load, network assets, rotation, ToD, hover/click, DD-only holoLayout,
// reduced motion and mobile. Usage: QA_OUT=<dir> PW_DIR=<dir with playwright-core> node final-holo.mjs [map ...]
import { mkdirSync, writeFileSync } from 'node:fs';
import { launch, open, toHolo as toHolo1, heading, BASE } from './lib.mjs';
const OUT = process.env.QA_OUT || '.';
mkdirSync(OUT, { recursive: true });
const maps = process.argv.slice(2).length ? process.argv.slice(2) : ['hagga', 'deep-desert', 'arrakeen', 'harko-village'];
const b = await launch();
const R = {};
const noise = /\/(me|stream|waypoints|vehicles)\b|api\/dune\/vehicles|Failed to load resource|HTTP 404/;
const clean = (log) => ({ errors: log.errors, console: log.console.filter((l) => !noise.test(l)) });
const H = (page) => heading(page).then((s) => s && +s.match(/Heading (\d+)/)[1]);
const click = (page, l) => page.evaluate((x) => document.querySelector(`[aria-label="${x}"]`)?.click(), l);
async function toHolo(page, ms = 14000) {
  page.setDefaultTimeout(150000);
  for (let i = 0; i < 3; i++) { await toHolo1(page, ms); if (await page.evaluate(() => !!document.querySelector('.holo-canvas.live'))) return true; }
  return false;
}
const tod = (page) => page.evaluate(() => document.querySelector('.holo-canvas')?.dataset.tod);
const tip = (page) => page.evaluate(() => document.querySelector('.tooltip')?.textContent.trim() || null);
const assets = (log) => log.net.filter((u) => /^\/(terrain|map-icons)\//.test(u) || /HaggaBasin|drape|normal|atlas/i.test(u));
async function probe(page, re, x0 = 430, x1 = 960, y0 = 310, y1 = 850, step = 14) {
  for (let y = y0; y < y1; y += step) for (let x = x0; x < x1; x += step) {
    await page.mouse.move(x, y); const t = await tip(page);
    if (t && re.test(t)) return { x, y, t };
  }
  return null;
}
for (const m of maps) {
  const r = (R[m] = {});
  const { page, log, ctx } = await open(b, `/maps/${m}?hour=12`);
  await page.waitForTimeout(2500);
  r.live = await toHolo(page);
  await page.mouse.move(1500, 80); await page.waitForTimeout(500);
  r.tod12 = await tod(page);
  await page.screenshot({ path: `${OUT}/${m}-holo-day.png` });
  r.assets = [...new Set(assets(log))];
  r.maxIdx = await page.evaluate(() => window.__maxIdx);
  r.gridLabels = await page.evaluate(() => Array.from(document.querySelectorAll('*')).filter((e) => !e.children.length && /^[A-I][1-9]$/.test((e.textContent || '').trim())).length);
  r.layoutChip = await page.evaluate(() => document.querySelector('.layout-chip')?.textContent?.trim() || null);
  // rotation
  r.rot = { start: await H(page) };
  await page.keyboard.press('e'); await page.waitForTimeout(1400); r.rot.e = await H(page);
  await page.keyboard.press('q'); await page.keyboard.press('q'); await page.waitForTimeout(1400); r.rot.qq = await H(page);
  await page.screenshot({ path: `${OUT}/${m}-rot-q.png` });
  await click(page, 'Reset to north up'); await page.waitForTimeout(1500); r.rot.n = await H(page);
  await page.keyboard.press('e'); await page.keyboard.press('e'); await page.waitForTimeout(1400);
  await click(page, 'Reset view'); await page.waitForTimeout(2000); r.rot.home = await H(page);
  // hover a marker, click a cluster
  r.hoverMarker = await probe(page, /./);
  r.cluster = await probe(page, /Zoom in to see each one/);
  if (r.cluster) {
    await page.mouse.click(r.cluster.x, r.cluster.y); await page.waitForTimeout(2500);
    r.cluster.after = await tip(page);
    await page.screenshot({ path: `${OUT}/${m}-cluster-click.png` });
  }
  Object.assign(r, clean(log));
  await ctx.close();
  // day/night presets via the toggle (no ?hour pin, which would override it)
  {
    const { page: p2, ctx: c2 } = await open(b, `/maps/${m}`);
    await p2.waitForTimeout(2000); await toHolo(p2, 12000);
    r.todSeq = {};
    for (const name of ['dawn', 'day', 'dusk', 'night', 'auto']) {
      const btn = p2.locator('.tod-btn', { hasText: new RegExp(name === 'auto' ? 'auto light' : name, 'i') }).first();
      if (await btn.count()) { await btn.click(); await p2.waitForTimeout(2500); r.todSeq[name] = [await tod(p2), await p2.evaluate(() => localStorage.getItem('ls-world-tod'))]; if (name === 'night' || name === 'dusk') { await p2.mouse.move(1500, 80); await p2.screenshot({ path: `${OUT}/${m}-tod-${name}.png` }); } }
      else r.todSeq[name] = 'NO BUTTON';
    }
    await c2.close();
  }
}
// ?hour override at night
for (const m of maps) {
  const { page, log, ctx } = await open(b, `/maps/${m}?hour=23`);
  await toHolo(page, 12000); await page.mouse.move(1500, 80);
  (R[m] ||= {}).hour23 = await tod(page);
  await page.screenshot({ path: `${OUT}/${m}-night-hour23.png` }); await ctx.close();
}
// holoLayout DD-only
if (maps.includes('hagga') || maps.includes('deep-desert')) {
  R.layout = {};
  for (const [m, key] of [['hagga', 'hagga'], ['deep-desert', 'dd']]) {
    const { page, log, ctx } = await open(b, `/maps/${m}?holoLayout=3&hour=12`);
    await page.waitForTimeout(2000); await toHolo(page, 14000);
    R.layout[key] = { chip: await page.evaluate(() => document.querySelector('.layout-chip')?.textContent?.trim() || null), layout3: log.net.some((u) => /layout_3/.test(u)), haggaTerrain: log.net.some((u) => /terrain\/hagga/.test(u)), ...clean(log) };
    await ctx.close();
  }
}
// reduced motion + mobile on each requested map
for (const m of maps) {
  {
    const { page, log, ctx } = await open(b, `/maps/${m}?hour=12`, { reducedMotion: 'reduce' });
    await page.waitForTimeout(2000); await toHolo(page, 14000);
    const rm = ((R[m] ||= {}).reduced = { maxIdx: await page.evaluate(() => window.__maxIdx) });
    await page.keyboard.press('e'); await page.waitForTimeout(150); rm.headingAfterE_150ms = await H(page);
    await page.screenshot({ path: `${OUT}/${m}-reduced.png` }); Object.assign(rm, clean(log)); await ctx.close();
  }
  {
    const { page, log, ctx } = await open(b, `/maps/${m}?hour=12`, { viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
    await page.waitForTimeout(2500);
    const mo = ((R[m] ||= {}).mobile = {});
    const t = page.getByRole('button', { name: /holo table/i }).first();
    mo.toggle = await t.isVisible().catch(() => false);
    if (mo.toggle) await t.click().catch(() => {});
    await page.waitForTimeout(14000);
    mo.live = await page.evaluate(() => !!document.querySelector('.holo-canvas.live'));
    mo.hScroll = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
    await page.screenshot({ path: `${OUT}/${m}-mobile.png` }); Object.assign(mo, clean(log)); await ctx.close();
  }
}
writeFileSync(`${OUT}/_final-holo-${maps.join('+')}.json`, JSON.stringify(R, null, 1));
console.log(JSON.stringify(R, null, 1));
await b.close();
