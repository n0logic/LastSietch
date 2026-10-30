/* Bases (plan A1): the unified base directory as a map or a list.
 *
 * Reads the cached A0 directory (/api/dune/v2/bases/directory) once and renders
 * both views from it. Read-only: nothing here posts. The drawer's Vault tab
 * hands off to the shared Base Vault panel (base-vault.js, via data-base-vault)
 * and, for a packed backup, loads the BRT backup fragment whose Base Vault
 * section is backup-vault.js.
 *
 * Map rule, inherited from the Claims map: one dimension at a time, because the
 * Hagga instances share one coordinate space. Packed backups are the exception:
 * they are not claims, the BRT rebuilds them at their stored origin in whatever
 * instance the owner stands in, so their hollow marker shows on every instance.
 *
 * Every value from the directory is written with textContent, never as markup.
 */
(function () {
  'use strict';

  var root = document.getElementById('ls-bases');
  if (!root) return;

  var VIEW = 1000;
  var API = '/admin/api/dune/v2/bases/directory';
  var MAPS = [];
  try { MAPS = JSON.parse(root.dataset.maps || '[]'); } catch (_) { MAPS = []; }

  var mapEl = document.getElementById('ls-bases-map');
  var stage = document.getElementById('ls-bases-stage');
  var img = document.getElementById('ls-bases-img');
  var svg = document.getElementById('ls-bases-svg');
  var tip = document.getElementById('ls-bases-tip');
  var countEl = document.getElementById('ls-bases-count');
  var noteEl = document.getElementById('ls-bases-note');
  var filterEl = document.getElementById('ls-bases-filter');
  var tbody = document.getElementById('ls-bases-rows');
  var emptyEl = document.getElementById('ls-bases-empty');
  var drawer = document.getElementById('ls-bases-drawer');

  var state = {
    data: null, rows: [], view: root.dataset.view === 'list' ? 'list' : 'map',
    status: '', needle: '', map: MAPS[0] || null, dim: 0,
    zoom: 1, panX: 0, panY: 0, selected: null
  };
  if (state.map && state.map.dims.length) state.dim = state.map.dims[0].dim;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }
  function svgEl(tag) { return document.createElementNS('http://www.w3.org/2000/svg', tag); }
  function fmt(n) { return Number.isFinite(n) ? n.toLocaleString() : 'Unknown'; }

  // --- row facts ----------------------------------------------------------

  function ownerName(r) {
    if (r.owner && r.owner.name) return r.owner.name;
    return r.status === 'archived' ? 'Not in the game database' : 'No owner on record';
  }
  function statusText(r) {
    if (r.status === 'packed' && r.backup_id) {
      var b = r.backup || {};
      return 'Packed #' + r.backup_id + (Number.isInteger(b.slots_used) ? ' (' + b.slots_used + '/' + (b.slots_cap || 3) + ')' : '');
    }
    return r.status_label || r.status;
  }
  function whereText(r) {
    var dims = {};
    MAPS.forEach(function (m) { m.dims.forEach(function (d) { dims[m.engine_map + ':' + d.dim] = d.label; }); });
    if (!r.map) return 'Unknown';
    if (r.status === 'packed') return r.map + ' (parked)';
    var label = dims[r.map + ':' + r.dimension_index];
    return label || (r.map + (r.dimension_index != null ? ' dim ' + r.dimension_index : ''));
  }
  function loadedText(r) {
    if (r.status === 'held') return 'Not loaded (held)';
    if (r.status === 'packed' || r.status === 'archived') return 'n/a';
    return r.resident === true ? 'Loaded' : r.resident === false ? 'Not loaded' : 'Unknown';
  }
  function vaultText(r) {
    var v = r.vault || {};
    if (!state.data || !state.data.sources.vault) return 'Unknown';
    if (!v.snapshots) return 'No snapshot';
    var age = v.age_days == null ? 'unknown age' : v.age_days === 0 ? 'today' : v.age_days + 'd ago';
    var check = v.verified === true ? ', verified' : v.verified === false ? ', verify FAILED' : '';
    return age + check + ' (' + v.snapshots + ')';
  }
  function brtText(r) {
    if (r.status === 'packed') return r.backup && r.backup.linked_actors != null ? fmt(r.backup.linked_actors) + ' actors' : '';
    var bits = [];
    if (Number.isInteger(r.owner_backup_slots)) bits.push(r.owner_backup_slots + '/' + (state.data.slot_cap || 3) + ' slots');
    if (Number.isInteger(r.owner_backup_tools)) bits.push(r.owner_backup_tools ? 'has tool' : 'no tool');
    return bits.join(', ');
  }
  function matches(r) {
    if (state.status && r.status !== state.status) return false;
    if (!state.needle) return true;
    var hay = [r.label, r.totem_id, r.backup_id, r.owner && r.owner.name, r.owner && r.owner.account_id]
      .filter(function (x) { return x != null && x !== ''; }).join(' ').toLowerCase();
    return hay.indexOf(state.needle) !== -1;
  }
  function byKey(key) {
    for (var i = 0; i < state.rows.length; i++) if (state.rows[i].key === key) return state.rows[i];
    return null;
  }

  // --- map ----------------------------------------------------------------

  function nx(x) { return (x - state.map.cal.originX) / state.map.cal.spanX * VIEW; }
  function ny(y) { return (y - state.map.cal.originY) / state.map.cal.spanY * VIEW; }
  function inView(vx, vy) { return vx >= 0 && vx <= VIEW && vy >= 0 && vy <= VIEW; }

  function onMap(r) {
    if (!r.marker || !state.map || r.map !== state.map.engine_map) return false;
    if (r.status !== 'packed' && r.dimension_index !== state.dim) return false;
    return inView(nx(r.marker.x), ny(r.marker.y));
  }
  function dotClass(r) {
    if (r.status === 'packed') return 'v2-claim-dot ls-bases-dot--packed';
    if (r.status === 'orphaned') return 'v2-claim-dot v2-claim-dot--orphaned';
    var cls = 'v2-claim-dot v2-claim-dot--' + (r.owner_activity || 'abandoned');
    return r.status === 'held' ? cls + ' ls-bases-dot--held' : cls;
  }

  function drawMap() {
    if (!state.map) return;
    var radius = 5 / state.zoom;
    var frag = document.createDocumentFragment();
    var shown = 0, total = 0;
    state.rows.forEach(function (r) {
      if (!onMap(r)) return;
      total += 1;
      var c = svgEl('circle');
      var hit = matches(r);
      c.setAttribute('cx', nx(r.marker.x).toFixed(2));
      c.setAttribute('cy', ny(r.marker.y).toFixed(2));
      c.setAttribute('r', (r.status === 'packed' ? radius * 1.3 : radius).toFixed(2));
      c.setAttribute('class', dotClass(r) + (hit ? '' : ' v2-claim-dot--muted') +
        (state.selected === r.key ? ' v2-claim-dot--selected' : ''));
      c.setAttribute('tabindex', hit ? '0' : '-1');
      c.setAttribute('role', 'button');
      c.setAttribute('aria-label', 'Open ' + (r.label || 'base ' + (r.totem_id || r.backup_id)) + ', ' + statusText(r));
      c.dataset.key = r.key;
      if (hit) shown += 1;
      frag.appendChild(c);
    });
    svg.replaceChildren(frag);
    if (state.view === 'map') countEl.textContent = (state.needle || state.status ? shown + ' of ' : '') + total + ' on this map';
    renderNote();
  }

  function renderNote() {
    if (!state.data || !state.map) return;
    var off = state.rows.filter(function (r) {
      return r.marker && (r.map !== state.map.engine_map || !inView(nx(r.marker.x), ny(r.marker.y)));
    });
    var unplaced = state.rows.filter(function (r) { return !r.marker; }).length;
    var bits = [];
    var missing = Object.keys(state.data.sources || {}).filter(function (k) { return !state.data.sources[k]; });
    if (missing.length) bits.push('Some sources did not answer (' + missing.join(', ') + '); their columns read Unknown.');
    if (off.length) bits.push(off.length + ' with a position on another map or outside this one (a packed Deep Desert origin sits outside Hagga).');
    if (unplaced) bits.push(unplaced + ' without a position (archived only, or a backup without its totem). Use the List.');
    if (state.data.stale) bits.push('Serving a cached copy; the relay was unreachable on the last refresh.');
    noteEl.textContent = bits.join(' ') + ' Directory is cached for 5 minutes. Positions are claim totems, not players.';
  }

  function applyTransform() {
    stage.style.transform = 'translate(' + state.panX + 'px,' + state.panY + 'px) scale(' + state.zoom + ')';
    drawMap();
  }

  function backdrop() {
    if (!state.map) return;
    if (state.map.img) {
      img.src = state.map.img;
      img.hidden = false;
      return;
    }
    img.hidden = true;
    img.removeAttribute('src');
    // Deep Desert's rock layout changes with Coriolis: ask the World map which one is on.
    var wanted = state.map.key;
    fetch('/admin/api/dune/v2/world/dd-layout', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (state.map.key !== wanted || !d || typeof d.backdrop !== 'string' || d.backdrop.indexOf('/admin/static/') !== 0) return;
        img.src = d.backdrop;
        img.hidden = false;
      })
      .catch(function () {});
  }

  function buttons(container, items, pressed, onPick) {
    container.replaceChildren();
    items.forEach(function (item) {
      var b = el('button', 'v2-claims-instances__btn', item.label);
      b.type = 'button';
      b.setAttribute('aria-pressed', String(item.value === pressed));
      b.addEventListener('click', function () { onPick(item.value); });
      container.append(b);
    });
  }

  function renderMapTools() {
    buttons(document.getElementById('ls-bases-maps'),
      MAPS.map(function (m) { return { value: m.key, label: m.label }; }), state.map && state.map.key,
      function (key) {
        state.map = MAPS.filter(function (m) { return m.key === key; })[0];
        state.dim = state.map.dims[0].dim;
        state.zoom = 1; state.panX = 0; state.panY = 0;
        renderMapTools(); backdrop(); applyTransform();
      });
    buttons(document.getElementById('ls-bases-dims'),
      (state.map ? state.map.dims : []).map(function (d) { return { value: d.dim, label: d.label }; }), state.dim,
      function (dim) { state.dim = dim; renderMapTools(); drawMap(); });
  }

  // --- tooltip ------------------------------------------------------------

  function showTip(r, ev) {
    tip.replaceChildren(el('div', 'v2-claims-tip__name', r.label || 'Unnamed base'),
      el('div', '', ownerName(r) + ' \u00b7 ' + statusText(r)),
      el('div', 'v2-claims-tip__meta', 'Vault: ' + vaultText(r)));
    tip.hidden = false;
    var pad = 14, x = ev.clientX + pad, y = ev.clientY + pad;
    if (x + tip.offsetWidth > window.innerWidth - 8) x = ev.clientX - tip.offsetWidth - pad;
    if (y + tip.offsetHeight > window.innerHeight - 8) y = ev.clientY - tip.offsetHeight - pad;
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  }

  // --- list ---------------------------------------------------------------

  var ORDER = { placed: 0, held: 1, orphaned: 2, packed: 3, archived: 4 };

  function drawList() {
    var rows = state.rows.filter(matches).sort(function (a, b) {
      return (ORDER[a.status] - ORDER[b.status]) || String(a.label).localeCompare(String(b.label));
    });
    var frag = document.createDocumentFragment();
    rows.forEach(function (r) {
      var tr = el('tr');
      var name = el('td', '', r.label || 'Unnamed base');
      name.append(el('span', 'ls-bases-table__sub',
        (r.totem_id ? 'totem ' + r.totem_id : '') + (r.backup_id ? (r.totem_id ? ' \u00b7 ' : '') + 'backup #' + r.backup_id : '')));
      var owner = el('td', '', ownerName(r));
      if (r.owner && r.owner.account_id) owner.append(el('span', 'ls-bases-table__sub', '#' + r.owner.account_id));
      var status = el('td');
      status.append(el('span', 'ls-pill ls-pill--' + r.status, statusText(r)));
      var vault = el('td', r.vault && r.vault.verified === false ? 'ls-warn' : '', vaultText(r));
      var open = el('button', 'btn btn-sm', 'Details');
      open.type = 'button';
      open.dataset.basesOpen = r.key;
      var action = el('td');
      action.append(open);
      tr.append(name, owner, status, el('td', '', whereText(r)), el('td', '', loadedText(r)), vault, el('td', '', brtText(r)), action);
      frag.append(tr);
    });
    tbody.replaceChildren(frag);
    emptyEl.hidden = rows.length > 0 || !state.data;
    if (state.view === 'list') countEl.textContent = rows.length + ' of ' + state.rows.length + ' bases';
  }

  function renderChips() {
    var counts = (state.data && state.data.counts) || {};
    Array.prototype.forEach.call(root.querySelectorAll('[data-bases-status]'), function (chip) {
      var key = chip.dataset.basesStatus;
      var n = key ? counts[key] : state.rows.length;
      var old = chip.querySelector('.ls-chip__n');
      if (old) old.remove();
      if (Number.isInteger(n)) chip.append(el('span', 'ls-chip__n', String(n)));
      chip.setAttribute('aria-pressed', String(key === state.status));
    });
  }

  // --- drawer -------------------------------------------------------------

  function dl(pairs) {
    var list = el('dl', 'ls-bases-dl');
    pairs.forEach(function (p) {
      if (p[1] == null || p[1] === '') return;
      list.append(el('dt', '', p[0]), el('dd', '', String(p[1])));
    });
    return list;
  }

  function showTab(name) {
    ['overview', 'vault', 'actions'].forEach(function (t) {
      document.getElementById('ls-bases-tab-' + t).setAttribute('aria-selected', String(t === name));
      document.getElementById('ls-bases-pane-' + t).hidden = t !== name;
    });
  }

  function openDrawer(r) {
    state.selected = r.key;
    document.getElementById('ls-bases-drawer-title').textContent = r.label || 'Unnamed base';
    var overview = document.getElementById('ls-bases-pane-overview');
    var holders = r.holders ? r.holders + ' co-holder' + (r.holders === 1 ? '' : 's') : null;
    overview.replaceChildren(dl([
      ['Status', statusText(r)],
      ['Owner', ownerName(r)],
      ['Account', r.owner && r.owner.account_id],
      ['Owner last seen', r.owner && r.owner.days_away != null ? r.owner.days_away + ' days ago' : null],
      ['Activity', r.owner_activity],
      ['Co-holders', holders],
      ['Where', whereText(r)],
      ['Loaded', loadedText(r)],
      ['Pieces', r.pieces], ['Placeables', r.placeables],
      ['Condition', r.health_pct != null ? r.health_pct + '%' : null],
      ['Totem', r.totem_id], ['Backup', r.backup_id ? '#' + r.backup_id : null],
      ['Owner BRT slots', Number.isInteger(r.owner_backup_slots) ? r.owner_backup_slots + '/' + state.data.slot_cap : null],
      ['Owner BaseBackupTool', Number.isInteger(r.owner_backup_tools) ? (r.owner_backup_tools ? r.owner_backup_tools + ' in inventory' : 'none') : null],
      ['Map position', r.marker ? r.marker.x + ', ' + r.marker.y + (r.marker.kind === 'origin' ? ' (stored origin)' : r.marker.kind === 'parked' ? ' (parked)' : '') : null]
    ]));
    if (r.status === 'placed' && r.resident) {
      overview.append(el('p', 'ls-bases-pane-note', 'Loaded: this base lives in a running server\'s memory. A database edit is overwritten by the next shutdown save, so any change waits for a window.'));
    }

    var vault = document.getElementById('ls-bases-pane-vault');
    vault.replaceChildren(dl([['Snapshots', r.vault ? r.vault.snapshots : null], ['Last capture', vaultText(r)],
      ['Last live-base capture', r.vault && r.vault.last_live_captured_at ? new Date(r.vault.last_live_captured_at).toLocaleString() : null]]));
    if (r.totem_id && r.status !== 'packed') {
      var open = el('button', 'btn btn-sm bv-open', 'Open Base Vault');
      open.type = 'button';
      open.dataset.baseVault = r.totem_id;
      vault.append(open);
    }
    if (r.status === 'packed' && r.backup_id) {
      var slot = el('div');
      slot.setAttribute('aria-busy', 'true');
      vault.append(slot);
      if (window.htmx) {
        window.htmx.ajax('GET', '/admin/api/dune/v2/players/bases/' + encodeURIComponent(r.backup_id) + '/_detail',
          { target: slot, swap: 'innerHTML' });
      }
    }

    var actions = document.getElementById('ls-bases-pane-actions');
    var link = el('a', '', 'Open Claims');
    link.href = '/admin/v2/players/claims';
    var p = el('p', 'ls-bases-pane-note', 'This page is read-only. Back up to owner and Adopt still run from ');
    p.append(link, document.createTextNode('. The guided wizards (Vault to BRT, Relocate, Pack) arrive with the next wave, each with a dry run, a typed confirm and a named approver.'));
    actions.replaceChildren(p);

    showTab('overview');
    drawer.classList.add('v2-bases-drawer--open');
    drawer.setAttribute('aria-hidden', 'false');
    document.getElementById('ls-bases-drawer-close').focus();
    if (state.view === 'map') drawMap();
  }

  function closeDrawer() {
    drawer.classList.remove('v2-bases-drawer--open');
    drawer.setAttribute('aria-hidden', 'true');
  }

  // --- view switching -----------------------------------------------------

  function setView(view) {
    state.view = view;
    document.getElementById('ls-bases-map-panel').hidden = view !== 'map';
    document.getElementById('ls-bases-list-panel').hidden = view !== 'list';
    Array.prototype.forEach.call(root.querySelectorAll('[data-bases-view]'), function (b) {
      b.setAttribute('aria-pressed', String(b.dataset.basesView === view));
    });
    try {
      var url = new URL(window.location.href);
      url.searchParams.set('view', view);
      window.history.replaceState(null, '', url.toString());
    } catch (_) {}
    if (view === 'map') applyTransform(); else drawList();
  }

  function redraw() {
    renderChips();
    if (state.view === 'map') drawMap(); else drawList();
  }

  // --- events -------------------------------------------------------------

  root.addEventListener('click', function (ev) {
    var t = ev.target;
    var v = t.closest && t.closest('[data-bases-view]');
    if (v) { setView(v.dataset.basesView); return; }
    var s = t.closest && t.closest('[data-bases-status]');
    if (s) { state.status = s.dataset.basesStatus; redraw(); return; }
    var o = t.closest && t.closest('[data-bases-open]');
    if (o) { var r = byKey(o.dataset.basesOpen); if (r) openDrawer(r); return; }
    var tab = t.closest && t.closest('[data-bases-tab]');
    if (tab) { showTab(tab.dataset.basesTab); return; }
    if (t.id === 'ls-bases-drawer-close' || t === drawer) closeDrawer();
  });
  document.addEventListener('keydown', function (ev) { if (ev.key === 'Escape') closeDrawer(); });
  filterEl.addEventListener('input', function () { state.needle = filterEl.value.trim().toLowerCase(); redraw(); });

  svg.addEventListener('mousemove', function (ev) {
    var r = ev.target && ev.target.dataset && ev.target.dataset.key ? byKey(ev.target.dataset.key) : null;
    if (r) showTip(r, ev); else tip.hidden = true;
  });
  svg.addEventListener('mouseleave', function () { tip.hidden = true; });
  svg.addEventListener('keydown', function (ev) {
    if (ev.key !== 'Enter' && ev.key !== ' ') return;
    var r = ev.target && ev.target.dataset && ev.target.dataset.key ? byKey(ev.target.dataset.key) : null;
    if (!r) return;
    ev.preventDefault();
    openDrawer(r);
  });

  var down = null, dragged = false;
  mapEl.addEventListener('click', function (ev) {
    if (dragged) { dragged = false; return; }
    var r = ev.target && ev.target.dataset && ev.target.dataset.key ? byKey(ev.target.dataset.key) : null;
    if (r) openDrawer(r);
  });
  mapEl.addEventListener('pointerdown', function (ev) {
    down = { x: ev.clientX, y: ev.clientY, panX: state.panX, panY: state.panY };
    dragged = false;
  });
  mapEl.addEventListener('pointermove', function (ev) {
    if (!down) return;
    var dx = ev.clientX - down.x, dy = ev.clientY - down.y;
    if (!dragged && Math.abs(dx) <= 3 && Math.abs(dy) <= 3) return;
    if (!dragged) { mapEl.setPointerCapture(ev.pointerId); mapEl.classList.add('v2-claims-map--dragging'); tip.hidden = true; }
    dragged = true;
    state.panX = down.panX + dx;
    state.panY = down.panY + dy;
    stage.style.transform = 'translate(' + state.panX + 'px,' + state.panY + 'px) scale(' + state.zoom + ')';
  });
  function endDrag(ev) {
    if (!down) return;
    down = null;
    mapEl.classList.remove('v2-claims-map--dragging');
    if (ev && ev.pointerId != null && mapEl.hasPointerCapture(ev.pointerId)) mapEl.releasePointerCapture(ev.pointerId);
  }
  mapEl.addEventListener('pointerup', endDrag);
  mapEl.addEventListener('pointercancel', endDrag);

  function clampPan() {
    var r = mapEl.getBoundingClientRect();
    state.panX = Math.min(0, Math.max(r.width - r.width * state.zoom, state.panX));
    state.panY = Math.min(0, Math.max(r.height - r.height * state.zoom, state.panY));
  }
  function zoomBy(factor, cx, cy) {
    var next = Math.max(1, Math.min(state.zoom * factor, 12));
    if (next === state.zoom) return;
    var r = mapEl.getBoundingClientRect();
    var ox = cx == null ? r.width / 2 : cx - r.left;
    var oy = cy == null ? r.height / 2 : cy - r.top;
    state.panX = ox - (ox - state.panX) * (next / state.zoom);
    state.panY = oy - (oy - state.panY) * (next / state.zoom);
    state.zoom = next;
    clampPan();
    applyTransform();
  }
  mapEl.addEventListener('wheel', function (ev) {
    ev.preventDefault();
    zoomBy(ev.deltaY < 0 ? 1.2 : 1 / 1.2, ev.clientX, ev.clientY);
  }, { passive: false });
  document.getElementById('ls-bases-zoom-in').addEventListener('click', function () { zoomBy(1.4); });
  document.getElementById('ls-bases-zoom-out').addEventListener('click', function () { zoomBy(1 / 1.4); });
  document.getElementById('ls-bases-zoom-reset').addEventListener('click', function () {
    state.zoom = 1; state.panX = 0; state.panY = 0; applyTransform();
  });

  // --- load ---------------------------------------------------------------

  renderMapTools();
  backdrop();
  fetch(API, { credentials: 'same-origin', cache: 'no-store' })
    .then(function (r) { if (!r.ok) throw new Error('status ' + r.status); return r.json(); })
    .then(function (d) {
      if (!d || d.available !== true || !Array.isArray(d.rows)) throw new Error('unavailable');
      state.data = d;
      state.rows = d.rows;
      renderChips();
      setView(state.view);
    })
    .catch(function (e) {
      countEl.textContent = 'Directory unavailable (' + e.message + '). Try again in a moment.';
    });
})();
