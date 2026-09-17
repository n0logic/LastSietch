/* ============================================================================
 * Admin wave 1, Set B: Systems & Health, Economy, Social.
 *
 * One card, one source, one fetch. There is no aggregator: a card that loses
 * its source paints its own error and every other card on the page keeps
 * updating. That is the same per-source isolation v2-monitor.js gets from the
 * aggregator envelope, arrived at structurally instead.
 *
 * Colour rule, enforced here rather than in CSS: a number gets the live class
 * ONLY when the poll that produced it just returned. A value read from a config
 * module or a file on disk is static and stays neutral no matter how fresh the
 * fetch was.
 * ========================================================================= */
(function () {
  'use strict';

  var root = document.querySelector('[data-sys-root]');
  if (!root) return;

  var POLL_MS = parseInt(root.getAttribute('data-poll-interval') || '15000', 10);
  var PAGE_SIZE = parseInt(root.getAttribute('data-page-size') || '50', 10);
  var pollState = document.getElementById('sys-poll-state');
  var pollTs = document.getElementById('sys-poll-ts');

  /* --- Formatting -------------------------------------------------------- */

  function fmtNum(n) {
    if (n === null || n === undefined || n === '') return '-';
    if (typeof n !== 'number') return String(n);
    return n.toLocaleString();
  }

  function fmtBytes(n) {
    if (!n) return '0 B';
    var units = ['B', 'KB', 'MB', 'GB', 'TB'];
    var i = 0;
    var v = n;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return (i === 0 ? v : v.toFixed(1)) + ' ' + units[i];
  }

  function fmtAge(seconds) {
    if (seconds === null || seconds === undefined) return '-';
    var d = Math.max(0, Math.floor(seconds));
    if (d < 60) return d + 's';
    if (d < 3600) return Math.floor(d / 60) + 'm';
    if (d < 86400) return Math.floor(d / 3600) + 'h';
    return Math.floor(d / 86400) + 'd';
  }

  function fmtEpochAge(epoch) {
    if (!epoch) return '-';
    return fmtAge(Math.floor(Date.now() / 1000) - epoch);
  }

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

  /* --- Card primitives --------------------------------------------------- */

  function cardBody(card) { return card.querySelector('[data-sys-body]'); }

  function setCardState(card, state, label) {
    var badge = card.querySelector('[data-sys-state]');
    if (!badge) return;
    badge.setAttribute('data-state', state);
    badge.textContent = label;
  }

  function showError(card, message) {
    setCardState(card, 'error', 'source down');
    var body = clear(cardBody(card));
    body.appendChild(el('p', 'sys-error', message));
  }

  /* stats(card, [{label, value, tone}]) - tone 'live' is the only Ibad path. */
  function stats(body, items) {
    var wrap = el('div', 'sys-stats');
    items.forEach(function (item) {
      var box = el('div', 'sys-stat');
      var cls = 'sys-stat__value';
      if (item.tone) cls += ' sys-stat__value--' + item.tone;
      box.appendChild(el('div', cls, item.value));
      box.appendChild(el('div', 'sys-stat__label', item.label));
      wrap.appendChild(box);
    });
    body.appendChild(wrap);
  }

  function caption(body, text) {
    body.appendChild(el('p', 'sys-card__caption', text));
  }

  /* table(body, headers, rows, cellFn) - cellFn returns an array of either a
     string or {text, cls} per row. */
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
        if (cell && typeof cell === 'object' && !(cell instanceof Node)) {
          tr.appendChild(el('td', cell.cls || null, cell.text));
        } else if (cell instanceof Node) {
          var td = el('td');
          td.appendChild(cell);
          tr.appendChild(td);
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

  function pill(text, tone) {
    return el('span', 'sys-pill' + (tone ? ' sys-pill--' + tone : ''), text);
  }

  function pager(card, body, data) {
    if (!card.hasAttribute('data-paged')) return;
    var offset = parseInt(card.getAttribute('data-offset') || '0', 10);
    var wrap = el('div', 'sys-pager');
    var shown = (data.total !== undefined && data.total !== null)
      ? (offset + 1) + ' to ' + (offset + (data.count || 0)) + ' of ' + fmtNum(data.total)
      : (offset + 1) + ' to ' + (offset + (data.count || 0));
    wrap.appendChild(el('span', null, shown));
    var prev = el('button', 'sys-pager__btn', 'Prev');
    prev.disabled = offset <= 0;
    prev.addEventListener('click', function () {
      card.setAttribute('data-offset', String(Math.max(0, offset - PAGE_SIZE)));
      loadCard(card);
    });
    var next = el('button', 'sys-pager__btn', 'Next');
    next.disabled = !data.has_more;
    next.addEventListener('click', function () {
      card.setAttribute('data-offset', String(offset + PAGE_SIZE));
      loadCard(card);
    });
    wrap.appendChild(prev);
    wrap.appendChild(next);
    body.appendChild(wrap);
  }

  function bars(body, series, labelFn, valueFn) {
    if (!series || !series.length) return;
    var max = 0;
    series.forEach(function (p) { max = Math.max(max, valueFn(p)); });
    var chart = el('div', 'sys-bars');
    series.forEach(function (p) {
      var bar = el('div', 'sys-bars__bar');
      var pct = max > 0 ? Math.max(3, Math.round((valueFn(p) / max) * 100)) : 3;
      bar.style.height = pct + '%';
      bar.title = labelFn(p) + ': ' + fmtNum(valueFn(p));
      chart.appendChild(bar);
    });
    body.appendChild(chart);
    var axis = el('div', 'sys-bars__axis');
    axis.appendChild(el('span', null, labelFn(series[0])));
    axis.appendChild(el('span', null, labelFn(series[series.length - 1])));
    body.appendChild(axis);
  }

  /* --- Flag boards ------------------------------------------------------- */

  var IS_OWNER = root.getAttribute('data-owner') === '1';

  /* Tri-state text: a flag the source did not report is unknown, never off.
     "we could not look" is not "it is off", and on a kill switch that is the
     whole difference. */
  function flagWord(value) {
    if (value === true || value === 'on') return 'on';
    if (value === false || value === 'off') return 'off';
    return 'unknown';
  }

  function flagPill(value) {
    var word = flagWord(value);
    return pill(word, word === 'unknown' ? null : (word === 'on' ? 'on' : 'off'));
  }

  function chip(text) { return el('span', 'sys-chip', text); }

  /* One row of the confirm panel that has to be typed through before a flag
     moves: the typed token, and a SECOND explicit line naming what players lose
     with its own acknowledgement. Both are re-checked server side. */
  function flagConfirm(card, opts) {
    var panel = el('div', 'sys-confirm');
    var next = opts.isOn ? 'off' : 'on';
    panel.appendChild(el('p', 'sys-confirm__head',
      'Turn ' + opts.name + ' ' + next + ' on the ' + opts.where + '.'));
    var lose = el('p', 'sys-confirm__impact');
    lose.textContent = next === 'off'
      ? 'While it is off, players lose ' + (opts.impact || 'this feature') + '.'
      : 'Turning it on gives players back ' + (opts.impact || 'this feature') + '.';
    panel.appendChild(lose);

    var ack = null;
    if (next === 'off') {
      var ackWrap = el('label', 'sys-confirm__ack');
      ack = document.createElement('input');
      ack.type = 'checkbox';
      ackWrap.appendChild(ack);
      ackWrap.appendChild(el('span', null,
        'I have read what players lose and I am turning it off anyway.'));
      panel.appendChild(ackWrap);
    }

    var field = el('label', 'sys-confirm__field');
    field.appendChild(el('span', null, 'Type ' + opts.confirm + ' to continue'));
    var typed = document.createElement('input');
    typed.type = 'text';
    typed.className = 'sys-confirm__input';
    typed.setAttribute('autocomplete', 'off');
    field.appendChild(typed);
    panel.appendChild(field);

    var status = el('p', 'sys-confirm__status');
    var apply = el('button', 'sys-btn sys-btn--danger', 'Apply');
    var cancel = el('button', 'sys-btn', 'Cancel');
    var actions = el('div', 'sys-confirm__actions');
    actions.appendChild(apply);
    actions.appendChild(cancel);
    panel.appendChild(actions);
    panel.appendChild(status);

    cancel.addEventListener('click', function () {
      if (panel.parentNode) panel.parentNode.removeChild(panel);
    });

    apply.addEventListener('click', function () {
      apply.disabled = true;
      status.textContent = 'Applying...';
      fetch(opts.url, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': (typeof csrfToken !== 'undefined') ? csrfToken : ''
        },
        body: JSON.stringify({
          name: opts.name,
          state: next,
          confirm: typed.value.trim(),
          acknowledge_dark: !!(ack && ack.checked)
        })
      }).then(function (r) {
        return r.json().then(function (data) { return { ok: r.ok, data: data }; });
      }).then(function (res) {
        if (!res.ok || res.data.success === false) {
          var msg = (res.data && (res.data.detail || res.data.write_error ||
                                  res.data.error)) || 'the change was refused';
          status.textContent = String(msg);
          apply.disabled = false;
          return;
        }
        // The server answered with a fresh read of its own source. Repaint the
        // whole card from that source rather than from what we just asked for.
        status.textContent = 'Applied. Re-reading the source.';
        loadCard(card);
      }).catch(function (err) {
        status.textContent = err.message || String(err);
        apply.disabled = false;
      });
    });
    return panel;
  }

  /* headers: Flag | Process env | Override | Effective | Control. `kind` is
     'process' or 'host'; a host row has no override column of its own because
     the file IS the state. */
  function flagBoard(body, rows, opts) {
    if (!rows || !rows.length) {
      body.appendChild(el('p', 'sys-empty', 'No flags in this family.'));
      return;
    }
    var headers = opts.kind === 'host'
      ? ['Flag', 'File on <game-host>', 'What it gates', 'Control']
      : ['Flag', 'Process env', 'Override', 'Effective', 'Control'];
    table(body, headers, rows, function (r) {
      var isOn = opts.kind === 'host' ? (r.state === 'on') : !!r.effective;
      var control;
      if (r.restart_required) {
        control = chip('restart to change');
      } else if (!IS_OWNER) {
        control = chip('owner only');
      } else if (opts.kind === 'host' && r.state === 'unknown') {
        control = chip('game host unread');
      } else {
        var btn = el('button', 'sys-btn' + (isOn ? ' sys-btn--danger' : ''),
                     isOn ? 'Turn off' : 'Turn on');
        btn.addEventListener('click', function (ev) {
          var card = ev.target.closest('[data-sys-card]');
          var host = ev.target.parentNode;
          var existing = host.querySelector('.sys-confirm');
          if (existing) { host.removeChild(existing); return; }
          host.appendChild(flagConfirm(card, {
            name: r.name,
            impact: r.impact,
            isOn: isOn,
            confirm: opts.confirm,
            url: opts.url,
            where: opts.where
          }));
        });
        control = btn;
      }
      if (opts.kind === 'host') {
        var note = el('span', r.warning ? 'sys-table__warn' : 'sys-table__muted',
                      r.impact || '');
        return [r.name, flagPill(r.state), note, control];
      }
      var envText = (r.env === null || r.env === undefined)
        ? 'unset, default ' + (r.default ? 'on' : 'off')
        : (r.env ? 'on' : 'off');
      var overText = (r.override === null || r.override === undefined)
        ? 'none' : (r.override ? 'on' : 'off');
      return [r.name,
              { text: envText, cls: 'sys-table__muted' },
              { text: overText, cls: 'sys-table__muted' },
              flagPill(r.effective),
              control];
    });
  }

  /* --- Renderers --------------------------------------------------------- */

  var RENDER = {};

  RENDER.flags = function (card, body, d) {
    setCardState(card, 'static', 'config module');
    caption(body, d.family + '. ' + d.source + '. ' + d.gates_on + ' of ' +
                  d.gates_total + ' gates on. Names carrying a secret are never ' +
                  'emitted.');
    caption(body, 'Request-time gates. The override file is read on every ' +
                  'request, so a change here lands on the next one with no ' +
                  'restart: ' + (d.override_file || 'data/feature_flags.json') + '.');
    flagBoard(body, d.runtime_gates, {
      kind: 'process', confirm: d.confirm || 'FLAG', where: 'portal process',
      url: '/admin/api/dune/v2/systems/flags/_set'
    });
    caption(body, 'Import-time constants. config.py resolves these once at ' +
                  'boot, so nothing this page could write would reach them: ' +
                  'they are read only until lastsietch-admin restarts.');
    flagBoard(body, d.restart_gates, {
      kind: 'process', confirm: d.confirm || 'FLAG', where: 'portal process',
      url: '/admin/api/dune/v2/systems/flags/_set'
    });
    caption(body, 'Everything else the running process resolved.');
    var gates = d.flags.filter(function (f) { return f.kind === 'gate'; });
    var tuning = d.flags.filter(function (f) { return f.kind !== 'gate'; });
    var row = el('div', 'sys-pill-row');
    gates.forEach(function (f) {
      row.appendChild(pill(f.name + ' ' + (f.value ? 'ON' : 'OFF'), f.value ? 'on' : 'off'));
    });
    body.appendChild(row);
    body.appendChild(el('p', 'sys-card__caption', 'Tuning values alongside the gates.'));
    table(body, ['Setting', 'Value'], tuning, function (f) {
      // Paths and URIs wrap; only real numbers get the tabular right-aligned
      // column, or one long path drags the table wider than its card.
      return [f.name, { text: String(f.value),
                        cls: f.kind === 'number' ? 'sys-table__num' : 'sys-table__wrap-cell' }];
    });
  };

  /* The game-host family. This one IS a live read: every value is a fresh
     test -f taken on <game-host> during this poll, so it earns the live badge that
     the config-module board above must never wear. */
  RENDER.host_flags = function (card, body, d) {
    setCardState(card, 'live', '<game-host>');
    caption(body, d.family + '. ' + d.source + '. These files are what the ' +
                  'game-side writers read, so this is the switch that really ' +
                  'darks a feature; the process board above only decides ' +
                  'whether the portal offers the door.');
    flagBoard(body, d.flags, {
      kind: 'host', confirm: d.confirm || 'FLAG', where: 'game host',
      url: '/admin/api/dune/v2/systems/host-flags/_set'
    });
    caption(body, 'servercmd-enabled is the odd one out: turning it off darks ' +
                  'this panel\'s own live actions on the game host, not a ' +
                  'player feature. SSH to <game-host> stays the way back in.');
  };

  RENDER.versions = function (card, body, d) {
    setCardState(card, 'static', 'served build');
    stats(body, [
      { label: 'V2 app version', value: d.v2_app_version || '-', tone: 'text' },
      { label: 'V2 shell key', value: d.v2_sw_key || '-', tone: 'text' },
      { label: 'Classic shell key', value: d.classic_sw_key || '-', tone: 'text' }
    ]);
    caption(body, 'Both V2 values must move on every portal deploy, or an ' +
                  'installed app keeps the old shell. Read from the built ' +
                  'artifact that is actually served, not the source tree.');
    var rows = [
      ['app version', d.v2_app_version_from || d.v2_app_version_error || '-'],
      ['V2 service worker', d.v2_sw_key_from || '-'],
      ['classic service worker', d.classic_sw_key_from || d.classic_sw_key_error || '-']
    ];
    table(body, ['Value', 'Read from'], rows, function (r) {
      return [r[0], { text: r[1], cls: 'sys-table__wrap-cell sys-table__muted' }];
    });
  };

  RENDER.services = function (card, body, d) {
    setCardState(card, 'live', 'live');
    table(body, ['Unit', 'State', 'Since', 'Role'], d.units, function (u) {
      var tone, label;
      if (!u.found) { tone = 'absent'; label = 'not on this host'; }
      else if (u.active_state === 'active') { tone = 'good'; label = 'active'; }
      else if (u.active_state === 'activating') { tone = 'warn'; label = u.active_state; }
      else { tone = 'bad'; label = u.active_state; }
      return [
        { text: u.unit, cls: 'sys-table__wrap-cell' },
        pill(label, tone),
        { text: u.since_epoch ? fmtEpochAge(u.since_epoch) : '-', cls: 'sys-table__num' },
        { text: u.label, cls: 'sys-table__muted' }
      ];
    });
    caption(body, 'The telemetry logger and the market bot run on the game ' +
                  'host, so on this box they read as not present. That is ' +
                  'their normal state here, not a fault.');
  };

  RENDER.db = function (card, body, d) {
    setCardState(card, 'live', 'live');
    table(body, ['Database', 'Size', 'WAL', 'SHM', 'Modified'], d.databases, function (f) {
      return [
        f.label || f.path,
        { text: fmtBytes(f.bytes), cls: 'sys-table__num' },
        { text: fmtBytes(f.wal_bytes), cls: 'sys-table__num' },
        { text: fmtBytes(f.shm_bytes), cls: 'sys-table__num' },
        { text: fmtEpochAge(f.mtime), cls: 'sys-table__num' }
      ];
    });
    caption(body, 'A WAL that keeps growing without checkpointing is the ' +
                  'shape of a stuck writer, which is why it gets its own column.');
  };

  RENDER.rmq = function (card, body, d) {
    var reading = d.reading || 0;
    setCardState(card, reading ? 'live' : 'error',
                 reading + ' of ' + (d.sources || 0) + ' reading');
    var keys = [
      ['partition_counts', 'Partition counts'],
      ['travel_queue', 'Travel queue'],
      ['last_funcom_push', 'Last Funcom push'],
      ['bgd_rpc', 'BGD RPC'],
      ['completions', 'Map completions']
    ];
    table(body, ['Capture', 'State', 'Detail'], keys, function (k) {
      var src = d[k[0]] || {};
      var ok = src.available !== false;
      var detail = ok ? summarise(src) : (src.error || 'unavailable');
      return [
        k[1],
        pill(ok ? 'reading' : 'down', ok ? 'good' : 'bad'),
        { text: detail, cls: 'sys-table__wrap-cell sys-table__muted' }
      ];
    });
  };

  function summarise(src) {
    var parts = [];
    Object.keys(src).forEach(function (key) {
      if (key === 'available' || key === 'error') return;
      var v = src[key];
      if (typeof v === 'number' || typeof v === 'string' || typeof v === 'boolean') {
        parts.push(key + '=' + v);
      } else if (Array.isArray(v)) {
        parts.push(key + '[' + v.length + ']');
      } else if (v && typeof v === 'object') {
        parts.push(key + '{' + Object.keys(v).length + '}');
      }
    });
    return parts.slice(0, 6).join('  ') || 'no fields';
  }

  RENDER.telemetry = function (card, body, d) {
    var up = (d.events || {}).available !== false;
    setCardState(card, up ? 'live' : 'error', up ? 'live' : 'relay down');
    stats(body, [
      { label: 'Last event', value: fmtAge(d.last_event_age_s), tone: up ? 'live' : 'bad' },
      { label: 'Last transfer', value: fmtAge(d.last_transfer_age_s), tone: up ? 'live' : null }
    ]);
    table(body, ['Relay read', 'State'], [
      ['events', d.events], ['transfers', d.transfers], ['world', d.world]
    ], function (r) {
      var ok = (r[1] || {}).available !== false;
      return [r[0], ok ? pill('reading', 'good')
                       : { text: (r[1] || {}).error || 'unavailable',
                           cls: 'sys-table__wrap-cell sys-table__muted' }];
    });
    var store = d.db || {};
    if (store.available) {
      table(body, ['Store', 'Size', 'WAL'], [store], function (f) {
        return [
          { text: f.path, cls: 'sys-table__wrap-cell' },
          { text: fmtBytes(f.bytes), cls: 'sys-table__num' },
          { text: fmtBytes(f.wal_bytes), cls: 'sys-table__num' }
        ];
      });
    } else {
      caption(body, store.error || 'telemetry store not readable from this host');
    }
    if (d.world && d.world.available === false) {
      caption(body, 'World counters: ' + d.world.error);
    }
  };

  RENDER.snapshot = function (card, body, d) {
    setCardState(card, 'live', 'live');
    table(body, ['Job', 'Last run', 'Result', 'State'], d.units, function (u) {
      var tone = !u.found ? 'absent'
        : (u.sub_state === 'failed' || u.active_state === 'failed') ? 'bad' : 'good';
      return [
        { text: u.label, cls: 'sys-table__wrap-cell' },
        { text: u.since_epoch ? fmtEpochAge(u.since_epoch) + ' ago' : '-', cls: 'sys-table__num' },
        pill(u.found ? (u.sub_state || u.active_state) : 'not on this host', tone),
        { text: u.unit, cls: 'sys-table__muted' }
      ];
    });
    caption(body, (d.report_body && d.report_body.error) || '');
    // One line, not a second copy of the Databases card: the nightly report
    // names these two files, so the card says which it tracks and how big they
    // are without duplicating the table next to it.
    caption(body, 'Tracked by the nightly report: ' +
                  (d.databases || []).map(function (f) {
                    return f.path.split('/').pop() + ' ' + fmtBytes(f.bytes);
                  }).join(', '));
  };

  RENDER.rewards = function (card, body, d) {
    setCardState(card, 'static', 'admin.db');
    var m = d.monthly || {};
    stats(body, [
      { label: 'Monthly claims, this period', value: fmtNum(m.claims_this_period) },
      { label: 'Monthly claims, prior period', value: fmtNum(m.claims_prior_period) },
      { label: 'Grace window', value: m.in_grace_window ? 'OPEN' : 'closed',
        tone: m.in_grace_window ? 'warn' : null }
    ]);
    caption(body, 'Period ' + m.period_key + ', grace runs ' + m.grace_days +
                  ' days from the period start and closes ' + m.grace_deadline + '.');

    var daily = {};
    (d.by_day || []).forEach(function (r) {
      daily[r.day] = (daily[r.day] || 0) + r.claims;
    });
    var days = Object.keys(daily).sort().map(function (k) {
      return { day: k, claims: daily[k] };
    });
    bars(body, days, function (p) { return p.day; }, function (p) { return p.claims; });
    caption(body, 'Claims per day, 14 days.');

    table(body, ['Run length', 'Accounts'], d.claim_runs || [], function (r) {
      return [r.bucket + ' days', { text: fmtNum(r.accounts), cls: 'sys-table__num' }];
    });
    caption(body, d.claim_run_note);
    table(body, ['Reward', 'Claims', 'Accounts'], d.totals || [], function (r) {
      return [r.reward_kind, { text: fmtNum(r.claims), cls: 'sys-table__num' },
              { text: fmtNum(r.accounts), cls: 'sys-table__num' }];
    });
  };

  RENDER.history = function (card, body, d) {
    setCardState(card, 'static', 'market_history.db read only');
    var lag = d.last_rollup_lag_hours;
    stats(body, [
      { label: 'Templates', value: fmtNum(d.templates) },
      { label: 'Rollup lag, hours', value: lag === null ? '-' : lag,
        tone: (lag !== null && lag > 2) ? 'warn' : null },
      { label: 'File size', value: fmtBytes(d.file ? d.file.bytes : 0) }
    ]);
    table(body, ['Tier', 'Rows', 'Newest sample', 'Oldest sample'], d.tiers || [],
      function (t) {
        return [
          t.tier,
          { text: fmtNum(t.rows), cls: 'sys-table__num' },
          { text: fmtAge(t.newest_age_s) + ' ago', cls: 'sys-table__num' },
          { text: t.oldest_ts ? new Date(t.oldest_ts * 1000).toISOString().slice(0, 10) : '-',
            cls: 'sys-table__num' }
        ];
      });
    caption(body, 'Raw keeps 48 hours, hourly 60 days, daily 365. The rollup ' +
                  'runs once an hour, so a lag above two hours means the ' +
                  'capture path stopped.');
  };

  RENDER.watch = function (card, body, d) {
    setCardState(card, 'static', 'admin.db');
    stats(body, [
      { label: 'Watches', value: fmtNum(d.watches) },
      { label: 'Armed', value: fmtNum(d.watches_armed) },
      { label: 'Alerts, 24h', value: fmtNum(d.alerts_24h) },
      { label: 'DMs pending', value: fmtNum(d.alerts_dm_pending),
        tone: d.alerts_dm_pending > 0 ? 'warn' : null }
    ]);
    caption(body, 'Loop ' + (d.loop_enabled ? 'enabled' : 'disabled') + ', every ' +
                  d.loop_interval_s + 's, cap ' + d.max_per_account +
                  ' per account. Last sweep touched a watch ' +
                  (d.last_checked_at || 'never') + '.');
    table(body, ['Template', 'Watchers', 'Lowest threshold'], d.top_templates || [],
      function (r) {
        return [
          { text: r.name_cached || r.template_id, cls: 'sys-table__wrap-cell' },
          { text: fmtNum(r.watchers), cls: 'sys-table__num' },
          { text: fmtNum(r.lowest_threshold), cls: 'sys-table__num' }
        ];
      });
    table(body, ['Fired', 'Item', 'Threshold', 'Matched', 'Seen', 'DM'],
      d.recent_alerts || [], function (r) {
        return [
          { text: r.created_at, cls: 'sys-table__num' },
          { text: r.name_cached || r.template_id, cls: 'sys-table__wrap-cell' },
          { text: fmtNum(r.threshold_price), cls: 'sys-table__num' },
          { text: fmtNum(r.match_price), cls: 'sys-table__num' },
          pill(r.seen_in_portal ? 'seen' : 'unseen', r.seen_in_portal ? 'good' : 'off'),
          pill(r.dm_sent ? 'sent' : 'pending', r.dm_sent ? 'good' : 'warn')
        ];
      }, 'No alert has fired.');
  };

  RENDER.karum_wtb = function (card, body, d) {
    setCardState(card, 'static', 'admin.db');
    var row = el('div', 'sys-pill-row');
    (d.by_status || []).forEach(function (s) {
      row.appendChild(pill(s.status + ' ' + s.n, s.status === 'active' ? 'on' : 'off'));
    });
    body.appendChild(row);
    caption(body, 'Wanted orders. A grade of ANY is a legacy row posted before ' +
                  'grades existed and any grade still fills it. Base is grade 0 ' +
                  'and is a real request, not an unset one.');
    table(body, ['Order', 'Requester', 'Item', 'Qty', 'Grade', 'Price', 'Status', 'Filler', 'Posted'],
      d.requests || [], function (r) {
        var grade = (r.quality_level === null || r.quality_level === undefined)
          ? 'ANY' : (r.quality_mode || 'exact') + ' ' + r.quality_level;
        return [
          { text: r.request_id, cls: 'sys-table__num' },
          { text: r.requester_name, cls: 'sys-table__wrap-cell' },
          { text: r.display_name || r.template_id, cls: 'sys-table__wrap-cell' },
          { text: fmtNum(r.stack_size), cls: 'sys-table__num' },
          grade,
          { text: fmtNum(r.price), cls: 'sys-table__num' },
          pill(r.status, r.status === 'active' ? 'on'
            : (r.status === 'paid_undelivered' ? 'bad'
              : (r.status === 'filled' ? 'good' : 'off'))),
          { text: r.filler_name || '-', cls: 'sys-table__muted' },
          { text: r.created_at, cls: 'sys-table__num' }
        ];
      }, 'No wanted order has been posted.');
    pager(card, body, { total: d.total, count: (d.requests || []).length, has_more: d.has_more });
  };

  RENDER.blueprints = function (card, body, d) {
    setCardState(card, 'static', 'admin.db');
    stats(body, [
      { label: 'Publishes, 24h', value: fmtNum(d.publishes_24h) },
      { label: 'Publishers, 24h', value: fmtNum(d.publishers_24h) },
      { label: 'Daily cap', value: fmtNum(d.daily_cap) },
      { label: 'Published with no blob', value: fmtNum(d.published_without_blob),
        tone: d.published_without_blob > 0 ? 'bad' : null }
    ]);
    var row = el('div', 'sys-pill-row');
    (d.by_status || []).forEach(function (s) {
      row.appendChild(pill(s.status + ' ' + s.n + ', ' + fmtBytes(s.bytes),
                           s.status === 'published' ? 'on' : 'off'));
    });
    body.appendChild(row);
    if ((d.accounts_at_cap || []).length) {
      caption(body, (d.accounts_at_cap || []).length +
                    ' account(s) are at the rolling 24 hour publish cap.');
    }
    table(body, ['Id', 'Author', 'Title', 'Pieces', 'Downloads', 'Size', 'Status', 'Published'],
      d.publishes || [], function (r) {
        return [
          { text: r.publish_id, cls: 'sys-table__num' },
          { text: r.author_name, cls: 'sys-table__wrap-cell' },
          { text: r.title, cls: 'sys-table__wrap-cell' },
          { text: fmtNum(r.piece_count), cls: 'sys-table__num' },
          { text: fmtNum(r.download_count), cls: 'sys-table__num' },
          { text: fmtBytes(r.blob_bytes), cls: 'sys-table__num' },
          pill(r.status, r.status === 'published' ? 'on' : 'off'),
          { text: r.created_at, cls: 'sys-table__num' }
        ];
      }, 'Nothing published yet.');
    pager(card, body, { total: d.total, count: (d.publishes || []).length, has_more: d.has_more });
  };

  RENDER.social_guilds = function (card, body, d) {
    setCardState(card, 'static', 'admin.db');
    var row = el('div', 'sys-pill-row');
    (d.join_requests_by_status || []).forEach(function (s) {
      row.appendChild(pill(s.status + ' ' + s.n, s.status === 'pending' ? 'warn' : 'off'));
    });
    row.appendChild(pill('recruiting ' + d.recruiting_open, 'on'));
    body.appendChild(row);
    table(body, ['Id', 'Requester', 'Guild', 'Status', 'Raised', 'Expires'],
      d.join_requests || [], function (r) {
        return [
          { text: r.id, cls: 'sys-table__num' },
          { text: r.requester_char_name || '-', cls: 'sys-table__wrap-cell' },
          { text: r.guild_id, cls: 'sys-table__num' },
          pill(r.status, r.status === 'pending' ? 'warn' : 'off'),
          { text: r.created_at, cls: 'sys-table__num' },
          { text: r.expires_at || '-', cls: 'sys-table__num' }
        ];
      }, 'No join request has been raised.');
    pager(card, body, { total: null, count: (d.join_requests || []).length, has_more: d.has_more });
    caption(body, 'Recruiting posts, newest first.');
    table(body, ['Guild', 'Open', 'Playstyle', 'Timezone', 'New players', 'Edited by', 'Updated'],
      d.recruiting || [], function (r) {
        return [
          { text: r.guild_id, cls: 'sys-table__num' },
          pill(r.recruiting ? 'listed' : 'closed', r.recruiting ? 'on' : 'off'),
          r.playstyle || '-',
          r.timezone || '-',
          r.new_player_friendly ? 'yes' : 'no',
          { text: r.set_by_char_name || '-', cls: 'sys-table__muted' },
          { text: r.updated_at, cls: 'sys-table__num' }
        ];
      }, 'No guild has posted a recruiting card.');
    caption(body, 'Per-guild inbox thresholds.');
    table(body, ['Guild', 'View role', 'Manage role', 'Set by', 'Updated'],
      d.inbox_configs || [], function (r) {
        return [
          { text: r.guild_id, cls: 'sys-table__num' },
          { text: r.view_min_role, cls: 'sys-table__num' },
          { text: r.manage_min_role, cls: 'sys-table__num' },
          { text: r.set_by_char_name || '-', cls: 'sys-table__muted' },
          { text: r.updated_at, cls: 'sys-table__num' }
        ];
      }, 'Every guild is on the default thresholds.');
  };

  RENDER.social_messages = function (card, body, d) {
    setCardState(card, 'static', 'admin.db');
    stats(body, [
      { label: 'Live messages', value: fmtNum(d.total) },
      { label: 'Unread', value: fmtNum(d.unread) },
      { label: 'Deleted', value: fmtNum(d.deleted) },
      { label: 'Blocks', value: fmtNum(d.blocks) }
    ]);
    caption(body, 'Volume only. Subjects and bodies are never read by this page.');
    var byDay = {};
    (d.volume_14d || []).forEach(function (r) { byDay[r.day] = (byDay[r.day] || 0) + r.n; });
    var days = Object.keys(byDay).sort().map(function (k) { return { day: k, n: byDay[k] }; });
    bars(body, days, function (p) { return p.day; }, function (p) { return p.n; });
    table(body, ['Inbox', 'State', 'Messages'], d.by_state || [], function (r) {
      return [r.recipient_kind, r.state, { text: fmtNum(r.n), cls: 'sys-table__num' }];
    });
    caption(body, d.blockers + ' player(s) hold a block, ' + d.blocks_7d +
                  ' added in the last 7 days.');
    table(body, ['Sender', 'Direct messages, 7d'], d.top_senders_7d || [], function (r) {
      return [{ text: r.sender_char_name, cls: 'sys-table__wrap-cell' },
              { text: fmtNum(r.sent), cls: 'sys-table__num' }];
    }, 'No direct message in the last 7 days.');
  };

  RENDER.social_gifts = function (card, body, d) {
    setCardState(card, 'static', 'admin.db');
    var caps = d.caps || {};
    stats(body, [
      { label: 'Gift events', value: fmtNum(d.total) },
      { label: 'Per sender per day', value: fmtNum(caps.per_identity_per_day) },
      { label: 'Per pair per day', value: fmtNum(caps.per_pair_per_day) },
      { label: 'Max per gift', value: fmtNum(caps.max_amount) }
    ]);
    var byDay = (d.volume_14d || []);
    bars(body, byDay, function (p) { return p.day; }, function (p) { return p.gifts; });
    caption(body, 'Gifts per day, 14 days.');
    table(body, ['Status', 'Events'], d.by_status || [], function (r) {
      return [r.status, { text: fmtNum(r.n), cls: 'sys-table__num' }];
    });
    table(body, ['Refusal reason, 30d', 'Count'], d.refusals_30d || [], function (r) {
      return [{ text: r.reason, cls: 'sys-table__wrap-cell' },
              { text: fmtNum(r.n), cls: 'sys-table__num' }];
    }, 'Nothing was refused in the last 30 days.');
    table(body, ['Recipient', 'Received, 30d'], d.inbound_over_threshold_30d || [],
      function (r) {
        return [{ text: r.recipient_identity, cls: 'sys-table__wrap-cell' },
                { text: fmtNum(r.received), cls: 'sys-table__num' }];
      }, 'Nobody is over the fan-in alert threshold.');
    caption(body, 'Fan-in alerts at ' + fmtNum(caps.inbound_alert_30d) +
                  ' Solari over 30 days. It is a thing to look at, never a block.');
  };

  RENDER.social_profiles = function (card, body, d) {
    setCardState(card, 'static', 'admin.db');
    stats(body, [
      { label: 'Profiles', value: fmtNum(d.total) },
      { label: 'Opted out', value: fmtNum(d.opted_out) },
      { label: 'With a blurb', value: fmtNum(d.with_blurb) },
      { label: 'Flagged', value: fmtNum(d.flagged_total),
        tone: d.flagged_total > 0 ? 'warn' : null }
    ]);
    caption(body, 'Heuristics, not verdicts: ' + (d.heuristics || []).join(', ') +
                  '. A hit is worth a look. Nothing here hides or removes anything.');
    table(body, ['Player', 'Listed', 'Flags', 'Blurb', 'Updated'], d.flagged || [],
      function (r) {
        return [
          { text: r.char_name || '-', cls: 'sys-table__wrap-cell' },
          pill(r.listed ? 'listed' : 'opted out', r.listed ? 'on' : 'off'),
          { text: (r.flags || []).join(', '), cls: 'sys-table__muted' },
          { text: r.blurb, cls: 'sys-table__wrap-cell' },
          { text: r.updated_at, cls: 'sys-table__num' }
        ];
      }, 'No profile blurb trips a heuristic.');
    pager(card, body, { total: d.flagged_total, count: (d.flagged || []).length,
                        has_more: d.has_more });
  };

  RENDER.social_lfg = function (card, body, d) {
    setCardState(card, 'static', 'admin.db');
    stats(body, [
      { label: 'Seeker cards', value: fmtNum(d.total) },
      { label: 'Active', value: fmtNum(d.active) }
    ]);
    table(body, ['Player', 'Playstyle', 'Timezone', 'Role', 'Note', 'State', 'Updated'],
      d.seekers || [], function (r) {
        return [
          { text: r.char_name || '-', cls: 'sys-table__wrap-cell' },
          r.playstyle || '-',
          r.timezone || '-',
          r.role || '-',
          { text: (r.note || '').slice(0, 120), cls: 'sys-table__wrap-cell' },
          pill(r.active ? 'active' : 'expired', r.active ? 'on' : 'off'),
          { text: r.updated_at, cls: 'sys-table__num' }
        ];
      }, 'Nobody is on the seeker wall.');
    pager(card, body, { total: d.total, count: (d.seekers || []).length, has_more: d.has_more });
  };

  /* --- Fetch loop -------------------------------------------------------- */

  function loadCard(card) {
    var url = card.getAttribute('data-src');
    var render = RENDER[card.getAttribute('data-render')];
    if (!url || !render) return Promise.resolve();
    var offset = parseInt(card.getAttribute('data-offset') || '0', 10);
    var full = url + (url.indexOf('?') === -1 ? '?' : '&') +
               'limit=' + PAGE_SIZE + '&offset=' + offset;
    return fetch(full, { credentials: 'same-origin' })
      .then(function (r) {
        if (r.status === 401) { window.location = '/admin/login'; throw new Error('Unauthorized'); }
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (d) {
        if (d.available === false) {
          showError(card, d.error || 'source unavailable');
          return;
        }
        var body = clear(cardBody(card));
        render(card, body, d);
        return true;
      })
      .catch(function (err) {
        showError(card, err.message || String(err));
        return false;
      });
  }

  /* Resolves false only when EVERY card failed, which is what the poll
     scheduler backs off on. One dead source must not slow the other eight
     down: that is the same per-source isolation the page is built on. */
  function poll() {
    var cards = root.querySelectorAll('[data-sys-card]');
    if (pollState) { pollState.setAttribute('data-state', 'live'); pollState.textContent = 'polling'; }
    var jobs = [];
    for (var i = 0; i < cards.length; i++) jobs.push(loadCard(cards[i]));
    return Promise.all(jobs).then(function (results) {
      if (pollState) { pollState.setAttribute('data-state', 'live'); pollState.textContent = 'live'; }
      if (pollTs) pollTs.textContent = 'refreshed just now';
      return results.indexOf(true) !== -1;
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    /* Nine cards, one of them an SSH round trip, every 15 seconds. window.v2Poll
       stops the whole board while the tab is hidden and backs off when the
       board is dark, so a pocketed phone costs nothing. */
    window.v2Poll(poll, POLL_MS);
  });
})();
