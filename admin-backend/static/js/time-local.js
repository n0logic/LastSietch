/* ============================================================================
 * Local-timezone formatting for panel timestamps.
 *
 * Server time is UTC (canonical, for logs/storage). Presentation converts to the
 * VIEWER's local zone here — the browser knows the user's timezone, so the
 * server never has to guess. Bare timestamps with no zone designator are treated
 * as UTC (the server's zone), NOT local, which avoids the classic "naive ISO
 * string parsed as local time" off-by-hours bug.
 *
 * API (global):
 *   fmtLocal(utc, opts?)   -> "Jun 10, 2026, 2:31 PM" in local zone
 *   fmtLocalDate(utc)      -> local date only
 *   utcLabel(utc)          -> "2026-06-10 19:31 UTC" (for hover tooltips)
 *   hydrateLocalTimes(root?)-> fill <time data-utc="..."> elements; idempotent,
 *                             call again after inserting dynamic content.
 * ============================================================================ */
(function () {
  function toDate(s) {
    if (s === null || s === undefined || s === '') return null;
    if (typeof s === 'number') return new Date(s);
    var str = String(s).trim().replace(' ', 'T');
    // No trailing Z and no ±hh:mm offset -> assume the server's UTC.
    if (!/[Zz]$/.test(str) && !/[+-]\d\d:?\d\d$/.test(str)) str += 'Z';
    var d = new Date(str);
    return isNaN(d.getTime()) ? null : d;
  }

  var DEFAULT_OPTS = {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: 'numeric', minute: '2-digit',
  };

  function fmtLocal(s, opts) {
    var d = toDate(s);
    if (!d) return '';
    try {
      return d.toLocaleString(undefined, opts || DEFAULT_OPTS);
    } catch (e) {
      return d.toLocaleString();
    }
  }

  function fmtLocalDate(s) {
    var d = toDate(s);
    return d ? d.toLocaleDateString() : '';
  }

  function utcLabel(s) {
    var d = toDate(s);
    return d ? d.toISOString().replace('T', ' ').replace(/\..*$/, '') + ' UTC' : '';
  }

  function hydrateLocalTimes(root) {
    var scope = root || document;
    var nodes = scope.querySelectorAll('time[data-utc]');
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      var utc = n.getAttribute('data-utc');
      if (!utc) continue;
      var opts = n.hasAttribute('data-date-only')
        ? { year: 'numeric', month: 'short', day: 'numeric' }
        : null;
      var local = fmtLocal(utc, opts);
      if (!local) continue;
      n.textContent = local;
      n.setAttribute('datetime', utc);
      if (!n.title) n.title = utcLabel(utc);
    }
  }

  window.fmtLocal = fmtLocal;
  window.fmtLocalDate = fmtLocalDate;
  window.utcLabel = utcLabel;
  window.hydrateLocalTimes = hydrateLocalTimes;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { hydrateLocalTimes(); });
  } else {
    hydrateLocalTimes();
  }
})();
