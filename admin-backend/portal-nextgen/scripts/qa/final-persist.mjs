// Final-gate: the Light choice persists between /maps (War-Table) and /world (Sand Table), plus cluster zoom shots.
import { launch, open, toHolo as toHolo1, BASE } from './lib.mjs';
const OUT = process.env.QA_OUT || '.';
const b = await launch(); const R = {};
const toHolo = async (page) => { page.setDefaultTimeout(150000); for (let i = 0; i < 3; i++) { await toHolo1(page, 12000); if (await page.evaluate(() => !!document.querySelector('.holo-canvas.live'))) return; } };
const onBtn = (page, sel) => page.evaluate((s) => document.querySelector(s)?.textContent.trim() || null, sel);
{
  const { page, ctx } = await open(b, '/maps/hagga'); await page.waitForTimeout(2000); await toHolo(page);
  await page.locator('.tod-btn', { hasText: /^night$/i }).click(); await page.waitForTimeout(800);
  await page.goto(BASE + '/world?view=3d'); await page.waitForSelector('.seg.small button.on', { timeout: 60000 }); await page.waitForTimeout(1500);
  R.mapsNight_then_world = await onBtn(page, '.seg.small button.on');
  await page.locator('.seg.small button', { hasText: /^dusk$/i }).click(); await page.waitForTimeout(500);
  await page.goto(BASE + '/maps/hagga'); await page.waitForTimeout(2000); await toHolo(page);
  R.worldDusk_then_maps = await onBtn(page, '.tod-btn.on');
  R.mapsTodAttr = await page.evaluate(() => document.querySelector('.holo-canvas')?.dataset.tod);
  await ctx.close();
}
{
  const { page, ctx } = await open(b, '/maps/hagga?hour=12'); await page.waitForTimeout(2000); await toHolo(page);
  await page.mouse.move(1500, 80);
  const click = (l) => page.evaluate((x) => document.querySelector(`[aria-label="${x}"]`)?.click(), l);
  await click('Zoom in'); await page.waitForTimeout(1500); await click('Zoom in'); await page.waitForTimeout(2500);
  await page.screenshot({ path: `${OUT}/hagga-clusters-mid.png`, clip: { x: 410, y: 291, width: 572, height: 571 } });
  for (let i = 0; i < 4; i++) { await click('Zoom in'); await page.waitForTimeout(700); } await page.waitForTimeout(2500);
  await page.screenshot({ path: `${OUT}/hagga-clusters-close.png`, clip: { x: 410, y: 291, width: 572, height: 571 } });
  await ctx.close();
}
console.log(JSON.stringify(R, null, 1)); await b.close();
