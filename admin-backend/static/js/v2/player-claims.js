/* Players > Claims: land-claim ownership map.
 *
 * Reads the cached /api/dune/v2/bases directory and plots one dot per claim on
 * the Hagga backdrop. No new backend call is needed to answer "who owns that":
 * the directory already carries owner, position, size and condition for all
 * ~163 claims, so hovering is instant and costs the game host nothing.
 *
 * THE ONE RULE THIS FILE EXISTS TO ENFORCE: exactly one dimension is drawn at a
 * time. Habbanya (dim 0, PvE) and Kulon (dim 1, PvP) are different worlds
 * sharing one coordinate space. Drawing both would put two unrelated bases on
 * the same pixel and invite precisely the wrong-owner answer that the
 * bases-near dimension bug produced on 2026-07-27. The instance switcher is
 * therefore a switch, never a pair of checkboxes.
 *
 * Stored backups are excluded, per the legend the payload ships: they keep a
 * world transform but nothing stands there. Their dimension_index is garbage
 * (values like 115475957 are live in the data), which is a second reason they
 * must never be drawn as claims.
 */
(function () {
  'use strict';

  var root = document.getElementById('v2-claims');
  if (!root) return;

  var VIEW = 1000;
  var CAL = {
    originX: parseFloat(root.dataset.originX),
    spanX: parseFloat(root.dataset.spanX),
    originY: parseFloat(root.dataset.originY),
    spanY: parseFloat(root.dataset.spanY)
  };
  var GAME_MAP = root.dataset.gameMap || 'HaggaBasin';

  var mapEl = document.getElementById('v2-claims-map');
  var stage = document.getElementById('v2-claims-stage');
  var svg = document.getElementById('v2-claims-svg');
  var tip = document.getElementById('v2-claims-tip');
  var side = document.getElementById('v2-claims-detail');
  var countEl = document.getElementById('v2-claims-count');
  var noteEl = document.getElementById('v2-claims-note');
  var filterEl = document.getElementById('v2-claims-filter');

  var state = {
    all: [],
    dim: parseInt(root.dataset.initialDim, 10) || 0,
    zoom: 1, panX: 0, panY: 0,
    needle: '',
    selected: null,
    legend: null,
    operators: []
  };

  // --- projection ---------------------------------------------------------

  function nx(worldX) { return (worldX - CAL.originX) / CAL.spanX * VIEW; }
  function ny(worldY) { return (worldY - CAL.originY) / CAL.spanY * VIEW; }
  function worldXFromView(vx) { return vx / VIEW * CAL.spanX + CAL.originX; }
  function worldYFromView(vy) { return vy / VIEW * CAL.spanY + CAL.originY; }

  // Pointer -> viewBox units, undoing pan and zoom.
  function pointToView(ev) {
    var r = mapEl.getBoundingClientRect();
    var px = (ev.clientX - r.left - state.panX) / state.zoom;
    var py = (ev.clientY - r.top - state.panY) / state.zoom;
    return { x: px / r.width * VIEW, y: py / r.height * VIEW };
  }

  // --- render -------------------------------------------------------------

  function activityClass(b) {
    if (b.ownership === 'orphaned') return 'orphaned';
    return b.owner_activity || 'abandoned';
  }

  function visible() {
    return state.all.filter(function (b) {
      return b.map === GAME_MAP &&
             b.ownership !== 'stored_backup' &&
             b.dimension_index === state.dim;
    });
  }

  function matches(b) {
    if (!state.needle) return true;
    var hay = [b.label, b.totem_id,
               b.owner && b.owner.name, b.owner && b.owner.account_id]
      .filter(Boolean).join(' ').toLowerCase();
    return hay.indexOf(state.needle) !== -1;
  }

  // Dots keep a constant on-screen size, so a zoomed-in view stays readable
  // instead of turning into overlapping blobs.
  function dotRadius() { return 5 / state.zoom; }

  function draw() {
    var rows = visible();
    var r = dotRadius();
    var frag = document.createDocumentFragment();
    var shown = 0;

    rows.forEach(function (b) {
      var c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      c.setAttribute('cx', nx(b.x).toFixed(2));
      c.setAttribute('cy', ny(b.y).toFixed(2));
      c.setAttribute('r', r.toFixed(2));
      c.setAttribute('class', 'v2-claim-dot v2-claim-dot--' + activityClass(b) +
        (matches(b) ? '' : ' v2-claim-dot--muted') +
        (state.selected === b.totem_id ? ' v2-claim-dot--selected' : ''));
      c.dataset.totem = b.totem_id;
      c.setAttribute('tabindex', matches(b) ? '0' : '-1');
      c.setAttribute('role', 'button');
      c.setAttribute('aria-label', 'Select claim ' + (b.label || b.totem_id));
      if (matches(b)) shown += 1;
      frag.appendChild(c);
    });

    if (state.selected) {
      var sel = rows.filter(function (b) { return b.totem_id === state.selected; })[0];
      if (sel) {
        var ring = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        ring.setAttribute('cx', nx(sel.x).toFixed(2));
        ring.setAttribute('cy', ny(sel.y).toFixed(2));
        ring.setAttribute('r', (r * 2.6).toFixed(2));
        ring.setAttribute('class', 'v2-claim-ping');
        frag.appendChild(ring);
      }
    }

    svg.textContent = '';
    svg.appendChild(frag);
    countEl.textContent = state.needle
      ? shown + ' of ' + rows.length + ' claims'
      : rows.length + ' claims';
  }

  function applyTransform() {
    stage.style.transform =
      'translate(' + state.panX + 'px,' + state.panY + 'px) scale(' + state.zoom + ')';
    // Radius depends on zoom, so the dots have to be re-emitted, not just moved.
    draw();
  }

  // --- tooltip + detail ---------------------------------------------------

  function byId(id) {
    return state.all.filter(function (b) { return String(b.totem_id) === String(id); })[0];
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch];
    });
  }

  function ownerLine(b) {
    if (!b.owner) return '<span class="v2-claims-tip__warn">ORPHANED, no owner on record</span>';
    var away = b.owner.days_away == null ? 'unknown' : b.owner.days_away + 'd ago';
    return '<span class="v2-claims-tip__owner">' + esc(b.owner.name) + '</span> ' +
           '(' + esc(b.owner.account_id) + ') last seen ' + esc(away) +
           (b.owner.online ? ' — ONLINE' : '');
  }

  function showTip(b, ev) {
    tip.innerHTML =
      '<div class="v2-claims-tip__name">' + esc(b.label || 'unnamed claim') + '</div>' +
      '<div>' + ownerLine(b) + '</div>' +
      '<div class="v2-claims-tip__meta">' +
        esc(b.pieces) + ' pieces · ' + esc(b.placeables) + ' placeables · ' +
        esc(b.health_pct) + '% condition · ' + esc(b.claim_segments) + ' segments' +
      '</div>';
    tip.hidden = false;
    var pad = 14;
    var w = tip.offsetWidth, h = tip.offsetHeight;
    var x = ev.clientX + pad, y = ev.clientY + pad;
    if (x + w > window.innerWidth - 8) x = ev.clientX - w - pad;
    if (y + h > window.innerHeight - 8) y = ev.clientY - h - pad;
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  }

  function hideTip() { tip.hidden = true; }

  function renderDetail(b, extra) {
    if (!b) {
      side.innerHTML = '<p class="v2-claims-side__hint">Hover a claim for a summary. ' +
        'Click one to pin it here, or click bare terrain for the nearest claims to that point.</p>';
      return;
    }
    var holders = (b.holders || []).filter(function (h) { return h.rank !== 1; });
    side.innerHTML =
      '<dl>' +
      '<dt>Name</dt><dd>' + esc(b.label || '(unnamed)') + '</dd>' +
      '<dt>Owner</dt><dd>' + (b.owner ? esc(b.owner.name) : 'ORPHANED') + '</dd>' +
      (b.owner ? '<dt>Account</dt><dd>' + esc(b.owner.account_id) + '</dd>' : '') +
      (b.owner ? '<dt>Last seen</dt><dd>' + esc(b.owner.last_login || '?') + '</dd>' : '') +
      '<dt>Activity</dt><dd>' + esc(b.owner_activity || 'n/a') + '</dd>' +
      '<dt>Co-holders</dt><dd>' + holders.length +
        (holders.length ? ' (' + holders.map(function (h) { return esc(h.name); }).join(', ') + ')' : '') +
      '</dd>' +
      '<dt>Pieces</dt><dd>' + esc(b.pieces) + '</dd>' +
      '<dt>Placeables</dt><dd>' + esc(b.placeables) + '</dd>' +
      '<dt>Condition</dt><dd>' + esc(b.health_pct) + '%</dd>' +
      '<dt>Segments</dt><dd>' + esc(b.claim_segments) + '</dd>' +
      '<dt>Totem</dt><dd>' + esc(b.totem_id) + '</dd>' +
      '<dt>World</dt><dd>' + esc(b.x) + ', ' + esc(b.y) + '</dd>' +
      '</dl>' +
      '<div id="v2-claims-options" class="v2-claims-options">' +
        '<p class="v2-claims-side__hint">Checking what can be done…</p>' +
      '</div>' + (extra || '');
    var vaultButton = document.createElement('button');
    vaultButton.type = 'button';
    vaultButton.className = 'btn btn-sm bv-open';
    vaultButton.dataset.baseVault = b.totem_id;
    vaultButton.textContent = 'Open Base Vault';
    side.insertBefore(vaultButton, document.getElementById('v2-claims-options'));
    loadOptions(b.totem_id);
  }

  // What an admin can do about this base. Two courses of action, and the order
  // is deliberate: backing the base into the OWNER's own tool frees the plot
  // without dispossessing anyone or spending an admin claim slot, so it is
  // listed first. Adopting is the heavier hammer.
  //
  // Read-only. This renders the decision and its blockers; it does not fire
  // anything. The write paths sit behind environment flags on the game host and
  // are not reachable from the browser at all, which is the correct place for
  // an irreversible act to live until someone has read the blockers.
  function loadOptions(totemId) {
    fetch('/admin/api/dune/v2/claims/' + encodeURIComponent(totemId) + '/options',
          { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        state.operators = d.operators || [];
        var el = document.getElementById('v2-claims-options');
        if (!el) return;
        if (!d.available) {
          el.innerHTML = '<p class="v2-claims-side__hint">Options unavailable: ' +
            esc(d.error || 'unknown') + '</p>';
          return;
        }
        el.innerHTML =
          '<h2>What can be done</h2>' +
          optionBlock('Back up to owner\u2019s tool', d.backup.eligible,
            d.backup.blockers, d.backup.warnings,
            'Plot frees, owner keeps everything, no admin slot used.' +
            (d.backup.owner_has_tool ? '' :
              ' Owner has NO tool; grant one first or they cannot rebuild.') +
            ' Does NOT despawn the structures: only a restart clears them.',
            'backup', totemId) +
          optionBlock('Adopt, owner kept as co-holder', d.adopt.eligible,
            d.adopt.refusal ? [d.adopt.refusal] : [], [],
            'Costs you a claim slot (cap 3). Reversible. Adopt first if you want ' +
            'to clear the plot for real, then remove it with your own tool in game.',
            'adopt', totemId);
      })
      .catch(function (e) {
        var el = document.getElementById('v2-claims-options');
        if (el) el.innerHTML = '<p class="v2-claims-side__hint">Options failed: ' +
          esc(e.message) + '</p>';
      });
  }

  function optionBlock(title, eligible, blockers, warns, note, action, totemId) {
    var html = '<div class="v2-claims-opt v2-claims-opt--' +
      (eligible ? 'ok' : 'blocked') + '">' +
      '<b>' + esc(title) + '</b>' +
      '<span class="v2-claims-opt__note">' + esc(note) + '</span>';
    (blockers || []).forEach(function (x) {
      html += '<span class="v2-claims-opt__blocker">blocked: ' + esc(x) + '</span>';
    });
    (warns || []).forEach(function (x) {
      html += '<span class="v2-claims-opt__warn">note: ' + esc(x) + '</span>';
    });
    if (eligible) {
      if (action === 'adopt') {
        // A picker, not a text box: nobody should have to remember a dune
        // account id. Headroom is shown per operator and anyone already at the
        // cap is disabled, so the 3-claim limit is visible before the click
        // rather than as a refusal after it.
        var last = localStorage.getItem('holAdminDuneAccount') || '';
        var opts = (state.operators || []).map(function (o) {
          var full = o.free_slots <= 0;
          return '<option value="' + esc(o.account_id) + '"' +
            (full ? ' disabled' : '') +
            (String(o.account_id) === last && !full ? ' selected' : '') + '>' +
            esc(o.name) + ' — ' + esc(o.claims) + '/' + esc(o.cap) +
            (full ? ' (full)' : '') + '</option>';
        }).join('');
        html += '<label class="v2-claims-opt__acct">adopt onto' +
          (opts
            ? '<select id="v2-claims-acct"><option value="">choose…</option>' +
              opts + '</select>'
            : '<input type="text" inputmode="numeric" id="v2-claims-acct" ' +
              'value="' + esc(last) + '" placeholder="dune account id">') +
          '</label>';
      }
      html += '<button type="button" class="v2-claims-opt__go" ' +
        'data-claims-action="' + esc(action) + '" ' +
        'data-claims-totem="' + esc(totemId) + '">' +
        (action === 'backup' ? 'Back up to owner' : 'Adopt as co-holder') +
        '</button>';
    }
    return html + '</div>';
  }

  // Both actions are real writes against a live game database. Backing up is
  // irreversible AND, per the 07-27 session, does not despawn the structures:
  // only an owner using the in-game tool does that, so a plot backed up this way
  // stays visibly occupied until a restart. Say so at the point of decision
  // rather than in a doc nobody has open.
  var CONFIRM = {
    backup: {
      typed: 'BACKUP',
      text: 'This puts the base into the OWNER’s tool and frees the claim. ' +
            'It CANNOT be undone.\n\nIt does NOT make the base disappear: nothing ' +
            'in the database can do that. The structures stand until the map ' +
            'reloads, and Hagga only reloads on a restart. If you need the plot ' +
            'visibly clear now, cancel and adopt it instead, then remove it with ' +
            'your own tool in game.\n\nType BACKUP to confirm.'
    },
    adopt: {
      typed: null,
      text: 'Adopt this claim? You become the owner and the current owner stays ' +
            'on as a co-holder, so they keep access. This costs one of your 3 ' +
            'claim slots and can be reverted.'
    }
  };

  document.addEventListener('click', function (ev) {
    var btn = ev.target.closest && ev.target.closest('[data-claims-action]');
    if (!btn) return;
    var action = btn.dataset.claimsAction;
    var totemId = btn.dataset.claimsTotem;
    var conf = CONFIRM[action];
    if (!conf) return;

    if (conf.typed) {
      var answer = window.prompt(conf.text, '');
      if (answer !== conf.typed) return;
    } else if (!window.confirm(conf.text)) {
      return;
    }

    var path = '/admin/api/dune/v2/claims/' + encodeURIComponent(totemId) + '/' + action;
    if (action === 'adopt') {
      var el = document.getElementById('v2-claims-acct');
      var acct = (el && el.value || '').trim();
      if (!/^[0-9]{1,19}$/.test(acct)) {
        window.alert('Enter your numeric dune account id first.');
        return;
      }
      localStorage.setItem('holAdminDuneAccount', acct);
      path += '?account_id=' + encodeURIComponent(acct);
    }

    btn.disabled = true;
    btn.textContent = 'working…';
    apiCall('POST', path, null)
      .then(function (res) {
        var okd = res && (res.ok === true || res.available === true);
        btn.textContent = okd ? 'done' : 'failed';
        var msg = okd ? 'Applied.' : ('Refused: ' +
          ((res.blockers && res.blockers.join('; ')) || res.error || 'see audit log'));
        window.alert(msg);
        // The directory is 5-minute cached, so re-read options rather than the
        // whole map: the panel must not keep showing an action already taken.
        loadOptions(totemId);
      })
      .catch(function (e) {
        btn.disabled = false;
        btn.textContent = 'retry';
        window.alert('Failed: ' + e.message);
      });
  });

  // --- nearest-claim lookup for a click on bare terrain -------------------

  function nearAt(vx, vy) {
    var wx = Math.round(worldXFromView(vx));
    var wy = Math.round(worldYFromView(vy));
    var url = '/admin/api/dune/v2/bases/near?map_name=' + encodeURIComponent(GAME_MAP) +
              '&dim=' + state.dim + '&x=' + wx + '&y=' + wy + '&limit=5';
    side.innerHTML = '<p class="v2-claims-side__hint">Looking up ' + wx + ', ' + wy + '…</p>';
    fetch(url, { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.available) {
          side.innerHTML = '<div class="v2-claims-error">Lookup failed: ' +
            esc(d.error || 'unknown') + '</div>';
          return;
        }
        var rows = d.bases || [];
        if (!rows.length) {
          side.innerHTML = '<p class="v2-claims-side__hint">No claims near ' +
            wx + ', ' + wy + ' in this world.</p>';
          return;
        }
        state.selected = rows[0].totem_id;
        draw();
        var list = rows.map(function (n) {
          return '<dt>' + esc(n.distance_m) + ' m</dt><dd>' +
                 (n.owner ? esc(n.owner.name) : 'ORPHANED') + ' — ' +
                 esc(n.label || n.totem_id) + '</dd>';
        }).join('');
        var full = byId(rows[0].totem_id);
        renderDetail(full, '<h2>Nearest to ' + wx + ', ' + wy + '</h2><dl>' + list + '</dl>');
      })
      .catch(function (e) {
        side.innerHTML = '<div class="v2-claims-error">Lookup failed: ' + esc(e.message) + '</div>';
      });
  }

  // --- events -------------------------------------------------------------

  svg.addEventListener('mousemove', function (ev) {
    var t = ev.target;
    if (t && t.dataset && t.dataset.totem) {
      var b = byId(t.dataset.totem);
      if (b) { showTip(b, ev); return; }
    }
    hideTip();
  });
  svg.addEventListener('mouseleave', hideTip);
  svg.addEventListener('keydown', function (ev) {
    if (ev.key !== 'Enter' && ev.key !== ' ') return;
    var t = ev.target;
    if (!t || !t.dataset || !t.dataset.totem) return;
    ev.preventDefault();
    state.selected = parseInt(t.dataset.totem, 10);
    renderDetail(byId(state.selected));
    var vaultButton = side.querySelector('[data-base-vault]');
    if (vaultButton) vaultButton.focus();
  });

  var dragged = false;
  mapEl.addEventListener('click', function (ev) {
    if (dragged) { dragged = false; return; }
    var t = ev.target;
    if (t && t.dataset && t.dataset.totem) {
      state.selected = parseInt(t.dataset.totem, 10);
      draw();
      renderDetail(byId(state.selected));
      return;
    }
    var p = pointToView(ev);
    nearAt(p.x, p.y);
  });

  // pan
  var down = null;
  mapEl.addEventListener('pointerdown', function (ev) {
    down = { x: ev.clientX, y: ev.clientY, panX: state.panX, panY: state.panY };
    dragged = false;
    mapEl.classList.add('v2-claims-map--dragging');
    mapEl.setPointerCapture(ev.pointerId);
  });
  mapEl.addEventListener('pointermove', function (ev) {
    if (!down) return;
    var dx = ev.clientX - down.x, dy = ev.clientY - down.y;
    if (Math.abs(dx) > 3 || Math.abs(dy) > 3) { dragged = true; hideTip(); }
    state.panX = down.panX + dx;
    state.panY = down.panY + dy;
    stage.style.transform =
      'translate(' + state.panX + 'px,' + state.panY + 'px) scale(' + state.zoom + ')';
  });
  function endDrag(ev) {
    if (!down) return;
    down = null;
    mapEl.classList.remove('v2-claims-map--dragging');
    if (ev && ev.pointerId != null && mapEl.hasPointerCapture(ev.pointerId)) {
      mapEl.releasePointerCapture(ev.pointerId);
    }
  }
  mapEl.addEventListener('pointerup', endDrag);
  mapEl.addEventListener('pointercancel', endDrag);

  // zoom about the cursor, so the terrain under the pointer stays put
  function zoomBy(factor, cx, cy) {
    var next = Math.max(1, Math.min(state.zoom * factor, 12));
    if (next === state.zoom) return;
    var r = mapEl.getBoundingClientRect();
    var ox = (cx == null ? r.width / 2 : cx - r.left);
    var oy = (cy == null ? r.height / 2 : cy - r.top);
    state.panX = ox - (ox - state.panX) * (next / state.zoom);
    state.panY = oy - (oy - state.panY) * (next / state.zoom);
    state.zoom = next;
    clampPan();
    applyTransform();
  }
  // Keep the backdrop covering the frame; panning into blank space is
  // disorienting and there is nothing out there to look at.
  function clampPan() {
    var r = mapEl.getBoundingClientRect();
    var minX = r.width - r.width * state.zoom;
    var minY = r.height - r.height * state.zoom;
    state.panX = Math.min(0, Math.max(minX, state.panX));
    state.panY = Math.min(0, Math.max(minY, state.panY));
  }
  mapEl.addEventListener('wheel', function (ev) {
    ev.preventDefault();
    zoomBy(ev.deltaY < 0 ? 1.2 : 1 / 1.2, ev.clientX, ev.clientY);
  }, { passive: false });

  document.getElementById('v2-claims-zoom-in')
    .addEventListener('click', function () { zoomBy(1.4); });
  document.getElementById('v2-claims-zoom-out')
    .addEventListener('click', function () { zoomBy(1 / 1.4); });
  document.getElementById('v2-claims-zoom-reset')
    .addEventListener('click', function () {
      state.zoom = 1; state.panX = 0; state.panY = 0; applyTransform();
    });

  Array.prototype.forEach.call(
    root.querySelectorAll('[data-claims-dim]'), function (btn) {
      btn.addEventListener('click', function () {
        state.dim = parseInt(btn.dataset.claimsDim, 10);
        state.selected = null;
        Array.prototype.forEach.call(
          root.querySelectorAll('[data-claims-dim]'), function (b) {
            b.setAttribute('aria-pressed', String(b === btn));
          });
        renderDetail(null);
        draw();
      });
    });

  filterEl.addEventListener('input', function () {
    state.needle = filterEl.value.trim().toLowerCase();
    draw();
  });

  // --- legend + off-map note ---------------------------------------------

  function renderLegend(legend) {
    var order = [
      ['active', 'active'], ['quiet', 'quiet'],
      ['dormant', 'dormant'], ['abandoned', 'abandoned']
    ];
    var act = (legend && legend.owner_activity) || {};
    var own = (legend && legend.ownership) || {};
    var html = '<div class="v2-claims-legend">';
    order.forEach(function (pair) {
      html += '<div class="v2-claims-legend__row">' +
        '<span class="v2-claims-legend__swatch v2-claim-legend--' + pair[0] +
        '" style="background:var(' + swatchVar(pair[0]) + ')"></span>' +
        '<span class="v2-claims-legend__label"><b>' + esc(pair[1]) + '</b>' +
        '<span>' + esc(act[pair[0]] || '') + '</span></span></div>';
    });
    html += '<div class="v2-claims-legend__row">' +
      '<span class="v2-claims-legend__swatch" style="background:var(--grace-period)"></span>' +
      '<span class="v2-claims-legend__label"><b>orphaned</b><span>' +
      esc(own.orphaned || '') + '</span></span></div>';
    html += '</div>';
    // The definitions come from the payload rather than this file on purpose:
    // a legend that restates the rules is a legend that drifts away from them.
    html += '<p class="v2-claims-note">' + esc(act._what || '') + '</p>';
    return html;
  }

  function swatchVar(k) {
    return { active: '--green', quiet: '--yellow',
             dormant: '--accent-bright', abandoned: '--red' }[k] || '--text-muted';
  }

  function renderNote(data) {
    var stored = state.all.filter(function (b) { return b.ownership === 'stored_backup'; }).length;
    var offMap = state.all.filter(function (b) {
      return b.map !== GAME_MAP && b.ownership !== 'stored_backup';
    });
    var bits = [];
    if (stored) {
      bits.push(stored + ' stored backup' + (stored === 1 ? '' : 's') +
        ' hidden: they keep a world position but nothing stands there.');
    }
    if (offMap.length) {
      bits.push(offMap.length + ' claim' + (offMap.length === 1 ? '' : 's') +
        ' on other maps not drawn here (' +
        offMap.map(function (b) { return esc(b.map) + ' dim ' + b.dimension_index; }).join(', ') +
        '); this backdrop is Hagga only.');
    }
    if (data.stale) bits.push('Serving a cached copy; the relay was unreachable on the last refresh.');
    noteEl.innerHTML = bits.join(' ') +
      ' Directory is cached for 5 minutes. Positions are claim totems, not players.';
  }

  // --- load ---------------------------------------------------------------

  fetch('/admin/api/dune/v2/bases', { credentials: 'same-origin' })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (!d.available) {
        side.innerHTML = '<div class="v2-claims-error">Directory unavailable: ' +
          esc(d.error || 'unknown') + '</div>';
        countEl.textContent = 'unavailable';
        return;
      }
      state.all = d.bases || [];
      state.legend = d.legend || null;
      draw();
      renderDetail(null);
      // Separate element from the detail pane: renderDetail replaces its own
      // innerHTML on every hover-to-pin, and the legend must survive that.
      document.getElementById('v2-claims-legend').innerHTML = renderLegend(state.legend);
      renderNote(d);
    })
    .catch(function (e) {
      side.innerHTML = '<div class="v2-claims-error">Directory failed to load: ' +
        esc(e.message) + '</div>';
      countEl.textContent = 'unavailable';
    });
})();
