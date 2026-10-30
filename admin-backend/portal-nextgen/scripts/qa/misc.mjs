// DD / hub / ?holoLayout / reduced-motion / mobile scenarios for the 2.26.0 holo QA.
import { writeFileSync } from 'node:fs';
import { launch, open, toHolo, heading, OUT, SUF } from './lib.mjs';
const R = {};
const H = (page) => heading(page).then((s) => s && +s.match(/Heading (\d+)/)[1]);
const press = (page, sel) => page.evaluate((q) => document.querySelector(q).click(), sel);
const info = (page) => page.evaluate(() => ({
  maxIndexCount: window.__maxIdx,
  gridLabels: Array.from(document.querySelectorAll('*')).filter((e) => e.children.length === 0 && /^[A-I][1-9]$/.test((e.textContent || '').trim())).length,
  layoutChip: document.querySelector('.layout-chip')?.textContent?.trim() || null,
  heap: performance.memory ? Math.round(performance.memory.usedJSHeapSize / 1048576) + ' MB' : 'n/a',
}));
const nets = (log) => [...new Set(log.net.filter((u) => /^\/(terrain|assets)\//.test(u) || /amtal|overmap|sand|bands|world/i.test(u) || /^\/(api|portal)\//.test(u) && !/maps\/[^/]+\/(data|live|players|me|waypoints|stream)|auth|server|features|characters|messages|market|settings|me$|dune\/(status|positions|vehicles)/.test(u)))];
const clean = (log) => ({ console: log.console.filter((l) => !/\/(me|stream|waypoints|vehicles)\b|Failed to load resource/.test(l)), errors: log.errors });
const b = await launch();

// Hagga with ?holoLayout=3 (must be ignored, no chip, no layout_3 fetch)
{
  const { page, log } = await open(b, '/maps/hagga?holoLayout=3'); await page.waitForTimeout(2500); await toHolo(page, 15000);
  R.haggaHoloLayout3 = { ...(await info(page)), net: nets(log), fetchedLayout3: log.net.some((u) => /layout_3/.test(u)), ...clean(log) };
  await page.screenshot({ path: `${OUT}/hagga-holoLayout3-ignored${SUF}.png` }); await page.context().close();
}
// Deep Desert default + rotated
{
  const { page, log } = await open(b, '/maps/deep-desert'); await page.waitForTimeout(2500); await toHolo(page, 15000);
  await page.mouse.move(1300, 150);
  R.ddDefault = { ...(await info(page)), heading: await H(page) };
  await page.screenshot({ path: `${OUT}/dd-holo-default${SUF}.png` });
  for (let i = 0; i < 3; i++) { await page.keyboard.press('e'); await page.waitForTimeout(500); }
  await page.mouse.move(700, 580); await page.mouse.down({ button: 'right' }); await page.mouse.move(700, 640, { steps: 8 }); await page.mouse.up({ button: 'right' }); await page.waitForTimeout(2500);
  await page.mouse.move(1300, 150); await page.waitForTimeout(500);
  R.ddRotated = { heading: await H(page) };
  await page.screenshot({ path: `${OUT}/dd-holo-rotated${SUF}.png` });
  R.ddNet = nets(log); R.ddFetchedHagga = log.net.some((u) => /terrain\/hagga/.test(u)); Object.assign(R.ddDefault, clean(log));
  await page.context().close();
}
// DD with ?holoLayout=3 (applies, chip shown)
{
  const { page, log } = await open(b, '/maps/deep-desert?holoLayout=3'); await page.waitForTimeout(2500); await toHolo(page, 15000);
  R.ddHoloLayout3 = { ...(await info(page)), fetchedLayout3: log.net.filter((u) => /layout_3/.test(u)), ...clean(log) };
  await page.screenshot({ path: `${OUT}/dd-holoLayout3-applied${SUF}.png` }); await page.context().close();
}
// Hub: Arrakeen
{
  const { page, log } = await open(b, '/maps/arrakeen'); await page.waitForTimeout(2500); await toHolo(page, 12000);
  await page.mouse.move(1300, 150);
  R.hub = { ...(await info(page)), net: nets(log), terrainFetches: log.net.filter((u) => /^\/terrain\//.test(u)), ...clean(log) };
  await page.screenshot({ path: `${OUT}/hub-holo${SUF}.png` }); await page.context().close();
}
// Reduced motion
{
  const { page, log } = await open(b, '/maps/hagga', { reducedMotion: 'reduce' }); await page.waitForTimeout(2500); await toHolo(page, 15000);
  await page.mouse.move(1300, 150);
  R.reducedMotion = { ...(await info(page)), net: nets(log) };
  await page.keyboard.press('e'); await page.waitForTimeout(120);
  R.reducedMotion.headingAfterE_120ms = await H(page);
  await page.keyboard.press('e'); await page.waitForTimeout(120);
  R.reducedMotion.headingAfter2E_120ms = await H(page);
  await page.screenshot({ path: `${OUT}/hagga-reduced-motion${SUF}.png` });
  Object.assign(R.reducedMotion, clean(log)); await page.context().close();
}
// Normal-motion snap timing for contrast (heading mid-ease right after a press)
{
  const { page } = await open(b, '/maps/hagga'); await page.waitForTimeout(2500); await toHolo(page, 12000);
  await page.keyboard.press('e'); await page.waitForTimeout(40); R.normalMotionHeadingAfterE_40ms = await H(page);
  await page.context().close();
}
// Mobile 390x844 touch
{
  const { page, log, ctx } = await open(b, '/maps/hagga', { viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
  await page.waitForTimeout(3000);
  const toggle = page.getByRole('button', { name: /holo table/i }).first();
  R.mobile = { toggleVisible: await toggle.isVisible().catch(() => false), toggleDisabled: await toggle.isDisabled().catch(() => null) };
  if (R.mobile.toggleVisible) { await toggle.tap().catch(async () => { await toggle.click(); }); }
  await page.waitForTimeout(15000);
  R.mobile.info = await info(page); R.mobile.heading = await H(page);
  R.mobile.boxes = await page.evaluate(() => { const r = (s) => { const e = document.querySelector(s); if (!e) return null; const b = e.getBoundingClientRect(); return { x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height) }; };
    return { compass: r('[aria-label="Rotate view"]'), zoom: r('.zoom-cluster'), fab: r('.fab'), viewport: r('[aria-label$="board: drag to pan, pinch or buttons to zoom, right-drag or two-finger twist to rotate"]'), stuck: r('[class*="stuck"]') }; });
  await page.screenshot({ path: `${OUT}/mobile-hagga${SUF}.png` });
  // two-finger twist with CDP touch events (about the viewport centre)
  const vb = R.mobile.boxes.viewport;
  if (vb) {
    const cdp = await ctx.newCDPSession(page);
    const cx = vb.x + vb.w / 2, cy = vb.y + vb.h / 2, r0 = 60;
    const pts = (a) => [{ x: cx + r0 * Math.cos(a), y: cy + r0 * Math.sin(a), id: 1 }, { x: cx - r0 * Math.cos(a), y: cy - r0 * Math.sin(a), id: 2 }];
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: pts(0) });
    for (let i = 1; i <= 20; i++) { await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: pts((i / 20) * (Math.PI / 3)) }); await page.waitForTimeout(40); }
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    await page.waitForTimeout(1500);
    R.mobile.headingAfterTwist60degCW = await H(page);
    await page.screenshot({ path: `${OUT}/mobile-hagga-twisted${SUF}.png` });
  }
  R.mobile.net = nets(log); R.mobile.drape = log.net.filter((u) => /HaggaBasin/.test(u)); Object.assign(R.mobile, clean(log));
  await page.context().close();
}
writeFileSync(`${OUT}/_results-misc${SUF}.json`, JSON.stringify(R, null, 1));
console.log(JSON.stringify(R, null, 1));
await b.close();
