/* ============================================================================
 * VC2 P1: Server > Monitor aggregator hydration.
 *
 * Polls /admin/api/dune/v2/server-monitor every 10s, fans the JSON envelope
 * out to per-panel render functions. Pure DOM update, no HTMX swap. A
 * singleflight `_inflight` guard prevents request pileup if a poll runs long.
 *
 * Relative timestamps (`data-ts="<epoch>"`) re-render every 10s independent
 * of the poll so the operator sees ageing data even mid-fetch.
 *
 * Action-stream auto-prepend honours a user-scrolled guard: if the operator
 * has scrolled away from the top of the stream we don't yank the viewport
 * back to the newest row.
 *
 * Reference: docs/dune-research/VC2-P1-EXECUTION-BRIEF.md §8.2.
 * ========================================================================= */
(function () {
  'use strict';

  var root = document.getElementById('v2-monitor');
  if (!root) return;

  var AGG_URL = root.getAttribute('data-aggregator-url')
              || '/admin/api/dune/v2/server-monitor';
  var AGG_INTERVAL = parseInt(root.getAttribute('data-poll-interval') || '10000', 10);
  var TICK_INTERVAL = 10000;
  var _inflight = false;
  var _lastData = null;

  /* --- Helpers ----------------------------------------------------------- */

  function $(id) { return document.getElementById(id); }

  function fmtRelative(epoch) {
    if (!epoch || epoch <= 0) return '--';
    var d = Math.floor(Date.now() / 1000) - epoch;
    if (d < 0) return 'now';
    if (d < 60)   return d + 's';
    if (d < 3600) return Math.floor(d / 60) + 'm ' + (d % 60) + 's';
    if (d < 86400) return Math.floor(d / 3600) + 'h ' + Math.floor((d % 3600) / 60) + 'm';
    return Math.floor(d / 86400) + 'd';
  }

  function fmtNumber(n) {
    if (n === null || n === undefined) return '--';
    if (typeof n !== 'number') return String(n);
    return n.toLocaleString();
  }

  function setText(id, text) {
    var el = $(id);
    if (el) el.textContent = (text === null || text === undefined || text === '') ? '--' : text;
  }

  function setState(id, state) {
    var el = $(id);
    if (el) el.setAttribute('data-state', state);
  }

  function setTs(id, epoch) {
    var el = $(id);
    if (!el) return;
    el.setAttribute('data-ts', String(epoch || 0));
    el.textContent = epoch ? ('refreshed ' + fmtRelative(epoch) + ' ago') : 'refreshed --';
  }

  function tickTimestamps() {
    var nodes = document.querySelectorAll('[data-ts]');
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      var ts = parseInt(el.getAttribute('data-ts'), 10);
      if (ts > 0) {
        var prefix = el.classList && el.classList.contains('monitor-panel__ts') ? 'refreshed ' : '';
        var suffix = prefix ? ' ago' : '';
        el.textContent = prefix + fmtRelative(ts) + suffix;
      }
    }
  }

  /* --- Hero panel -------------------------------------------------------- */

  function renderHeroPanelFromAgg(d) {
    var bg = d.battlegroup || {};
    setText('hero-bg-title', bg.title || bg.name || bg.serverDisplayName || (bg.available === false ? 'BG unavailable' : '--'));
    setText('hero-bg-region', bg.region || bg.serverRegion || '');
    setText('hero-bg-guid', bg.guid || bg.serverGuid || '');

    /* Funcom push pill. */
    var fp = d.funcom_push || {};
    var fpState = fp.available === false ? 'error' : (fp.diff_from_baseline && fp.diff_from_baseline.length ? 'warn' : 'ok');
    setState('hero-pill-funcom', fpState);
    setText('hero-pill-funcom-value', fp.last_ts ? fmtRelative(fp.last_ts) + ' ago' : (fp.available === false ? 'offline' : '--'));

    /* BGD RPC pill. */
    var rpc = d.bgd_rpc || {};
    var rpcState = rpc.available === false ? 'error' : (rpc.last_ts ? 'ok' : 'warn');
    setState('hero-pill-bgd', rpcState);
    setText('hero-pill-bgd-value', rpc.last_ts ? fmtRelative(rpc.last_ts) + ' ago' : (rpc.available === false ? 'offline' : '--'));

    /* Travel queue pill. */
    var tq = d.travel_queue || {};
    var depth = (tq.depth !== undefined) ? tq.depth : (typeof tq === 'number' ? tq : 0);
    var tqState = tq.available === false ? 'error' : (depth > 0 ? 'warn' : 'ok');
    setState('hero-pill-travel', tqState);
    setText('hero-pill-travel-value', tq.available === false ? 'offline' : (fmtNumber(depth) + ' queued'));

    /* Per-map tiles. */
    var grid = $('hero-maps');
    if (!grid) return;
    var maps = d.maps_summary || [];
    var empty = $('hero-maps-empty');
    if (!maps.length) {
      if (empty) empty.style.display = '';
      return;
    }
    if (empty) empty.style.display = 'none';

    /* Re-render only when the map set changes; otherwise just update values. */
    var existingKeys = {};
    for (var i = 0; i < grid.children.length; i++) {
      var c = grid.children[i];
      if (c.dataset && c.dataset.map) existingKeys[c.dataset.map] = c;
    }

    var seen = {};
    for (var j = 0; j < maps.length; j++) {
      var m = maps[j];
      seen[m.map] = true;
      var tile = existingKeys[m.map];
      if (!tile) {
        tile = document.createElement('div');
        tile.className = 'hero-map-tile';
        tile.dataset.map = m.map;
        tile.innerHTML =
          '<div class="hero-map-tile__name"><span class="hero-map-tile__status"></span><span class="hero-map-tile__name-text"></span></div>' +
          '<div class="hero-map-tile__row"><span>Players</span><span class="hero-map-tile__value hero-map-tile__value--players">--</span></div>' +
          '<div class="hero-map-tile__row"><span>PvE</span><span class="hero-map-tile__value hero-map-tile__value--pve">--</span></div>' +
          '<div class="hero-map-tile__row hero-map-tile__row--pvp"><span>PvP</span><span class="hero-map-tile__value">--</span></div>';
        grid.appendChild(tile);
      }
      tile.querySelector('.hero-map-tile__name-text').textContent = m.map;
      var statusDot = tile.querySelector('.hero-map-tile__status');
      statusDot.setAttribute('data-ready', m.ready === true ? 'true' : 'false');
      statusDot.setAttribute('data-alive', m.alive === true ? 'true' : 'false');
      tile.querySelector('.hero-map-tile__value--players').textContent = fmtNumber(m.players);
      tile.querySelector('.hero-map-tile__value--pve').textContent = fmtNumber(m.partition_pve);
      tile.querySelector('.hero-map-tile__row--pvp .hero-map-tile__value').textContent = fmtNumber(m.partition_pvp);
    }
    /* Remove tiles for maps no longer reported. */
    for (var key in existingKeys) {
      if (existingKeys.hasOwnProperty(key) && !seen[key]) {
        grid.removeChild(existingKeys[key]);
      }
    }

    /* Indicator: green if every map ready+alive, warn if any not, error if status itself missing. */
    var heroState = 'ok';
    if (bg.available === false) heroState = 'error';
    else {
      for (var k = 0; k < maps.length; k++) {
        if (maps[k].ready === false || maps[k].alive === false) { heroState = 'warn'; break; }
      }
    }
    setState('panel-hero-indicator', heroState);
    setTs('panel-hero-ts', d.generated_at);
  }

  /* --- Presence panel ---------------------------------------------------- */

  function renderPresencePanelFromAgg(d) {
    var samples = d.presence_1h || [];
    var current = 0;
    var peak = 0;
    var playhours = 0;

    /* Each sample is either {ts, count} from telemetry or just a number. */
    var values = [];
    for (var i = 0; i < samples.length; i++) {
      var s = samples[i];
      var v = (s && typeof s === 'object') ? (s.count || s.players || s.value || 0) : (typeof s === 'number' ? s : 0);
      values.push(v);
      if (v > peak) peak = v;
    }
    if (values.length) current = values[values.length - 1];

    /* Fallback to summing maps_summary players when telemetry empty. */
    if (!current && d.maps_summary) {
      var sumP = 0;
      for (var m = 0; m < d.maps_summary.length; m++) {
        if (typeof d.maps_summary[m].players === 'number') sumP += d.maps_summary[m].players;
      }
      current = sumP;
      if (sumP > peak) peak = sumP;
    }

    /* Approx 1h playhours = mean(samples) * 1h. */
    if (values.length) {
      var total = 0;
      for (var n = 0; n < values.length; n++) total += values[n];
      playhours = Math.round(total / values.length);
    }

    setText('presence-current', fmtNumber(current));
    setText('presence-peak', fmtNumber(peak));
    setText('presence-playhours', fmtNumber(playhours));

    /* Sparkline polyline. */
    var spark = $('presence-sparkline');
    if (spark) {
      var line = $('presence-sparkline-line');
      var fill = $('presence-sparkline-fill');
      if (values.length > 1 && peak > 0) {
        var stepX = 200 / (values.length - 1);
        var pts = [];
        for (var p = 0; p < values.length; p++) {
          var x = (p * stepX).toFixed(2);
          var y = (60 - (values[p] / peak) * 56 - 2).toFixed(2);
          pts.push(x + ',' + y);
        }
        if (line) line.setAttribute('points', pts.join(' '));
        if (fill) {
          var fillPts = ['0,60'].concat(pts).concat(['200,60']);
          fill.setAttribute('points', fillPts.join(' '));
        }
        spark.setAttribute('data-has-data', 'true');
      } else {
        if (line) line.setAttribute('points', '');
        if (fill) fill.setAttribute('points', '');
        spark.setAttribute('data-has-data', 'false');
      }
    }

    var available = (d.sources && d.sources.presence_1h) || values.length > 0;
    setState('panel-presence-indicator', available ? 'ok' : 'warn');
    setTs('panel-presence-ts', d.generated_at);
  }

  /* --- World Counters panel --------------------------------------------- */

  function renderWorldCountersFromAgg(d) {
    var wc = d.world_counters || {};
    setText('wc-subfiefs-value', fmtNumber(wc.subfiefs));
    setText('wc-structures-value', fmtNumber(wc.structures));
    setText('wc-vehicles-value', fmtNumber(wc.vehicles));

    var sumP = 0;
    var maps = d.maps_summary || [];
    for (var i = 0; i < maps.length; i++) {
      if (typeof maps[i].players === 'number') sumP += maps[i].players;
    }
    setText('wc-players-value', fmtNumber(sumP));

    /* Trend sparkline from world snapshots (if telemetry returned a series). */
    var trend = $('wc-trend');
    var snaps = wc.snapshots || [];
    var line = $('wc-trend-line');
    if (trend && line && snaps.length > 1) {
      var vals = [];
      for (var s = 0; s < snaps.length; s++) {
        var sn = snaps[s];
        var total = (sn.subfiefs || 0) + (sn.structures || 0) + (sn.vehicles || 0);
        vals.push(total);
      }
      var maxV = 1;
      for (var v = 0; v < vals.length; v++) if (vals[v] > maxV) maxV = vals[v];
      var stepX = 200 / (vals.length - 1);
      var pts = [];
      for (var p = 0; p < vals.length; p++) {
        pts.push((p * stepX).toFixed(2) + ',' + (50 - (vals[p] / maxV) * 46 - 2).toFixed(2));
      }
      line.setAttribute('points', pts.join(' '));
      trend.setAttribute('data-has-data', 'true');
    } else if (trend) {
      if (line) line.setAttribute('points', '');
      trend.setAttribute('data-has-data', 'false');
    }

    setState('panel-world-counters-indicator', wc.available === false ? 'warn' : 'ok');
    setTs('panel-world-counters-ts', d.generated_at);
  }

  /* --- Action Stream panel ---------------------------------------------- */

  function _mergeRecent(d) {
    var recent = d.recent || {};
    var out = [];

    function push(items, kind) {
      if (!Array.isArray(items)) return;
      for (var i = 0; i < items.length; i++) {
        var it = items[i] || {};
        var ts = it.ts || it.timestamp || it.created_at || it.time || 0;
        if (typeof ts === 'string') ts = Math.floor(Date.parse(ts) / 1000) || 0;
        out.push({
          ts: ts,
          kind: kind,
          label: it.label || it.action || it.event || it.type || kind,
          body: it.body || it.message || it.detail || it.summary || JSON.stringify(it).slice(0, 200),
        });
      }
    }
    push(recent.combat, 'pvp');
    push(recent.map_lifecycle, 'maps');
    push(recent.transfers, 'transfers');
    push(recent.grants, 'grants');
    push(recent.levelups, 'levelup');

    out.sort(function (a, b) { return b.ts - a.ts; });
    return out;
  }

  function renderActionStreamFromAgg(d) {
    var list = $('panel-action-stream-body');
    if (!list) return;
    var rows = _mergeRecent(d);
    var empty = $('action-stream-empty');

    if (!rows.length) {
      if (empty) empty.style.display = '';
      setText('action-stream-count', '0 events');
      setState('panel-action-stream-indicator', 'warn');
      setTs('panel-action-stream-ts', d.generated_at);
      var staleEl = $('panel-action-stream-stale');
      if (staleEl) staleEl.hidden = !(d.sources && d.sources.events === false && d.sources.completions === false);
      return;
    }
    if (empty) empty.style.display = 'none';

    var userScrolled = list.dataset.userScrolled === '1';
    var prevScrollTop = list.scrollTop;

    /* Cap rows displayed to keep DOM small. */
    var MAX_ROWS = 100;
    var slice = rows.slice(0, MAX_ROWS);

    /* Re-render: simple wipe + rebuild keeps merge logic trivial. */
    while (list.firstChild) list.removeChild(list.firstChild);
    for (var i = 0; i < slice.length; i++) {
      var r = slice[i];
      var li = document.createElement('li');
      li.className = 'action-stream__row';
      li.setAttribute('data-kind', r.kind);
      li.innerHTML =
        '<span class="action-stream__ts"></span>' +
        '<span class="action-stream__kind"></span>' +
        '<span class="action-stream__body"></span>';
      li.children[0].textContent = r.ts ? fmtRelative(r.ts) + ' ago' : '--';
      li.children[1].textContent = r.kind;
      li.children[2].textContent = r.body;
      list.appendChild(li);
    }

    setText('action-stream-count', rows.length + ' events');
    setState('panel-action-stream-indicator', 'ok');
    setTs('panel-action-stream-ts', d.generated_at);

    if (userScrolled) {
      list.scrollTop = prevScrollTop;
    }
  }

  function streamScrollGuard() {
    var el = $('panel-action-stream-body');
    if (!el) return;
    el.dataset.userScrolled = '0';
    el.addEventListener('scroll', function () {
      el.dataset.userScrolled =
        (el.scrollTop + el.clientHeight < el.scrollHeight - 50) ? '1' : '0';
    });

    var filters = document.querySelectorAll('.action-stream__pill');
    for (var i = 0; i < filters.length; i++) {
      filters[i].addEventListener('click', function (ev) {
        var btn = ev.currentTarget;
        var filter = btn.getAttribute('data-filter') || 'all';
        for (var j = 0; j < filters.length; j++) {
          filters[j].classList.toggle('action-stream__pill--active', filters[j] === btn);
        }
        el.setAttribute('data-filter', filter);
      });
    }
  }

  /* --- Funcom Intelligence panel ---------------------------------------- */

  function renderFuncomIntelFromAgg(d) {
    var fp = d.funcom_push || {};
    setText('fi-push-ts', fp.last_ts ? fmtRelative(fp.last_ts) + ' ago' : '--');
    setText('fi-push-sha', fp.last_sha256 || '--');
    setText('fi-push-count', fmtNumber(fp.push_count_today));
    var fpStale = $('fi-push-stale');
    if (fpStale) fpStale.hidden = !(fp.available === false);

    var rpc = d.bgd_rpc || {};
    setText('fi-rpc-last', rpc.last_ts ? fmtRelative(rpc.last_ts) + ' ago' : '--');
    setText('fi-rpc-1h', fmtNumber(rpc.count_1h));
    setText('fi-rpc-today', fmtNumber(rpc.count_today));
    var rpcStale = $('fi-rpc-stale');
    if (rpcStale) rpcStale.hidden = !(rpc.available === false);

    /* Diff banner + panel border highlight when funcom push diff present. */
    var diff = fp.diff_from_baseline || [];
    var panel = $('panel-funcom-intel');
    var banner = $('funcom-diff-banner');
    if (diff && diff.length) {
      if (panel) panel.setAttribute('data-diff', 'changed');
      if (banner) {
        banner.hidden = false;
        var bannerTs = $('funcom-diff-banner-ts');
        if (bannerTs) bannerTs.textContent = fp.last_ts ? fmtRelative(fp.last_ts) + ' ago' : 'recently';
      }
    } else {
      if (panel) panel.setAttribute('data-diff', 'clean');
      if (banner) banner.hidden = true;
    }

    var intelState = 'ok';
    if (fp.available === false && rpc.available === false) intelState = 'error';
    else if (fp.available === false || rpc.available === false || (diff && diff.length)) intelState = 'warn';
    setState('panel-funcom-intel-indicator', intelState);
    setTs('panel-funcom-intel-ts', d.generated_at);
  }

  /* --- Leaderboards panel (VC4) ----------------------------------------- */

  var LB_TOP_N = 5;

  function _renderBoard(listId, emptyId, rows, rowFn) {
    var list = $(listId);
    if (!list) return false;
    var empty = $(emptyId);
    var data = Array.isArray(rows) ? rows.slice(0, LB_TOP_N) : [];

    /* Wipe and rebuild: small lists, swap each poll. Keep the empty <li>. */
    while (list.firstChild) list.removeChild(list.firstChild);
    if (!data.length) {
      if (empty) { list.appendChild(empty); empty.style.display = ''; }
      return false;
    }
    for (var i = 0; i < data.length; i++) {
      var li = document.createElement('li');
      li.className = 'lb-row';
      li.innerHTML =
        '<span class="lb-row__rank"></span>' +
        '<span class="lb-row__name"></span>' +
        '<span class="lb-row__value"></span>';
      li.children[0].textContent = (i + 1);
      li.children[1].textContent = rowFn.name(data[i]);
      li.children[2].textContent = rowFn.value(data[i]);
      list.appendChild(li);
    }
    return true;
  }

  function renderLeaderboardsFromAgg(d) {
    var lb = d.leaderboards || {};
    setText('lb-week', lb.week || 'current week');

    var anyPvp = _renderBoard('lb-pvp-list', 'lb-pvp-empty', lb.pvp, {
      name: function (r) { return r.name || r.account_id || '--'; },
      value: function (r) { return fmtNumber(r.kills) + ' k'; },
    });
    var anyDeaths = _renderBoard('lb-deaths-list', 'lb-deaths-empty', lb.deaths, {
      name: function (r) { return r.name || r.account_id || '--'; },
      value: function (r) {
        var kd = (r.kd === null || r.kd === undefined) ? '--' : r.kd;
        return fmtNumber(r.deaths) + ' d · ' + kd + ' K/D';
      },
    });
    var anyPilots = _renderBoard('lb-pilots-list', 'lb-pilots-empty', lb.pilots, {
      name: function (r) { return r.name || r.account_id || '--'; },
      value: function (r) {
        var km = (r.km === null || r.km === undefined) ? '--' : r.km;
        return km + ' km';
      },
    });

    var src = d.sources || {};
    var allDown = (src.leaderboard_pvp === false &&
                   src.leaderboard_deaths === false &&
                   src.leaderboard_pilots === false);
    var anyData = anyPvp || anyDeaths || anyPilots;
    setState('panel-leaderboards-indicator', allDown ? 'error' : (anyData ? 'ok' : 'warn'));
    var staleEl = $('panel-leaderboards-stale');
    if (staleEl) staleEl.hidden = !allDown;
    setTs('panel-leaderboards-ts', d.generated_at);
  }

  /* --- Aggregator polling ----------------------------------------------- */

  function setRefreshState(state) {
    var el = $('v2-monitor-refresh-state');
    if (!el) return;
    el.setAttribute('data-state', state);
    el.textContent = state;
  }

  /* Returns a promise that resolves false on a failed poll, which is what
     window.v2Poll backs off on. */
  function pollAggregator() {
    if (_inflight) return true;
    _inflight = true;
    setRefreshState('active');
    return fetch(AGG_URL, { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(function (r) {
        if (r.status === 401) { window.location = '/admin/login'; throw new Error('unauthorized'); }
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (d) {
        _lastData = d;
        renderHeroPanelFromAgg(d);
        renderPresencePanelFromAgg(d);
        renderWorldCountersFromAgg(d);
        renderActionStreamFromAgg(d);
        renderFuncomIntelFromAgg(d);
        renderLeaderboardsFromAgg(d);
        setTs('v2-monitor-refresh-ts', d.generated_at);
        setRefreshState('idle');
        return true;
      })
      .catch(function () {
        setRefreshState('error');
        return false;
      })
      .finally(function () { _inflight = false; });
  }

  document.addEventListener('DOMContentLoaded', function () {
    streamScrollGuard();
    /* Both timers go through window.v2Poll: nothing runs while the tab is
       hidden, becoming visible refreshes at once so the operator never reads a
       stale board, and a failing aggregator is asked less often, not more. */
    window.v2Poll(pollAggregator, AGG_INTERVAL);
    window.v2Poll(tickTimestamps, TICK_INTERVAL);
  });
})();
