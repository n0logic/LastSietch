/* theme-boot.js — no-FOUC theme bootstrap. Loaded SYNCHRONOUSLY in <head>
 * (before first paint). External file because the portal CSP is script-src
 * 'self' — inline scripts never execute, which silently disabled the old
 * inline version of this bootstrap (themes reset to night on every page).
 * Accepts: night (default), day, atreides, harkonnen. */
(function () {
  try {
    var VALID = {night:1,day:1,atreides:1,harkonnen:1};
    var t = localStorage.getItem('ls-portal-theme');
    if (!VALID[t]) {
      t = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'day' : 'night';
    }
    document.documentElement.setAttribute('data-theme', t);
  } catch (e) {}
})();
