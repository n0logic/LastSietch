// Marker hover/select spot-check at a rotated heading + tilt-clamp image diffs.
import { writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { launch, open, toHolo, heading, OUT } from './lib.mjs';
const R = {};
const tmp = process.env.PW_DIR;
const b = await launch();
const { page } = await open(b, '/maps/hagga');
await page.waitForTimeout(2500);
await toHolo(page, 16000);
const press = (sel) => page.evaluate((q) => document.querySelector(q).click(), sel);
const layersOff = async () => { await page.getByRole('button', { name: /^none$/i }).first().click(); await page.waitForTimeout(1500); };
const layersChoam = async () => { await page.getByRole('button', { name: /^none$/i }).first().click(); await page.waitForTimeout(500); await page.getByText('CHOAM Exchange', { exact: true }).first().click(); await page.waitForTimeout(2500); };
const find = async (name) => { await page.mouse.move(1300, 150); await page.waitForTimeout(400);
  const on = `${tmp}/${name}-on.png`, off = `${tmp}/${name}-off.png`;
  await page.screenshot({ path: on }); await layersOff(); await page.screenshot({ path: off }); await layersChoam();
  return JSON.parse(execFileSync('python3', ['find_markers.py', on, off]).toString()); };
const tip = () => page.evaluate(() => document.querySelector('.tooltip')?.textContent?.trim() || null);
const orbit = async (dx, dy) => { await page.mouse.move(700, 580); await page.mouse.down({ button: 'right' }); await page.mouse.move(700 + dx, 580 + dy, { steps: 12 }); await page.mouse.up({ button: 'right' }); await page.waitForTimeout(1500); };
const diff = (a, c) => JSON.parse(execFileSync('python3', ['-c', `import sys,json;import numpy as np;from PIL import Image;A=np.asarray(Image.open(sys.argv[1]).convert('L')).astype(int)[291:862,410:982];B=np.asarray(Image.open(sys.argv[2]).convert('L')).astype(int)[291:862,410:982];print(json.dumps(round(float(abs(A-B).mean()),3)))`, a, c]).toString());
const snap = async (n) => { const p = `${tmp}/${n}.png`; await page.screenshot({ path: p }); return p; };

// Only CHOAM Exchange markers (4 of them, cyan)
await page.getByRole('button', { name: /^none$/i }).first().click(); await page.waitForTimeout(800);
await page.getByText('CHOAM Exchange', { exact: true }).first().click(); await page.waitForTimeout(3000);
await page.mouse.move(1300, 150); await page.waitForTimeout(500);
for (const [label, steps, tiltDy] of [['h0', 0, 0], ['h135', 3, 0], ['h135-t60', 0, 80]]) {
  if (steps) { for (let i = 0; i < steps; i++) { await page.keyboard.press('e'); await page.waitForTimeout(500); } await page.waitForTimeout(800); }
  if (tiltDy) { await orbit(0, tiltDy); }
  await page.mouse.move(1300, 150); await page.waitForTimeout(800);
  const pts = await find('m-' + label);
  const res = [];
  for (const [x, y, n] of pts.slice(0, 6)) {
    await page.mouse.move(x, y); await page.waitForTimeout(600);
    const t = await tip();
    await page.mouse.move(x + 35, y + 35); await page.waitForTimeout(500);
    const off = await tip();
    let nearest = null;
    if (!t) { // where does the hover test actually respond around the drawn marker?
      let best = 1e9;
      for (let dy = -90; dy <= 90; dy += 10) for (let dx = -90; dx <= 90; dx += 10) {
        await page.mouse.move(x + dx, y + dy); await page.waitForTimeout(25);
        if (await tip()) { const d = Math.hypot(dx, dy); if (d < best) { best = d; nearest = { dx, dy }; } }
      }
    }
    res.push({ x, y, px: n, tipAtMarker: t, tipOffset35px: off, nearestHitOffsetPx: nearest });
  }
  R[label] = { heading: await heading(page), found: pts.length, res };
  await page.mouse.move(1300, 150);
  await page.screenshot({ path: `${OUT}/hit-${label}.png` });
}
// click select at the last marker position (heading 135 / tilt 60)
const pts = await find('m-final');
if (pts.length) { await page.mouse.click(pts[0][0], pts[0][1]); await page.waitForTimeout(800);
  R.clickPopup = await page.evaluate(() => document.querySelector('[class*="popup"]')?.textContent?.trim().slice(0, 160) || null);
  await page.screenshot({ path: `${OUT}/hit-click-popup.png` }); }

// tilt clamp: low
await page.keyboard.press('Escape');
await press('[aria-label="Reset to north up"]'); await page.waitForTimeout(1500);
await page.getByRole('button', { name: /^default$/i }).first().click(); await page.waitForTimeout(2500);
await page.mouse.move(1300, 150);
const a0 = await snap('t-a0'); const a1 = await snap('t-a1'); R.noiseFloor = diff(a0, a1);
await orbit(0, -50); const l1 = await snap('t-l1');       // 35 - 15 = 20 exactly
await orbit(0, -500); const l2 = await snap('t-l2');      // far past the clamp
R.tiltLow20_vs_overdrag = diff(l1, l2);
await orbit(0, 30); const l3 = await snap('t-l3');        // +9 deg -> 29
R.tiltLow20_vs_29 = diff(l1, l3);
await press('[aria-label="Reset to north up"]'); await page.waitForTimeout(1500);
await orbit(0, 150); const h1 = await snap('t-h1');       // 35 + 45 = 80
await orbit(0, 500); const h2 = await snap('t-h2');
R.tiltHigh80_vs_overdrag = diff(h1, h2);
await orbit(0, -30); const h3 = await snap('t-h3');       // 71
R.tiltHigh80_vs_71 = diff(h1, h3);
writeFileSync(`${OUT}/_results-hit.json`, JSON.stringify(R, null, 1));
console.log(JSON.stringify(R, null, 1));
await b.close();
