// Sand Table FX screenshots + checks against the local review preview. Usage:
//   PW_DIR=<dir with playwright-core> QA_OUT=~/dd-re/fx node sandtable-fx.mjs [shot ...]
// shots: default storm hagga windpass primrose sandbike reduced low
import { launch, open } from './lib.mjs';
const OUT = process.env.QA_OUT || process.env.HOME + '/dd-re/fx';
const want = new Set(process.argv.slice(2));
const on = (n) => !want.size || want.has(n);
const browser = await launch();
const VP = { viewport: { width: 1600, height: 1250 } };
const HR = '&hour=15';
const stormBody = () => ({ sandstorm: { dimensions: { 1: { storm_sector: 'E8', storm_scanned_utc: new Date(Date.now() - 30000).toISOString(), last_spawn_utc: new Date(Date.now() - 60000).toISOString(), center_nx: 560, center_ny: 380, radius_nr: 220, heading_yaw: 36.8, stage: 2 } } } });

// Hagga: Habbanya and Amtal storming at once; dim 1 (Kulon, closed 2026-10-28) clear and has no tab.
const haggaBody = () => { const at = new Date(Date.now() - 30000).toISOString(); return { sandstorm: { dimensions: {
  0: { storm_state: 'active', storm_scanned_utc: at, center_nx: 260, center_ny: 330, radius_nr: 200, heading_yaw: 20 },
  1: { storm_state: 'clear', next_eta_utc: new Date(Date.now() + 1800000).toISOString() },
  2: { storm_state: 'active', storm_scanned_utc: at, center_nx: 640, center_ny: 560, radius_nr: 220, heading_yaw: 210 } } } }; };

async function scene(path, o = {}) {
  const r = await open(browser, path, { ...VP, ...o });
  if (o.storm) await r.page.route('**/portal/maps/*/live', (route) => route.fulfill({ json: /\/maps\/hagga\//.test(route.request().url()) ? haggaBody() : stormBody() }));
  return r;
}
async function ready(page, ms = 5000) {
  await page.waitForFunction(() => document.querySelector('.host') && document.querySelector('.host').__sandtable && !document.querySelector('.state.over'), null, { timeout: 90000 }).catch(() => {});
  await page.waitForTimeout(ms);
}
const info = (page) => page.evaluate(() => { const h = document.querySelector('.host'); const c = h && h.__sandtable; if (!c) return 'no-host'; return c.fxInfo ? c.fxInfo() : null; });
async function focus(page, id, tiltPx = 0, ms = 2600) {
  await page.evaluate((i) => document.querySelector('.host').__sandtable.descend(i, () => {}), id);
  await page.waitForTimeout(ms);
  if (tiltPx) {   // right-drag changes the tilt: a steeper look down onto the stone
    const b = await page.locator('.host').boundingBox();
    await page.mouse.move(b.x + b.width * 0.08, b.y + b.height * 0.3); await page.mouse.down({ button: 'right' });
    await page.mouse.move(b.x + b.width * 0.08, b.y + b.height * 0.3 + tiltPx, { steps: 8 }); await page.mouse.up({ button: 'right' });
    await page.waitForTimeout(1200);
  }
}
const rec = [];
const done = async (n, page, log, ctx) => { rec.push([n, JSON.stringify({ errors: log.errors, fx: await info(page) })]); console.log(n, rec.at(-1)[1], log.console.filter((c) => !/HTTP 404/.test(c)).slice(0, 4)); await ctx.close(); };

if (on('default')) { const { ctx, page, log } = await scene('/world?view=3d' + HR); await ready(page); await page.screenshot({ path: OUT + '/01-default.png' }); await done('default', page, log, ctx); }
if (on('storm')) { const { ctx, page, log } = await scene('/world?view=3d' + HR, { storm: true }); await ready(page, 7000); await focus(page, 'deep-desert', 160); await page.waitForTimeout(1500); await page.screenshot({ path: OUT + '/02-storm.png' }); await done('storm', page, log, ctx); }
if (on('hagga')) { const { ctx, page, log } = await scene('/world?view=3d' + HR, { storm: true }); await ready(page, 7000); await focus(page, 'hagga-basin', 160); await page.waitForTimeout(1500); await page.screenshot({ path: OUT + '/10-hagga-storms.png' }); await done('hagga', page, log, ctx); }
if (on('windpass')) { const { ctx, page, log } = await scene('/world?view=3d' + HR); await ready(page); await focus(page, 'wind-pass', 160); await page.screenshot({ path: OUT + '/03-windpass-darkness.png' }); await done('windpass', page, log, ctx); }
if (on('primrose')) { const { ctx, page, log } = await scene('/world?view=3d' + HR); await ready(page); await focus(page, 'water-shippers-mansion'); await page.screenshot({ path: OUT + '/04-primrose.png' }); await done('primrose', page, log, ctx); }
if (on('sandbike')) { const { ctx, page, log } = await scene('/world?view=3d' + HR); await ready(page); const bp = (await info(page)).vehiclePos.sandbike;
  await page.evaluate(([x, z]) => document.querySelector('.host').__sandtable.focusPoint(x, z, 8, 50), [bp[0], bp[2]]);
  await page.waitForTimeout(2200);
  await page.screenshot({ path: OUT + '/05-sandbike.png' }); await done('sandbike', page, log, ctx); }
if (on('orni')) { const { ctx, page, log } = await scene('/world?view=3d' + HR); await ready(page);
  const bp = (await info(page)).vehiclePos.ornithopter;
  await page.evaluate(([x, z]) => document.querySelector('.host').__sandtable.focusPoint(x, z, 9, 50), [bp[0], bp[2]]);
  await page.waitForTimeout(2200); await page.screenshot({ path: OUT + '/07-ornithopter.png' }); await done('orni', page, log, ctx); }
if (on('night')) { const { ctx, page, log } = await scene('/world?view=3d&hour=22'); await ready(page, 6000); await page.screenshot({ path: OUT + '/08-night.png' });
  const bp = (await info(page)).vehiclePos.sandbike;
  await page.evaluate(([x, z]) => document.querySelector('.host').__sandtable.focusPoint(x, z, 8, 50), [bp[0], bp[2]]);
  await page.waitForTimeout(2200); await page.screenshot({ path: OUT + '/09-night-sandbike.png' }); await done('night', page, log, ctx); }
if (on('reduced')) { const { ctx, page, log } = await scene('/world?view=3d' + HR, { reducedMotion: 'reduce', storm: true }); await ready(page, 6000); await page.screenshot({ path: OUT + '/06-reduced.png' }); await done('reduced', page, log, ctx); }
await browser.close();
