/* Last Sietch portal — public Server page.
 * Ticks every [data-end-utc] countdown box (Landsraad term timer + next Deep
 * Desert reset) from the served end timestamp; no re-fetch. Vanilla, no deps.
 * Same bare-timestamp = UTC convention as time-local.js / the landsraad board
 * countdown. CSP-safe external file (script-src 'self'); no inline script.
 * Each box: <div data-end-utc="..." data-ended-label="..."><span data-countdown-out>.
 */
(function () {
  'use strict';

  function toUtcDate(s) {
    if (!s) return null;
    var str = String(s).trim().replace(' ', 'T');
    if (!/[Zz]$/.test(str) && !/[+-]\d\d:?\d\d$/.test(str)) str += 'Z';
    var d = new Date(str);
    return isNaN(d.getTime()) ? null : d;
  }

  function fmt(ms) {
    if (ms <= 0) return null;
    var s = Math.floor(ms / 1000);
    var d = Math.floor(s / 86400); s -= d * 86400;
    var h = Math.floor(s / 3600);  s -= h * 3600;
    var m = Math.floor(s / 60);    s -= m * 60;
    if (d > 0) return d + 'd ' + h + 'h ' + m + 'm';
    if (h > 0) return h + 'h ' + m + 'm ' + s + 's';
    return m + 'm ' + s + 's';
  }

  var boxes = document.querySelectorAll('[data-end-utc]');
  if (!boxes.length) return;

  function tickAll() {
    var live = 0;
    for (var i = 0; i < boxes.length; i++) {
      var box = boxes[i];
      var out = box.querySelector('[data-countdown-out]');
      if (!out) continue;
      var end = toUtcDate(box.getAttribute('data-end-utc'));
      if (!end) { out.textContent = ''; continue; }
      var left = fmt(end.getTime() - Date.now());
      if (left === null) {
        out.textContent = box.getAttribute('data-ended-label') || 'now';
        box.classList.add('is-ended');
      } else {
        out.textContent = left;
        live++;
      }
    }
    return live;
  }

  tickAll();
  var timer = window.setInterval(function () {
    if (tickAll() === 0) window.clearInterval(timer);
  }, 1000);
})();
