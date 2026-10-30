// Holo storm gate (2.26.1): the War-Table storm draws ONLY from a live position.
// Replays DD /live payloads with a recent spawn, a passed ETA, a fresh scan with no
// centre, storm_state 'clear', and a real centre, and reads what the holo drew.
// Usage: QA_OUT=<dir> PW_DIR=<dir> node storm-gate.mjs
import { writeFileSync, mkdirSync } from 'node:fs';
import { launch, open, toHolo, BASE } from './lib.mjs';
const OUT = process.env.QA_OUT || '.';
mkdirSync(OUT, { recursive: true });
const ago = (m) => new Date(Date.now() - m * 60000).toISOString();
const cases = {
  'recent-spawn': { last_spawn_utc: ago(3), next_eta_utc: ago(4) },
  'eta-passed': { next_eta_utc: ago(5), last_spawn_utc: ago(240) },
  'scan-no-centre': { storm_sector: 'E5', storm_scanned_utc: ago(1), last_spawn_utc: ago(2) },
  'clear': { storm_state: 'clear', storm_sector: 'E5', storm_scanned_utc: ago(1), center_nx: 500, center_ny: 450, radius_nr: 150 },
  'positioned': { storm_state: 'active', storm_sector: 'E5', storm_scanned_utc: ago(1), center_nx: 500, center_ny: 450, radius_nr: 150, heading_yaw: 30 },
};
const want = { 'recent-spawn': false, 'eta-passed': false, 'scan-no-centre': false, clear: false, positioned: true };
const R = { fail: [] };
const b = await launch();
for (const [name, info] of Object.entries(cases)) {
  const { page, ctx } = await open(b, '/', {});
  const body = JSON.stringify({ spice: { dimensions: {} }, worms: { dimensions: {} }, sandstorm: { available: true, dimensions: { 0: info, 1: info } } });
  await page.route('**/portal/maps/deep-desert/live*', (r) => r.fulfill({ status: 200, contentType: 'application/json', body }));
  await page.goto(BASE + '/maps/deep-desert?inst=pvp', { waitUntil: 'load' });
  await page.waitForTimeout(3000);
  if (!(await page.evaluate(() => !!document.querySelector('.holo-canvas.live')))) await toHolo(page, 16000);
  await page.waitForFunction(() => document.querySelector('.holo-canvas')?.__holoQa?.storm, null, { timeout: 60000 });
  await page.waitForTimeout(3000);
  R[name] = await page.evaluate(() => document.querySelector('.holo-canvas').__holoQa.storm());
  R[name].board = await page.evaluate(() => !!document.querySelector('.map-storm-layer'));
  if (R[name].visible !== want[name]) R.fail.push(`${name}: holo storm visible=${R[name].visible}, want ${want[name]}`);
  if (R[name].board !== want[name]) R.fail.push(`${name}: board storm drawn=${R[name].board}, want ${want[name]}`);
  await ctx.close();
}
await b.close();
R.pass = R.fail.length === 0;
writeFileSync(`${OUT}/_storm-gate.json`, JSON.stringify(R, null, 1));
console.log(JSON.stringify(R, null, 1));
process.exit(R.pass ? 0 : 1);
