// Hover then click a cluster on the Hagga holo: tooltip card text, and the camera zooms in.
import { launch, open, toHolo } from './lib.mjs';
const b = await launch();
const { page, log } = await open(b, '/maps/hagga?hour=12');
for (let i = 0; i < 3; i++) { await toHolo(page, 14000); if (await page.evaluate(() => !!document.querySelector('.holo-canvas.live'))) break; }
const R = {};
const tip = () => page.evaluate(() => document.querySelector('.tooltip')?.textContent.trim() || null);
let found = null;
outer: for (let y = 420; y < 780; y += 12) for (let x = 440; x < 960; x += 12) {
  await page.mouse.move(x, y);
  const t = await tip();
  if (t && /Zoom in to see each one/.test(t)) { found = { x, y, t }; break outer; }
}
R.hover = found;
if (found) {
  const before = await page.screenshot();
  await page.mouse.click(found.x, found.y);
  await page.waitForTimeout(2500);
  R.afterClickTooltip = await tip();
  const after = await page.screenshot();
  R.viewChanged = !before.equals(after);
  await page.screenshot({ path: (process.env.QA_OUT || '.') + '/hagga-cluster-click.png' });
}
R.errors = log.errors;
console.log(JSON.stringify(R, null, 1));
await b.close();
