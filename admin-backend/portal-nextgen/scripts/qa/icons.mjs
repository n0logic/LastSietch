// Holo marker icons: default, zoomed into a dense area, Deep Desert, a hub.
// Run: PW_DIR=<dir with playwright-core> QA_OUT=/path node icons.mjs (preview on :4173)
import { launch, open, toHolo, OUT, SUF } from './lib.mjs';
const b = await launch();
const wheel = async (page, n, dy = -300) => { await page.mouse.move(800, 560); for (let i = 0; i < n; i++) { await page.mouse.wheel(0, dy); await page.waitForTimeout(250); } await page.waitForTimeout(1500); };
for (const [key, name] of (process.env.ONLY ? [[process.env.ONLY, process.env.ONLY]] : [['hagga', 'hagga'], ['deep-desert', 'dd'], ['arrakeen', 'arrakeen']])) {
  const { page, log } = await open(b, `/maps/${key}`);
  await page.waitForTimeout(2500); await toHolo(page, 15000); await page.mouse.move(1500, 100);
  await page.screenshot({ path: `${OUT}/${name}-holo-default${SUF}.png` });
  if (key !== 'arrakeen') {
    await wheel(page, 4); await page.screenshot({ path: `${OUT}/${name}-holo-zoom-mid${SUF}.png` });
    await wheel(page, 6); await page.screenshot({ path: `${OUT}/${name}-holo-zoom-close${SUF}.png` });
  }
  console.log(key, JSON.stringify({ pageerrors: log.errors.slice(0, 5), consoleErrors: log.console.filter((l) => /error|shader|HTTP|GL/i.test(l) && !/\/me|\/stream|\/waypoints|vehicles/.test(l)).slice(0, 6), atlas: log.net.filter((u) => /map-icons/.test(u)) }));
  await page.context().close();
}
await b.close();
