// 2.26.0 hub diorama terrain: Arrakeen + Harko Village in the holo (default and rotated), console + network.
import { launch, open, toHolo, OUT, SUF } from './lib.mjs';
const b = await launch();
for (const key of ['arrakeen', 'harko-village']) {
  const { page, log } = await open(b, `/maps/${key}`);
  await page.waitForTimeout(2500); await toHolo(page, 15000); await page.waitForTimeout(4000);
  await page.screenshot({ path: `${OUT}/hub-${key}-holo${SUF}.png` });
  await page.keyboard.press('e'); await page.waitForTimeout(600); await page.keyboard.press('e'); await page.waitForTimeout(1500);
  await page.screenshot({ path: `${OUT}/hub-${key}-holo-rot90${SUF}.png` });
  console.log(key, JSON.stringify({ pageerrors: log.errors.slice(0, 5), consoleErrors: log.console.filter((l) => /error|shader|HTTP/i.test(l) && !/\/me|\/stream|\/waypoints|vehicles/.test(l)).slice(0, 6),
    terrain: [...new Set(log.net.filter((u) => /\/terrain\/|img\/maps/.test(u)))] }));
  await page.context().close();
}
await b.close();
