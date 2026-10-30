// Run against `npm run preview:review`. Needs playwright-core (PW_DIR = a dir with it in node_modules), a Chromium
// (CHROME=...), python3 with numpy/scipy/Pillow for find_markers.py. Writes PNGs + _results-*.json into QA_OUT.
// Shared helpers for the 2.26.0 holo QA run (local preview only; never touches production).
import { createRequire } from 'node:module';
const require = createRequire((process.env.PW_DIR || process.cwd()) + '/');
export const { chromium } = require('playwright-core');
export const BASE = process.env.QA_BASE || 'http://127.0.0.1:4173';   // QA_BASE: a second preview on another port
export const OUT = process.env.QA_OUT || 'qa-out';
const EXE = process.env.CHROME || process.env.HOME + '/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome';

export async function launch() {
  return chromium.launch({ executablePath: EXE, args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist', '--enable-precise-memory-info'] });
}

// Opens a page with console/pageerror/network capture. Harness-only 404s (the preview has no
// /me, /stream, /waypoints, /vehicles routes) are tagged so they do not count as app errors.
export async function open(browser, path, o = {}) {
  const ctx = await browser.newContext({ viewport: o.viewport || { width: 1600, height: 1000 }, reducedMotion: o.reducedMotion, hasTouch: o.hasTouch, isMobile: o.isMobile, deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  // Records the largest indexed draw (terrain mesh index count: 511 seg = 1566726, 383 = 880134, 95 = 54150)
  await page.addInitScript(() => {
    window.__maxIdx = 0;
    const wrap = (P) => { if (!P) return; const f = P.drawElements; P.drawElements = function (m, c, ...r) { if (c > window.__maxIdx) window.__maxIdx = c; return f.call(this, m, c, ...r); }; };
    wrap(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);
    const P = window.WebGL2RenderingContext && WebGL2RenderingContext.prototype;
    if (P) { const g = P.drawElementsInstanced; P.drawElementsInstanced = function (m, c, ...r) { if (c > window.__maxIdx) window.__maxIdx = c; return g.call(this, m, c, ...r); }; }
  });
  const log = { console: [], errors: [], net: [] };
  page.on('console', (m) => { if (['error', 'warning'].includes(m.type())) log.console.push(m.type() + ': ' + m.text()); });
  page.on('pageerror', (e) => log.errors.push(e.message));
  page.on('request', (r) => log.net.push(new URL(r.url()).pathname + new URL(r.url()).search));
  page.on('response', (r) => { if (r.status() >= 400) log.console.push('HTTP ' + r.status() + ' ' + new URL(r.url()).pathname); });
  await page.goto(BASE + path, { waitUntil: 'load' });
  return { ctx, page, log };
}

export async function toHolo(page, ms = 12000) {
  await page.getByRole('button', { name: /holo table/i }).first().click();
  await page.waitForTimeout(ms);
}
export const heading = (page) => page.evaluate(() => { const d = document.querySelector('[aria-label^="Heading"]'); return d ? d.getAttribute('aria-label') : null; });

export const SUF = process.env.QA_SUFFIX || '';
