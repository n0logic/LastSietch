import { launch, open, toHolo, OUT, SUF } from './lib.mjs';
const b = await launch();
const { page, log } = await open(b, '/maps/hagga');
await page.waitForTimeout(2500); await toHolo(page, 16000);
await page.mouse.move(700, 580); await page.mouse.down({ button: 'right' }); await page.mouse.move(700, 720, { steps: 10 }); await page.mouse.up({ button: 'right' }); await page.waitForTimeout(2500);
await page.mouse.move(1300, 150); await page.waitForTimeout(500);
await page.screenshot({ path: `${OUT}/hagga-storm-top${SUF}.png` });
console.log(log.net.filter((u) => /live|storm|flipbook/.test(u)).slice(0, 8), log.console.filter((l) => !/Failed to load/.test(l)));
await b.close();
