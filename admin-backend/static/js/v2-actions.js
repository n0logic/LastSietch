/* ============================================================================
 * V2 Player Live Actions: rescue-teleport / give-item / award-xp / refill-water.
 *
 * Drives the NON-destructive native server-command path at an ONLINE player.
 * Each card posts to its own endpoint; Preview = dry-run, the labelled apply
 * button = a live send gated on a typed confirm token (RESCUE/GIVE/XP/WATER).
 *
 * Uses apiCall/showMsg/hideMsg from base.html (CSRF header attached there).
 * The fragment is HTMX-swapped into #player-subtab-body, so wiring is delegated
 * off `document` and reads fields per-card by [data-field], scoped to the card.
 *
 * Endpoints (admin + CSRF gated server-side):
 *   POST /admin/api/dune/v2/player/{aid}/_rescue_teleport {x,y,z,exact,reason,mode,confirm}
 *   POST /admin/api/dune/v2/player/{aid}/_give_item       {item,qty,durability,reason,mode,confirm}
 *   POST /admin/api/dune/v2/player/{aid}/_award_xp        {category,experience,reason,mode,confirm}
 *   POST /admin/api/dune/v2/player/{aid}/_refill_water    {water_amount,reason,mode,confirm}
 * ============================================================================ */
(function () {
  // data-action -> { endpoint suffix, payload builder reading per-card fields }.
  var ACTIONS = {
    rescue_teleport: {
      path: '_rescue_teleport',
      build: function (f) {
        return {
          x: numField(f, 'x'),
          y: numField(f, 'y'),
          z: numField(f, 'z'),
          exact: !!(f.exact && f.exact.checked),
          reason: strField(f, 'reason'),
        };
      },
    },
    give_item: {
      path: '_give_item',
      build: function (f) {
        return {
          item: strField(f, 'item'),
          qty: intField(f, 'qty', 1),
          durability: numField(f, 'durability', 1),
          reason: strField(f, 'reason'),
        };
      },
    },
    award_xp: {
      path: '_award_xp',
      build: function (f) {
        return {
          category: strField(f, 'category'),
          experience: intField(f, 'experience', null),
          reason: strField(f, 'reason'),
        };
      },
    },
    refill_water: {
      path: '_refill_water',
      build: function (f) {
        return {
          water_amount: intField(f, 'water_amount', 100),
          reason: strField(f, 'reason'),
        };
      },
    },
  };

  function fields(card) {
    // Map data-field name -> input element, scoped to this card only.
    var map = {};
    var els = card.querySelectorAll('[data-field]');
    for (var i = 0; i < els.length; i++) {
      map[els[i].getAttribute('data-field')] = els[i];
    }
    return map;
  }

  function strField(f, name) {
    var el = f[name];
    return el ? (el.value || '').trim() : '';
  }

  function numField(f, name, dflt) {
    var el = f[name];
    if (!el || (el.value || '').trim() === '') return dflt === undefined ? null : dflt;
    var n = parseFloat(el.value);
    return isFinite(n) ? n : (dflt === undefined ? null : dflt);
  }

  function intField(f, name, dflt) {
    var el = f[name];
    if (!el || (el.value || '').trim() === '') return dflt;
    var n = parseInt(el.value, 10);
    return isFinite(n) ? n : dflt;
  }

  function accountId() {
    var root = document.getElementById('v2-live-actions');
    return root ? root.getAttribute('data-account-id') : null;
  }

  function submit(card, mode) {
    var action = card.getAttribute('data-action');
    var spec = ACTIONS[action];
    var aid = accountId();
    if (!spec || !aid) return;

    var msgEl = card.querySelector('[data-role="msg"]');
    var msgId = ensureMsgId(msgEl);
    hideMsg(msgId);

    var f = fields(card);
    var payload = spec.build(f);
    payload.mode = mode;

    if (!payload.reason) {
      showMsg(msgId, 'Reason is required.', 'error');
      return;
    }
    if (mode === 'apply') {
      var token = card.getAttribute('data-confirm');
      var typed = strField(f, 'confirm');
      if (typed !== token) {
        showMsg(msgId, 'Type ' + token + ' to apply.', 'error');
        return;
      }
      payload.confirm = token;
    }

    var buttons = card.querySelectorAll('[data-role="dry-run"],[data-role="apply"]');
    setDisabled(buttons, true);
    showMsg(msgId, mode === 'apply' ? 'Applying…' : 'Previewing…', 'info');

    var path = '/admin/api/dune/v2/player/' + encodeURIComponent(aid) + '/' + spec.path;
    apiCall('POST', path, payload).then(function (d) {
      var ok = d && d.success;
      var detail = (d && d.detail) || '';
      if (ok) {
        var label = mode === 'apply' ? 'Applied.' : 'Preview:';
        showMsg(msgId, (label + ' ' + detail).trim(), 'success');
      } else {
        showMsg(msgId, 'Failed: ' + (detail || 'unknown error'), 'error');
      }
      setDisabled(buttons, false);
    }).catch(function (e) {
      showMsg(msgId, 'Error: ' + (e && e.message ? e.message : String(e)), 'error');
      setDisabled(buttons, false);
    });
  }

  function setDisabled(nodes, val) {
    for (var i = 0; i < nodes.length; i++) nodes[i].disabled = val;
  }

  var _msgSeq = 0;
  function ensureMsgId(el) {
    if (!el) return null;
    if (!el.id) el.id = 'v2-la-msg-' + (++_msgSeq);
    return el.id;
  }

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-role="dry-run"],[data-role="apply"]');
    if (!btn) return;
    var card = btn.closest('.v2-live-action');
    if (!card) return;
    e.preventDefault();
    submit(card, btn.getAttribute('data-role') === 'apply' ? 'apply' : 'dry-run');
  });

  /* ==========================================================================
   * Give-item template typeahead.
   *
   * The give-item field submits the native template_id (e.g. `Stone`); admins
   * know the friendly name ("Granite Stone"), not the backend id. The catalog
   * (template_id <-> name + category, ~1662 items) is fetched once and filtered
   * client-side. Picking a row writes the template_id into the input and shows
   * the resolved name underneath. Free text is still allowed (the backend +
   * relay wrapper remain authoritative).
   * ======================================================================== */
  var CATALOG_URL = '/admin/api/dune/v2/catalog/give-items';
  var MAX_RESULTS = 12;
  var catalog = null;          // [{id,name,cat,_h}] once loaded
  var catalogState = 'idle';   // idle | loading | ready | error

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function loadCatalog(then) {
    if (catalogState === 'ready') { then(); return; }
    if (catalogState === 'loading') { setTimeout(function () { loadCatalog(then); }, 120); return; }
    catalogState = 'loading';
    apiCall('GET', CATALOG_URL).then(function (d) {
      catalog = ((d && d.items) || []).map(function (e) {
        return {
          id: e.id, name: e.name, cat: e.cat,
          stack: e.pak_max_stack || 0, mtx: !!e.mtx, nt: !!e.non_tradeable,
          _h: (e.name + ' ' + e.id).toLowerCase()
        };
      });
      catalogState = 'ready';
      then();
    }).catch(function () {
      catalogState = 'error';
      then();
    });
  }

  function search(q) {
    var tokens = q.toLowerCase().split(/\s+/).filter(Boolean);
    if (!tokens.length) return [];
    var out = [];
    for (var i = 0; i < catalog.length && out.length < 400; i++) {
      var e = catalog[i], ok = true;
      for (var t = 0; t < tokens.length; t++) {
        if (e._h.indexOf(tokens[t]) === -1) { ok = false; break; }
      }
      if (ok) out.push(e);
    }
    // Rank: exact id, name-prefix, then alpha. Keep it cheap.
    var ql = q.toLowerCase();
    out.sort(function (a, b) {
      var ar = a.id.toLowerCase() === ql ? 0 : (a.name.toLowerCase().indexOf(ql) === 0 ? 1 : 2);
      var br = b.id.toLowerCase() === ql ? 0 : (b.name.toLowerCase().indexOf(ql) === 0 ? 1 : 2);
      return ar - br || a.name.localeCompare(b.name);
    });
    return out.slice(0, MAX_RESULTS);
  }

  function giCard(el) {
    var card = el.closest('.v2-live-action');
    return card && card.getAttribute('data-action') === 'give_item' ? card : null;
  }

  function parts(card) {
    return {
      input: card.querySelector('[data-role="item-input"]'),
      results: card.querySelector('[data-role="item-results"]'),
      resolved: card.querySelector('[data-role="item-resolved"]'),
      qtyHint: card.querySelector('[data-role="qty-hint"]'),
    };
  }

  function byId(id) {
    if (!catalog || !id) return null;
    var ql = id.toLowerCase();
    for (var i = 0; i < catalog.length; i++) { if (catalog[i].id.toLowerCase() === ql) return catalog[i]; }
    return null;
  }

  function badgesHtml(e) {
    var b = '';
    if (e.mtx) b += '<span class="la-ta-badge la-ta-badge--mtx" title="MTX template — event/paid item">MTX</span>';
    if (e.nt) b += '<span class="la-ta-badge la-ta-badge--nt" title="Cannot be listed on the exchange">NO-TRADE</span>';
    return b;
  }

  function showResolved(p, entry) {
    if (!p.resolved) return;
    if (entry) {
      var warn = '';
      if (entry.mtx) warn += ' <span class="la-ta-badge la-ta-badge--mtx">MTX — event/paid item</span>';
      if (entry.nt) warn += ' <span class="la-ta-badge la-ta-badge--nt">cannot be listed on exchange</span>';
      p.resolved.innerHTML = '<strong>' + esc(entry.name) + '</strong>'
        + (entry.cat ? ' <span class="la-ta-cat">' + esc(entry.cat) + '</span>' : '') + warn;
      p.resolved.hidden = false;
    } else {
      p.resolved.hidden = true;
      p.resolved.textContent = '';
    }
    if (p.qtyHint) {
      if (entry && entry.stack) {
        p.qtyHint.textContent = 'Pak max stack: ' + entry.stack.toLocaleString() + ' (live config may differ)';
        p.qtyHint.hidden = false;
      } else {
        p.qtyHint.hidden = true;
        p.qtyHint.textContent = '';
      }
    }
  }

  function closeResults(p, input) {
    p.results.hidden = true;
    p.results.innerHTML = '';
    if (input) input.setAttribute('aria-expanded', 'false');
  }

  function render(p) {
    var q = (p.input.value || '').trim();
    if (!q) { closeResults(p, p.input); showResolved(p, null); return; }
    if (catalogState === 'error') {
      p.results.innerHTML = '<div class="la-ta-empty">Catalog unavailable — type the template id directly.</div>';
      p.results.hidden = false;
      return;
    }
    var hits = search(q);
    // Exact-id typed by hand -> show its friendly name even with no dropdown.
    var exact = null;
    for (var i = 0; i < hits.length; i++) { if (hits[i].id.toLowerCase() === q.toLowerCase()) { exact = hits[i]; break; } }
    showResolved(p, exact);
    if (!hits.length) {
      p.results.innerHTML = '<div class="la-ta-empty">No items match.</div>';
      p.results.hidden = false;
      p.input.setAttribute('aria-expanded', 'true');
      return;
    }
    p.results.innerHTML = hits.map(function (e) {
      return '<div class="la-ta-item" role="option" data-id="' + esc(e.id) + '" data-name="' + esc(e.name) + '">'
        + '<span class="la-ta-name">' + esc(e.name) + '</span>'
        + badgesHtml(e)
        + (e.cat ? '<span class="la-ta-cat">' + esc(e.cat) + '</span>' : '')
        + '<span class="la-ta-id">' + esc(e.id) + '</span></div>';
    }).join('');
    p.results.hidden = false;
    p.input.setAttribute('aria-expanded', 'true');
  }

  function choose(p, id, name) {
    p.input.value = id;
    showResolved(p, byId(id) || { id: id, name: name, cat: '', stack: 0, mtx: false, nt: false });
    closeResults(p, p.input);
    p.input.focus();
  }

  function activeIndex(p) {
    var items = p.results.querySelectorAll('.la-ta-item');
    for (var i = 0; i < items.length; i++) { if (items[i].classList.contains('is-active')) return i; }
    return -1;
  }

  function moveActive(p, delta) {
    var items = p.results.querySelectorAll('.la-ta-item');
    if (!items.length) return;
    var cur = activeIndex(p);
    if (cur >= 0) items[cur].classList.remove('is-active');
    var next = cur < 0 ? (delta > 0 ? 0 : items.length - 1) : (cur + delta + items.length) % items.length;
    items[next].classList.add('is-active');
    items[next].scrollIntoView({ block: 'nearest' });
  }

  document.addEventListener('input', function (e) {
    var card = giCard(e.target);
    if (!card || e.target.getAttribute('data-role') !== 'item-input') return;
    var p = parts(card);
    loadCatalog(function () { render(p); });
  });

  document.addEventListener('focusin', function (e) {
    var card = giCard(e.target);
    if (!card || e.target.getAttribute('data-role') !== 'item-input') return;
    if ((e.target.value || '').trim()) loadCatalog(function () { render(parts(card)); });
  });

  document.addEventListener('keydown', function (e) {
    var card = giCard(e.target);
    if (!card || e.target.getAttribute('data-role') !== 'item-input') return;
    var p = parts(card);
    if (p.results.hidden) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); moveActive(p, 1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); moveActive(p, -1); }
    else if (e.key === 'Enter') {
      var act = p.results.querySelector('.la-ta-item.is-active');
      if (act) { e.preventDefault(); choose(p, act.getAttribute('data-id'), act.getAttribute('data-name')); }
    } else if (e.key === 'Escape') {
      closeResults(p, p.input);
    }
  });

  document.addEventListener('click', function (e) {
    var item = e.target.closest('.la-ta-item');
    if (item) {
      var card = item.closest('.v2-live-action');
      if (card) { var p = parts(card); choose(p, item.getAttribute('data-id'), item.getAttribute('data-name')); }
      return;
    }
    // Outside the give-item field -> dismiss any open results.
    if (!giCard(e.target)) {
      var open = document.querySelectorAll('.v2-live-action[data-action="give_item"] [data-role="item-results"]:not([hidden])');
      for (var i = 0; i < open.length; i++) {
        open[i].hidden = true; open[i].innerHTML = '';
        var inp = open[i].closest('.v2-live-action').querySelector('[data-role="item-input"]');
        if (inp) inp.setAttribute('aria-expanded', 'false');
      }
    }
  });
})();
