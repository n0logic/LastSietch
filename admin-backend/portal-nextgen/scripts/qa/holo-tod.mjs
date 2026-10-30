// Run against `npm run preview:review`. War-Table time of day + marker clusters screenshots.
// QA_OUT = output dir. Env as scripts/qa/lib.mjs.
import { mkdirSync } from 'node:fs';
import { launch, open, toHolo as toHolo1, OUT } from './lib.mjs';
// the holo mount is idle-deferred and can lose a click race: retry until the canvas is live
async function toHolo(page, ms) {
  for (let i = 0; i < 3; i++) {
    await toHolo1(page, ms);
    if (await page.evaluate(() => !!document.querySelector('.holo-canvas.live'))) return;
  }
}
mkdirSync(OUT, { recursive: true });
const b = await launch();
const R = {};
const click = (page, label) => page.evaluate((l) => document.querySelector(`[aria-label="${l}"]`).click(), label);

// Hagga at each light, default view
for (const [h, name] of [[6.5, 'dawn'], [12, 'day'], [18.5, 'dusk'], [23, 'night']]) {
  const { page, log } = await open(b, '/maps/hagga?hour=' + h);
  await toHolo(page, 15000);
  await page.mouse.move(1500, 80);
  R['hagga-' + name] = await page.evaluate(() => document.querySelector('.holo-canvas')?.dataset.tod);
  await page.screenshot({ path: `${OUT}/hagga-${name}.png` });
  if (name === 'day') {
    // clusters: default, mid, close zoom
    await page.screenshot({ path: `${OUT}/hagga-clusters-default.png` });
    await click(page, 'Zoom in'); await page.waitForTimeout(1500);
    await click(page, 'Zoom in'); await page.waitForTimeout(2500);
    await page.screenshot({ path: `${OUT}/hagga-clusters-mid.png` });
    for (let i = 0; i < 5; i++) { await click(page, 'Zoom in'); await page.waitForTimeout(600); }
    await page.waitForTimeout(2500);
    await page.screenshot({ path: `${OUT}/hagga-clusters-close.png` });
  }
  R['errors-' + name] = log.errors;
  await page.context().close();
}
{
  const { page, log } = await open(b, '/maps/deep-desert?hour=23');
  await toHolo(page, 15000);
  await page.mouse.move(1500, 80);
  await page.screenshot({ path: `${OUT}/deep-desert-night.png` });
  R['errors-dd'] = log.errors;
  await page.context().close();
}
console.log(JSON.stringify(R, null, 1));
await b.close();
