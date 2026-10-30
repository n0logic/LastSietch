/* ============================================================================
 * Systems & Health: the flag registry (E1) and the server build card (H4).
 *
 * Both cards are READ ONLY. They carry no control of any kind: the two flag
 * boards above stay the only write levers on this page. Their own file and
 * their own poll, so systems.js (and its shipped cache key) is untouched; the
 * markup reuses the sys-* classes from systems.css.
 *
 * Tri-state rule, same as the boards: a gate this page could not read is
 * "unknown", never "off".
 * ========================================================================= */
(function () {
  'use strict';

  var root = document.querySelector('[data-sys-root]');
  if (!root) return;

  var POLL_MS = 60000;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
  }

  function pill(text, tone) {
    return el('span', 'sys-pill' + (tone ? ' sys-pill--' + tone : ''), text);
  }

  function caption(body, text) {
    body.appendChild(el('p', 'sys-card__caption', text));
  }

  function setState(card, state, label) {
    var badge = card.querySelector('[data-reg-state]');
    if (!badge) return;
    badge.setAttribute('data-state', state);
    badge.textContent = label;
  }

  function table(body, headers, rows, cellFn, emptyText) {
    if (!rows || !rows.length) {
      body.appendChild(el('p', 'sys-empty', emptyText || 'Nothing recorded.'));
      return;
    }
    var wrap = el('div', 'sys-table-wrap');
    var tbl = el('table', 'sys-table');
    var thead = el('thead');
    var hrow = el('tr');
    headers.forEach(function (h) { hrow.appendChild(el('th', null, h)); });
    thead.appendChild(hrow);
    tbl.appendChild(thead);
    var tbody = el('tbody');
    rows.forEach(function (row) {
      var tr = el('tr');
      cellFn(row).forEach(function (cell) {
        if (cell instanceof Node) {
          var td = el('td');
          td.appendChild(cell);
          tr.appendChild(td);
        } else if (cell && typeof cell === 'object') {
          tr.appendChild(el('td', cell.cls || null, cell.text));
        } else {
          tr.appendChild(el('td', null, cell));
        }
      });
      tbody.appendChild(tr);
    });
    tbl.appendChild(tbody);
    wrap.appendChild(tbl);
    body.appendChild(wrap);
  }

  /* --- E1: flag registry -------------------------------------------------- */

  var KIND_ORDER = [
    ['runtime', 'Portal process, request time (an override lands on the next request)'],
    ['boot', 'Portal process, read once at start (restart lastsietch-admin to change)'],
    ['host_file', 'Host files under /etc/lastsietch'],
    ['writer_env', 'Writer and bot environments'],
    ['timer', 'Timers'],
    ['planned', 'Planned (named by a plan, no code reads them yet)']
  ];

  var STATE_TONE = { on: 'on', off: 'off', dormant: 'bad', unknown: null, planned: 'absent' };

  function statePill(state) {
    return pill(state, STATE_TONE.hasOwnProperty(state) ? STATE_TONE[state] : null);
  }

  var RENDER = {};

  RENDER.registry = function (card, body, d) {
    setState(card, 'static', 'read only');
    var counts = d.counts || {};
    var row = el('div', 'sys-pill-row');
    ['on', 'off', 'dormant', 'unknown', 'planned'].forEach(function (k) {
      if (counts[k]) row.appendChild(pill(k + ' ' + counts[k], STATE_TONE[k]));
    });
    body.appendChild(row);
    caption(body, 'Every gate on the stack, its owner, default, scope, what a change ' +
                  'needs and where it lives. Read only: the boards above are the ' +
                  'only controls, and "toggle" marks the gates they reach. A ' +
                  '<game-host> file or timer with no read-only status path reads ' +
                  'unknown (not probed); this page opens no new SSH.');
    if (!d.relay_available) {
      caption(body, 'The relay flag-list did not answer (' + (d.relay_error || 'no reply') +
                    '), so every <game-host> file reads unknown this poll.');
    }
    if ((d.withheld || []).length) {
      caption(body, d.withheld.length + ' entr' + (d.withheld.length === 1 ? 'y' : 'ies') +
                    ' withheld by the secret-name rule: ' + d.withheld.join(', ') + '.');
    }
    var byKind = {};
    (d.rows || []).forEach(function (r) { (byKind[r.kind] = byKind[r.kind] || []).push(r); });
    KIND_ORDER.forEach(function (k) {
      var rows = byKind[k[0]];
      if (!rows || !rows.length) return;
      body.appendChild(el('h3', 'sys-card__caption sys-reg__group', k[1] + ' (' + rows.length + ')'));
      table(body, ['Gate', 'State', 'Host', 'Where', 'Scope', 'Default', 'Change needs', 'Owner', 'Gates'],
        rows, function (r) {
          var name = el('span', null, r.name);
          if (r.toggle) {
            name.appendChild(document.createTextNode(' '));
            name.appendChild(el('span', 'sys-chip', 'toggle'));
          }
          var state = el('span');
          state.appendChild(statePill(r.state));
          state.appendChild(el('div', 'sys-table__muted', r.source || ''));
          return [
            name,
            state,
            { text: r.host || '-', cls: 'sys-table__muted' },
            { text: r.where || '-', cls: 'sys-table__wrap-cell sys-table__muted' },
            { text: r.scope || '-', cls: 'sys-table__muted' },
            { text: r.default || '-', cls: 'sys-table__muted' },
            { text: r.restart || '-', cls: 'sys-table__muted' },
            { text: r.owner || '-', cls: 'sys-table__muted' },
            { text: r.feature || '', cls: 'sys-table__wrap-cell' }
          ];
        });
    });
  };

  /* --- H4: server build ---------------------------------------------------- */

  RENDER.build = function (card, body, d) {
    setState(card, 'static', '<web-host> read');
    var rec = d.record || {};
    var log = d.log || {};
    var base = d.baseline || {};
    var v = d.verdict || {};

    var banner = el('p', 'sys-reg__verdict sys-reg__verdict--' + (v.tone || 'absent'));
    banner.appendChild(pill(v.code ? v.code.replace(/_/g, ' ') : 'unknown', v.tone || 'absent'));
    banner.appendChild(document.createTextNode(' ' + (v.text || '')));
    body.appendChild(banner);

    var steamServer = log.available ? (log.steam_server_buildid || '-') : 'log unread';
    var steamClient = log.available ? (log.steam_client_buildid || 'not in log') : 'log unread';
    table(body, ['', 'Installed (recorded)', 'Latest on Steam (watcher)', 'Watcher baseline (unit env)'], [
      ['Server build', rec.server_buildid, steamServer, base.DUNE_HFW_BASE_BUILDID || '-'],
      ['Client build', rec.client_buildid, steamClient, base.DUNE_HFW_CLIENT_BASE_BUILDID || 'not set'],
      ['Image tag', rec.image_tag, 'known only after an intake diff', '-']
    ], function (r) {
      var moved = r[0] !== 'Image tag' && r[2] && r[1] && /^\d+$/.test(r[2]) && r[2] !== r[1];
      return [r[0],
              { text: r[1] || '-', cls: 'sys-table__num' },
              moved ? pill(r[2], 'warn') : { text: r[2] || '-', cls: 'sys-table__num' },
              { text: r[3], cls: 'sys-table__num sys-table__muted' }];
    });

    var notes = [];
    if (d.baseline_matches_installed === false) {
      notes.push('The watcher is armed on ' + base.DUNE_HFW_BASE_BUILDID + ', not the installed ' +
                 rec.server_buildid + '. That is expected after a skipped build: it alerts on the ' +
                 'NEXT one.');
    }
    if (d.notified) {
      notes.push('The watcher has fired and is silent until it is re-armed (hotfix-notified.marker ' +
                 'is present), so the Steam reading is as old as its last result.');
    }
    if (d.need_daemon_reload) {
      notes.push('The unit file changed on disk without a daemon-reload: the baseline shown is ' +
                 'what systemd loaded, and the next run uses it.');
    }
    if (!log.available) notes.push('Watcher log: ' + (log.error || 'unreadable') + '.');
    notes.forEach(function (n) { caption(body, n); });

    var t = d.timer || {};
    caption(body, 'Watcher: ' + (log.result || 'no result') + ' at ' + (log.last_result_at || '-') +
                  '; last log line ' + (log.last_line_at || '-') + '. Timer ' + (t.state || 'unknown') +
                  (t.source ? ' (' + t.source + ')' : '') + '. Baseline from ' +
                  (d.baseline_from || '-') + '.');

    body.appendChild(el('h3', 'sys-card__caption sys-reg__group', 'Image per server set (recorded ' +
                                (rec.recorded || '-') + ', not a live read)'));
    table(body, ['Set', 'Map', 'Image tag', 'Note'], rec.sets || [], function (s) {
      var patched = /-lastsietch-b\dtest$/.test(s.tag || '');
      return [{ text: s.set, cls: 'sys-table__num' },
              s.map,
              patched ? pill(s.tag, 'warn') : { text: s.tag, cls: 'sys-table__wrap-cell' },
              { text: s.note || '', cls: 'sys-table__muted' }];
    });
    caption(body, 'No dispatcher token reads the BattleGroup images, so this table is the ' +
                  'window record. Patched sets are wiped by every Funcom build and are not ' +
                  'carried by gap.sh.');

    (rec.skipped || []).forEach(function (s) {
      caption(body, 'Skipped ' + s.decided + ': server build ' + s.server_buildid + ' (tag ' +
                    s.image_tag + '). ' + s.why + '. ' + s.runbook);
    });

    var stats = el('div', 'sys-stats');
    var online = el('div', 'sys-stat');
    var onlineValue = el('div', 'sys-stat__value', '-');
    online.appendChild(onlineValue);
    online.appendChild(el('div', 'sys-stat__label', 'Players online'));
    stats.appendChild(online);
    body.appendChild(stats);
    fetch('/admin/api/dune/pulse', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (p) {
        if (p && typeof p.online === 'number') {
          onlineValue.textContent = String(p.online);
          onlineValue.className = 'sys-stat__value sys-stat__value--live';
        }
      })
      .catch(function () { /* the count stays '-' */ });
    caption(body, 'Latest window runbook: ' + (rec.runbook || '-'));
  };

  /* --- Fetch loop -------------------------------------------------------- */

  function loadCard(card) {
    var url = card.getAttribute('data-src');
    var render = RENDER[card.getAttribute('data-render')];
    var body = card.querySelector('[data-reg-body]');
    if (!url || !render || !body) return Promise.resolve(false);
    return fetch(url, { credentials: 'same-origin' })
      .then(function (r) {
        if (r.status === 401) { window.location = '/admin/login'; throw new Error('Unauthorized'); }
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (d) {
        if (d.available === false) throw new Error(d.error || 'source unavailable');
        render(card, clear(body), d);
        return true;
      })
      .catch(function (err) {
        setState(card, 'error', 'source down');
        clear(body).appendChild(el('p', 'sys-error', err.message || String(err)));
        return false;
      });
  }

  function poll() {
    var cards = root.querySelectorAll('[data-reg-card]');
    var jobs = [];
    for (var i = 0; i < cards.length; i++) jobs.push(loadCard(cards[i]));
    return Promise.all(jobs).then(function (results) { return results.indexOf(true) !== -1; });
  }

  document.addEventListener('DOMContentLoaded', function () {
    window.v2Poll(poll, POLL_MS);
  });
})();
