// Sand Table (/world) screenshots against the local review preview. Usage:
//   PW_DIR=<dir with playwright-core> QA_OUT=~/dd-re/sandtable node sandtable.mjs [shot ...]
// shots: default rotated hover chart mobile keyboard (all when none given)
import { launch, open, BASE } from './lib.mjs';
const OUT = process.env.QA_OUT || process.env.HOME + '/dd-re/sandtable';
const want = new Set(process.argv.slice(2));
const on = (n) => !want.size || want.has(n);

const browser = await launch();
const VP = { viewport: { width: 1600, height: 1250 } };
const Q = process.env.Q || '';
async function ready(page, ms = 9000) {
  await page.waitForFunction(() => document.querySelector('.host') && document.querySelector('.host').__sandtable && !document.querySelector('.state.over'), null, { timeout: 90000 }).catch(() => {});
  await page.waitForTimeout(ms);
}
const report = (n, log) => console.log(n, JSON.stringify({ errors: log.errors, console: log.console.filter((c) => !/HTTP 404/.test(c)).slice(0, 8) }));

if (on('default')) {
  const { ctx, page, log } = await open(browser, '/world?view=3d' + Q, VP);
  await ready(page, 4500);
  await page.screenshot({ path: OUT + '/01-default.png' });
  console.log('stats', JSON.stringify(await page.evaluate(() => document.querySelector('.host').__sandtable.stats())));
  report('default', log); await ctx.close();
}
if (on('rotated')) {
  const { ctx, page, log } = await open(browser, '/world?view=3d' + Q, VP);
  await ready(page, 3000);
  await page.evaluate(() => document.querySelector('.host').__sandtable.setOrbitNow(135, 52, 1.3));
  await page.waitForTimeout(1500);
  await page.screenshot({ path: OUT + '/02-rotated.png' });
  report('rotated', log); await ctx.close();
}
if (on('hover')) {
  const { ctx, page, log } = await open(browser, '/world?view=3d' + Q, VP);
  await ready(page, 3000);
  const p = await page.evaluate(() => document.querySelector('.host').__sandtable.screenOf('hagga-basin'));
  const box = await page.locator('.host').boundingBox();
  await page.mouse.move(box.x + p.x, box.y + p.y);
  await page.waitForTimeout(1200);
  await page.screenshot({ path: OUT + '/03-hover-hagga.png' });
  report('hover', log); await ctx.close();
}
if (on('chart')) {
  const { ctx, page, log } = await open(browser, '/world?view=chart');
  await page.waitForSelector('svg .stone', { timeout: 20000 }).catch(() => {});
  await page.waitForTimeout(1200);
  await page.hover('[data-stone="hagga-basin"]').catch(() => {});
  await page.waitForTimeout(500);
  await page.screenshot({ path: OUT + '/04-guild-chart.png' });
  report('chart', log); await ctx.close();
}
if (on('mobile')) {
  const { ctx, page, log } = await open(browser, '/world', { viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
  await page.waitForSelector('svg .stone', { timeout: 20000 }).catch(() => {});
  await page.waitForTimeout(1200);
  await page.screenshot({ path: OUT + '/05-mobile-chart.png' });
  report('mobile', log); await ctx.close();
}
if (on('top')) {
  const { ctx, page, log } = await open(browser, '/world?view=3d' + Q, VP);
  await ready(page, 5000);
  await page.evaluate(() => document.querySelector('.host').__sandtable.setOrbitNow(0, 80, 1));
  await page.waitForTimeout(1500);
  await page.screenshot({ path: OUT + '/dbg-top.png' });
  await ctx.close();
}
if (on('blocked')) {
  // decode blocked (CSP without wasm): the page must land on the Guild chart cleanly
  const { ctx, page, log } = await open(browser, '/world?view=3d' + Q, VP);
  await page.close();
  const p2 = await ctx.newPage();
  await p2.addInitScript(() => { WebAssembly.instantiate = () => Promise.reject(new Error('blocked by CSP')); WebAssembly.compile = () => Promise.reject(new Error('blocked by CSP')); });
  await p2.goto(BASE + '/world?view=3d' + Q);
  await p2.waitForSelector('svg .stone', { timeout: 60000 });
  console.log('blocked -> chart', await p2.evaluate(() => ({ notice: document.querySelector('.notice')?.textContent, canvas: document.querySelectorAll('canvas').length })));
  await ctx.close();
}
if (on('lowtilt')) {
  const { ctx, page } = await open(browser, '/world?view=3d' + Q, VP);
  await ready(page, 5000);
  for (const [az, t] of [[45, 22], [135, 22], [225, 22], [315, 22], [180, 60]]) {
    await page.evaluate(([a, b]) => document.querySelector('.host').__sandtable.setOrbitNow(a, b, 1), [az, t]);
    await page.waitForTimeout(900);
    await page.locator('.host').screenshot({ path: OUT + `/dbg-rim-${az}-${t}.png` });
  }
  await ctx.close();
}
if (on('tod')) {
  for (const [n, h] of (process.env.TODS ? process.env.TODS.split(',') : ['dawn', 'day', 'dusk', 'night']).map((n) => [n, { dawn: 6.5, day: 12.5, dusk: 18.5, night: 23 }[n]])) {
    const { ctx, page, log } = await open(browser, '/world?view=3d&hour=' + h, VP);
    await ready(page, 5000);
    await page.screenshot({ path: OUT + `/tod-${n}.png` });
    report('tod-' + n, log); await ctx.close();
  }
}
if (on('cap')) {
  const { ctx, page, log } = await open(browser, '/world?view=3d&hour=12.5', VP);
  await ready(page, 5000);
  await page.evaluate(() => document.querySelector('.host').__sandtable.lookAtCm(0, 3000, 2.6, 55));
  await page.waitForTimeout(1500);
  await page.screenshot({ path: OUT + '/07-polar-cap.png' });
  console.log('hires requested', JSON.stringify(log.net.filter((u) => /hires/.test(u))));
  await ctx.close();
}
if (on('rings')) {
  for (const v of ['a', 'b', 'c']) for (const [n, h] of [['day', 12.5], ['night', 23]]) {
    const { ctx, page, log } = await open(browser, `/world?view=3d&rings=${v}&hour=${h}`, VP);
    await ready(page, 5500);
    await page.screenshot({ path: OUT + `/rings-${v}-${n}.png` });
    // close-up: Hagga, then a dungeon
    for (const [id, tag] of [['hagga-basin', 'hagga'], ['testing-station-no-195', 'dungeon']]) {
      await page.evaluate((i) => { const e = document.querySelector('.host').__sandtable; e.lookAtStone ? e.lookAtStone(i, 7, 50) : 0; }, id);
      await page.waitForTimeout(900);
      await page.locator('.host').screenshot({ path: OUT + `/rings-${v}-${n}-close-${tag}.png` });
    }
    report(`rings-${v}-${n}`, log); await ctx.close();
  }
}
if (on('pick')) {
  // hover must name the stone under the pointer: page scrolled 300 px, DPR 1.5, rotated and low tilt
  const ctx = await browser.newContext({ viewport: { width: 1500, height: 760 }, deviceScaleFactor: 1.5 });
  const page = await ctx.newPage();
  await page.goto(BASE + '/world?view=3d&hour=12.5');
  await ready(page, 5000);
  const names = { 'ruins-of-tsimpo': 'Ruins of Tsimpo', 'testing-station-no-195': 'Testing Station No. 195', 'neo-carthag-arena': 'Neo Carthag Arena', 'harko-village': 'Harko Village', 'hagga-basin': 'Hagga Basin' };
  let bad = 0, total = 0;
  for (const [az, tilt, zoom] of [[0, 38, null], [90, 38, null], [0, 22, null], [90, 22, null], [200, 30, 2.2]]) {
    await page.evaluate(([a, t, z]) => { window.scrollTo(0, 0); document.querySelector('.host').__sandtable.setOrbitNow(a, t, z || undefined); }, [az, tilt, zoom]);
    await page.evaluate(() => window.scrollBy(0, 300));
    await page.waitForTimeout(700);
    for (const [id, name] of Object.entries(names)) {
      const p = await page.evaluate((i) => document.querySelector('.host').__sandtable.plateScreen(i), id);
      const box = await page.locator('.host').boundingBox();
      if (!p || p.x < 0 || p.y < 0 || p.x > box.width || p.y > box.height) continue;   // off screen in this view
      await page.mouse.move(box.x + p.x, box.y + p.y);
      await page.waitForTimeout(250);
      const got = await page.evaluate(() => document.querySelector('.card h2')?.textContent || '');
      total++;
      // Neo Carthag Arena sits 37 m behind Harko; Harko's taller miniature can genuinely hide its disc centre
      if (id === 'neo-carthag-arena' && got === 'Harko Village') { console.log('occluded by Harko miniature (as rendered)', az, tilt); continue; }
      if (got.toLowerCase() !== name.toLowerCase()) { bad++; console.log('MISPICK', az, tilt, id, '->', got); }
    }
  }
  console.log(`pick: ${total - bad}/${total} correct (scrolled 300px, DPR 1.5)`);
  await page.screenshot({ path: OUT + '/dbg-pick.png' });
  await ctx.close();
}
if (on('keyboard')) {
  const { ctx, page, log } = await open(browser, '/world?view=3d' + Q, VP);
  await ready(page, 2500);
  await page.focus('.table');
  const seen = [];
  for (let i = 0; i < 30; i++) {
    await page.keyboard.press('ArrowRight'); await page.waitForTimeout(150);
    const t = await page.evaluate(() => document.querySelector('.sr')?.textContent || '');
    seen.push(t.split('.')[0]);
    if (/Hagga Basin/.test(t)) break;
  }
  console.log('cycle', JSON.stringify(seen));
  await page.keyboard.press('Enter'); await page.waitForTimeout(800);
  console.log('chooser', await page.evaluate(() => !!document.querySelector('.chooser')));
  await page.screenshot({ path: OUT + '/06-keyboard-chooser.png' });
  await page.keyboard.press('Escape'); await page.waitForTimeout(300);
  console.log('chooser after esc', await page.evaluate(() => !!document.querySelector('.chooser')));
  await page.keyboard.press('Enter'); await page.waitForTimeout(500);
  await page.getByRole('button', { name: /Amtal/ }).click();
  await page.waitForTimeout(2500);
  console.log('url', page.url());
  report('keyboard', log); await ctx.close();
}
await browser.close();
