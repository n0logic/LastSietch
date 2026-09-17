/* VC4 - Player Workbench JS (multi-grant cart).
 *
 * - Recipient batch picker: type-ahead from /api/dune/grant/players (JSON),
 *   click to add, chip with x to remove. MAX_BATCH cap. Each entry carries
 *   online_status + in_grace_period for the offline cross-check.
 * - Grant search + browse accordion built from /v2/players/grants/_index
 *   (presets + derived catalog). No HTML scraping.
 * - Item rows (items_batch): searched against the give-item catalog fetched
 *   once from /api/dune/v2/catalog/give-items, with grade pills that unlock
 *   only on a gradeable template. Never catalog.entries.item.
 * - Config panel: render a grant/preset's fields, validate client-side, then
 *   add to (or update) the cart.
 * - Cart: ordered list of lines (preset or custom); edit/remove/clear.
 * - Live query preview: recipients x lines = fires, offline-queued count.
 * - Fire: one POST /v2/players/grants/cart/fire under a single batch_id.
 *
 * Defers to base.html's csrfToken global for the CSRF header.
 */

(function () {
  'use strict';

  function $(id) { return document.getElementById(id); }

  var MAX_BATCH = (typeof window.WORKBENCH_MAX_BATCH === 'number') ? window.WORKBENCH_MAX_BATCH : 25;
  var MAX_CART = (typeof window.WORKBENCH_MAX_CART === 'number') ? window.WORKBENCH_MAX_CART : 25;
  var MAX_TOTAL_FIRES = 500;     // server hard ceiling (mirrors v2_players.py)
  var SOFT_CONFIRM_FIRES = 200;  // client friction prompt above this

  // Recipient batch: array of {aid, name, online_status, in_grace_period}.
  var batch = [];
  var allPlayers = [];
  var playersLoaded = false;
  var playersLoading = false;

  // Catalog/preset index (loaded once) + flat search index.
  var indexData = { presets: [], catalog: [] };
  var onlineSafeOnly = false;  // when true, hide RAM-fragile (offline-only) grants
  var searchIndex = [];     // [{source, key, label, category, ramFragile, entry}]
  var indexLoaded = false;

  // Cart: array of line objects (see makeLine). editingId set while editing.
  var cart = [];
  var cartSeq = 0;
  var editingId = null;

  /* ------------------------------ Utilities ------------------------------ */

  function escapeHTML(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function escapeAttr(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function summariseDetail(d) {
    if (!d) return '';
    var parts = [];
    Object.keys(d).forEach(function (k) {
      var v = d[k];
      if (v === null || v === undefined) return;
      if (typeof v === 'object') return;
      parts.push(k + '=' + v);
    });
    return parts.join(' ');
  }

  /* ------------------------------ Batch UI ------------------------------- */

  function renderBatch() {
    var ul = $('batch-chips');
    var counter = $('batch-count');
    var clear = $('batch-clear');
    if (ul) {
      ul.innerHTML = '';
      batch.forEach(function (r, idx) {
        var li = document.createElement('li');
        li.className = 'v2-batch__chip';
        li.innerHTML = '<span class="v2-batch__chip-name">' + escapeHTML(r.name) + '</span>' +
                       '<code class="v2-batch__chip-aid">#' + r.aid + '</code>' +
                       '<button type="button" class="v2-batch__chip-x" aria-label="remove">x</button>';
        li.querySelector('button').addEventListener('click', function () {
          batch.splice(idx, 1);
          renderBatch();
          onStateChange();
        });
        ul.appendChild(li);
      });
    }
    if (counter) counter.textContent = batch.length + ' / ' + MAX_BATCH;
    if (clear) clear.hidden = batch.length === 0;
  }

  function lookupPlayer(aid) {
    for (var i = 0; i < allPlayers.length; i++) {
      if (allPlayers[i].aid === aid) return allPlayers[i];
    }
    return null;
  }

  function addToBatch(aid, name) {
    aid = parseInt(aid, 10);
    if (!aid || aid < 1) return false;
    if (batch.length >= MAX_BATCH) {
      flashSearchStatus('batch is full (max ' + MAX_BATCH + ')');
      return false;
    }
    if (batch.some(function (r) { return r.aid === aid; })) {
      flashSearchStatus('already in batch');
      return false;
    }
    var p = lookupPlayer(aid);
    batch.push({
      aid: aid,
      name: name || (p && p.name) || ('aid ' + aid),
      online_status: p ? p.online_status : null,
      in_grace_period: p ? !!p.in_grace_period : false,
    });
    renderBatch();
    onStateChange();
    return true;
  }

  function loadPlayers(cb) {
    if (playersLoaded || playersLoading) { if (cb && playersLoaded) cb(); return; }
    playersLoading = true;
    fetch('/admin/api/dune/grant/players', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        allPlayers = ((data && data.players) || []).map(function (p) {
          return {
            aid: parseInt(p.account_id, 10),
            name: p.name || ('aid ' + p.account_id),
            online_status: p.online_status,
            in_grace_period: !!p.in_grace_period,
          };
        });
        playersLoaded = true;
        playersLoading = false;
        // Refresh online flags on already-batched recipients.
        batch.forEach(function (r) {
          var p = lookupPlayer(r.aid);
          if (p) { r.online_status = p.online_status; r.in_grace_period = p.in_grace_period; }
        });
        onStateChange();
        if (cb) cb();
      })
      .catch(function () {
        playersLoading = false;
        flashSearchStatus('couldn\'t load player list - check relay');
      });
  }

  function matchPlayers(query) {
    var q = (query || '').trim().toLowerCase();
    if (!q) return allPlayers.slice(0, 12);
    return allPlayers.filter(function (p) {
      return p.name.toLowerCase().indexOf(q) !== -1 || String(p.aid).indexOf(q) === 0;
    }).slice(0, 12);
  }

  function renderSuggest(matches) {
    var ul = $('batch-suggest');
    if (!ul) return;
    ul.innerHTML = '';
    if (!matches.length) { ul.hidden = true; return; }
    matches.forEach(function (p) {
      var li = document.createElement('li');
      li.className = 'v2-batch__suggest-item';
      li.tabIndex = 0;
      var dot = p.online_status === 'online' ? ' (online)' : (p.in_grace_period ? ' (grace)' : '');
      li.innerHTML = '<span>' + escapeHTML(p.name) + '</span> <code>#' + p.aid + '</code>' +
                     '<span class="v2-batch__suggest-flag">' + escapeHTML(dot) + '</span>';
      var pick = function () {
        if (addToBatch(p.aid, p.name)) {
          var input = $('batch-search');
          if (input) { input.value = ''; input.focus(); }
          ul.hidden = true;
        }
      };
      li.addEventListener('click', pick);
      li.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { e.preventDefault(); pick(); }
      });
      ul.appendChild(li);
    });
    ul.hidden = false;
  }

  function flashSearchStatus(msg) {
    var input = $('batch-search');
    if (!input) return;
    var prev = input.placeholder;
    input.placeholder = msg;
    setTimeout(function () { input.placeholder = prev; }, 1800);
  }

  function bindBatchSearch() {
    var input = $('batch-search');
    if (!input || input.dataset.bound === '1') return;
    input.dataset.bound = '1';
    input.addEventListener('focus', function () {
      loadPlayers(function () { renderSuggest(matchPlayers(input.value)); });
    });
    input.addEventListener('input', function () {
      loadPlayers(function () { renderSuggest(matchPlayers(input.value)); });
    });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        var matches = matchPlayers(input.value);
        if (matches.length) {
          if (addToBatch(matches[0].aid, matches[0].name)) {
            input.value = '';
            var s = $('batch-suggest'); if (s) s.hidden = true;
          }
        } else if (/^\d+$/.test(input.value.trim())) {
          if (addToBatch(input.value.trim(), null)) input.value = '';
        }
      } else if (e.key === 'Escape') {
        var s2 = $('batch-suggest'); if (s2) s2.hidden = true;
      }
    });
    document.addEventListener('click', function (e) {
      var box = document.querySelector('.v2-batch__search');
      if (box && !box.contains(e.target)) {
        var ul = $('batch-suggest'); if (ul) ul.hidden = true;
      }
    });
  }

  function bindBatchControls() {
    var addCurrent = $('batch-add-current');
    if (addCurrent && addCurrent.dataset.bound !== '1') {
      addCurrent.dataset.bound = '1';
      addCurrent.addEventListener('click', function () {
        addToBatch(addCurrent.getAttribute('data-aid'), addCurrent.getAttribute('data-name'));
      });
    }
    var clear = $('batch-clear');
    if (clear && clear.dataset.bound !== '1') {
      clear.dataset.bound = '1';
      clear.addEventListener('click', function () {
        batch = [];
        renderBatch();
        onStateChange();
      });
    }
  }

  /* --------------------------- Index + search ---------------------------- */

  function loadIndex(cb) {
    if (indexLoaded) { if (cb) cb(); return; }
    fetch('/admin/v2/players/grants/_index', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        indexData = { presets: (data && data.presets) || [], catalog: (data && data.catalog) || [] };
        buildSearchIndex();
        indexLoaded = true;
        renderBrowse();
        if (cb) cb();
      })
      .catch(function () {
        var b = $('wb-browse');
        if (b) b.innerHTML = '<div class="msg err">couldn\'t load catalog - check relay</div>';
      });
  }

  function buildSearchIndex() {
    searchIndex = [];
    indexData.presets.forEach(function (p) {
      searchIndex.push({
        source: 'preset', key: p.name, label: p.display || p.name,
        category: 'Presets', ramFragile: !!p.ram_fragile, entry: p,
      });
    });
    indexData.catalog.forEach(function (c) {
      searchIndex.push({
        source: 'catalog', key: c.grant_type, label: c.label,
        category: c.category || 'Other', ramFragile: !!c.ram_fragile, entry: c,
      });
    });
  }

  function findIndexItem(source, key) {
    for (var i = 0; i < searchIndex.length; i++) {
      if (searchIndex[i].source === source && String(searchIndex[i].key) === String(key)) {
        return searchIndex[i];
      }
    }
    return null;
  }

  function matchGrants(query) {
    var q = (query || '').trim().toLowerCase();
    // Hide the modal-owned grant types (entry.picker): they are not standalone
    // tiles, only the two opens-marked launcher tiles surface them. They stay in
    // searchIndex so findIndexItem still resolves them for cart-line edits.
    var pool = searchIndex.filter(function (it) {
      if (it.entry && it.entry.picker) return false;
      if (onlineSafeOnly && it.ramFragile) return false;
      return true;
    });
    if (!q) return pool.slice(0, 14);
    return pool.filter(function (it) {
      return it.label.toLowerCase().indexOf(q) !== -1 ||
             String(it.key).toLowerCase().indexOf(q) !== -1 ||
             it.category.toLowerCase().indexOf(q) !== -1;
    }).slice(0, 14);
  }

  function renderGrantSuggest(matches) {
    var ul = $('wb-grant-suggest');
    if (!ul) return;
    ul.innerHTML = '';
    if (!matches.length) { ul.hidden = true; return; }
    matches.forEach(function (it) {
      var li = document.createElement('li');
      li.className = 'v2-gsearch__item';
      li.setAttribute('data-source', it.source);
      li.setAttribute('data-key', it.key);
      li.tabIndex = 0;
      var flag = it.ramFragile ? '<span class="v2-gsearch__flag">offline</span>' : '';
      li.innerHTML = '<span class="v2-gsearch__label">' + escapeHTML(it.label) + '</span>' +
                     '<span class="v2-gsearch__cat">' + escapeHTML(it.category) + '</span>' + flag;
      var pick = function () {
        activate(it.source, it.key);
        var input = $('wb-grant-search');
        if (input) input.value = '';
        ul.hidden = true;
      };
      li.addEventListener('click', pick);
      li.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { e.preventDefault(); pick(); }
      });
      ul.appendChild(li);
    });
    ul.hidden = false;
  }

  function bindGrantSearch() {
    var input = $('wb-grant-search');
    if (!input || input.dataset.bound === '1') return;
    input.dataset.bound = '1';
    input.addEventListener('focus', function () { loadIndex(function () { renderGrantSuggest(matchGrants(input.value)); }); });
    input.addEventListener('input', function () { loadIndex(function () { renderGrantSuggest(matchGrants(input.value)); }); });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        var m = matchGrants(input.value);
        if (m.length) { activate(m[0].source, m[0].key); input.value = ''; var s = $('wb-grant-suggest'); if (s) s.hidden = true; }
      } else if (e.key === 'Escape') {
        var s2 = $('wb-grant-suggest'); if (s2) s2.hidden = true;
      }
    });
    document.addEventListener('click', function (e) {
      var box = $('wb-grant-search');
      var sug = $('wb-grant-suggest');
      if (sug && box && e.target !== box && !sug.contains(e.target)) sug.hidden = true;
    });
    var safeToggle = $('wb-grant-online-safe');
    if (safeToggle && safeToggle.dataset.bound !== '1') {
      safeToggle.dataset.bound = '1';
      // Sync initial state in case the browser restored the checkbox as checked.
      onlineSafeOnly = safeToggle.checked;
      safeToggle.addEventListener('change', function () {
        onlineSafeOnly = safeToggle.checked;
        // Re-render the browse/presets grid so offline-only grants are hidden/shown.
        loadIndex(function () { renderBrowse(); });
        // Only refresh the suggestion list if it's already open, so toggling
        // doesn't pop the dropdown unprompted.
        var ul = $('wb-grant-suggest');
        if (ul && !ul.hidden) {
          renderGrantSuggest(matchGrants(input.value));
        }
      });
    }
  }

  function renderBrowse() {
    var mount = $('wb-browse');
    if (!mount) return;
    mount.innerHTML = '';
    // Group by category, Presets first. Presets render as a default-open roll-up
    // (the old server-rendered card shelf was removed); other categories start
    // collapsed, and the accordion is single-open (see the toggle handler below).
    var groups = [];
    var byCat = {};
    function push(cat, item) {
      // Online-safe filter: hide RAM-fragile (offline-only) grants AND presets
      // when the toggle is on, matching matchGrants() for the search dropdown.
      if (onlineSafeOnly && item.ramFragile) return;
      if (!byCat[cat]) { byCat[cat] = { category: cat, items: [] }; groups.push(byCat[cat]); }
      byCat[cat].items.push(item);
    }
    if (indexData.presets.length) {
      indexData.presets.forEach(function (p) {
        push('Presets', { source: 'preset', key: p.name, label: p.display || p.name, ramFragile: !!p.ram_fragile });
      });
    }
    indexData.catalog.forEach(function (c) {
      // Modal-owned types (entry.picker) are hidden from the browse grid; the two
      // opens-marked launcher tiles are their only entry points.
      if (c.picker) return;
      push(c.category || 'Other', { source: 'catalog', key: c.grant_type, label: c.label, ramFragile: !!c.ram_fragile });
    });

    groups.forEach(function (g) {
      var det = document.createElement('details');
      det.className = 'v2-catalog__group';
      if (g.category === 'Presets') det.open = true;   // Presets expanded by default
      // Single-open accordion: opening one group collapses the others.
      det.addEventListener('toggle', function () {
        if (!det.open) return;
        var all = mount.querySelectorAll('details.v2-catalog__group');
        for (var i = 0; i < all.length; i++) { if (all[i] !== det) all[i].open = false; }
      });
      var sum = document.createElement('summary');
      sum.className = 'v2-catalog__summary';
      sum.textContent = g.category + ' (' + g.items.length + ')';
      det.appendChild(sum);
      var wrap = document.createElement('div');
      wrap.className = 'v2-catalog__items';
      g.items.forEach(function (item) {
        var tile = document.createElement('button');
        tile.type = 'button';
        tile.className = 'v2-catalog__tile';
        tile.setAttribute('data-source', item.source);
        tile.setAttribute('data-key', item.key);
        var flag = item.ramFragile ? ' <span class="v2-gsearch__flag">offline</span>' : '';
        tile.innerHTML = '<span class="v2-catalog__tile-label">' + escapeHTML(item.label) + '</span>' + flag;
        tile.addEventListener('click', function () { activate(item.source, item.key); });
        wrap.appendChild(tile);
      });
      det.appendChild(wrap);
      mount.appendChild(det);
    });
  }

  /* -------------------- Give-item catalog (item rows) -------------------- */
  /* The item rows read data/dune-give-item-catalog.json through the admin-gated
   * v2 route, never the grant catalog's entries.item: that list is a 2026-05-21
   * community cross-reference of an older build, while the give-item catalog
   * carries the NATIVE template ids plus is_gradeable, pak_max_stack, tier and
   * category per template. Fetched once per page, shared by every row. */
  var GIVE_ITEMS_URL = '/admin/api/dune/v2/catalog/give-items';
  var STACK_SERVER_CAP = 9999;     // fallback cap when a template has no pak max
  var MAX_ITEM_RESULTS = 200;      // the results list never renders more

  /* quality_level is the item GRADE, not a rarity: grades run Base(0) through 5
   * and exist only on gradeable (T6) templates. Base(0) is a real grade, never
   * "unset". 6 is not a grade and routers/dune_grant.py refuses it. */
  var GRADE_LABELS = ['Base', 'G1', 'G2', 'G3', 'G4', 'G5'];

  var giveItems = null;            // mapped rows, null until the fetch settles
  var giveItemById = {};
  var giveItemsError = null;
  var giveItemsLoading = false;
  var giveItemsWaiters = [];

  function setGiveItems(rows, err) {
    giveItemsError = err;
    giveItems = rows.map(function (it) {
      return {
        template_id: it.id,
        label: it.name || it.id,
        cat: it.cat || '',
        tier: (it.tier === undefined || it.tier === null) ? null : it.tier,
        is_gradeable: !!it.is_gradeable,
        pak_max_stack: it.pak_max_stack || 0,
        non_tradeable: !!it.non_tradeable,
        mtx: !!it.mtx,
        _h: ((it.name || '') + ' ' + it.id).toLowerCase(),
      };
    });
    giveItemById = {};
    giveItems.forEach(function (e) { giveItemById[e.template_id] = e; });
    giveItemsLoading = false;
    var waiters = giveItemsWaiters;
    giveItemsWaiters = [];
    waiters.forEach(function (fn) { fn(); });
  }

  function loadGiveItems(cb) {
    if (giveItems !== null) { if (cb) cb(); return; }
    if (cb) giveItemsWaiters.push(cb);
    if (giveItemsLoading) return;
    giveItemsLoading = true;
    fetch(GIVE_ITEMS_URL, { credentials: 'same-origin' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (d) { setGiveItems((d && d.items) || [], null); })
      .catch(function (e) { setGiveItems([], e.message || 'error'); });
  }

  /* Category chips. The catalog ships 57 fine-grained `cat` values; these group
   * them into the families an admin actually shops by. Anything unmapped lands
   * in "More" rather than disappearing. */
  var GIVE_CAT_FAMILIES = [
    ['weapons', 'Weapons', ['ranged', 'shortblades', 'longblades', 'battlerifle',
      'pistol', 'shotgun', 'smg', 'melee', 'heavypistol', 'spitdart',
      'heavyshotgun', 'heavyrifle', 'flamethrower', 'lasgun', 'missilelauncher',
      'fireballer', 'cutteray', 'ammunition']],
    ['armor', 'Armor', ['chest', 'hands', 'feet', 'head', 'legs', 'lightarmor',
      'heavyarmor', 'armor', 'stillsuits', 'shield', 'utilitywearables']],
    ['tools', 'Tools', ['watertools', 'bloodtools', 'cartographytools',
      'utility', 'compactor']],
    ['vehicles', 'Vehicles', ['engine', 'psu', 'powerpack', 'chassis', 'hull',
      'suspensor', 'locomotion', 'cockpit', 'cabin', 'rear', 'tail', 'buggy',
      'sandbike', 'sandcrawler', 'lightornithopter', 'mediumornithopter',
      'transportornithopter']],
    ['components', 'Components', ['components', 'deployables']],
    ['rawresources', 'Raw resources', ['rawresources']],
    ['refinedresources', 'Refined resources', ['refinedresources', 'fuel']],
  ];
  var GIVE_CAT_OF = {};
  GIVE_CAT_FAMILIES.forEach(function (f) {
    f[2].forEach(function (cat) { GIVE_CAT_OF[cat] = f[0]; });
  });

  function itemFamily(e) { return GIVE_CAT_OF[e.cat] || 'more'; }

  function isSchematicId(id) { return /_Schematic$/.test(String(id || '')); }

  function itemInFamily(e, fam) {
    if (!fam) return true;
    if (fam === 'schematics') return isSchematicId(e.template_id);
    return itemFamily(e) === fam;
  }

  function itemFamilyChips() {
    // Only the families the loaded catalog actually has rows for.
    var seen = {};
    (giveItems || []).forEach(function (e) {
      seen[itemFamily(e)] = true;
      if (isSchematicId(e.template_id)) seen.schematics = true;
    });
    var out = [{ key: '', label: 'All' }];
    GIVE_CAT_FAMILIES.forEach(function (f) { if (seen[f[0]]) out.push({ key: f[0], label: f[1] }); });
    if (seen.schematics) out.push({ key: 'schematics', label: 'Schematics' });
    if (seen.more) out.push({ key: 'more', label: 'More' });
    return out;
  }

  function searchItems(query, fam) {
    var tokens = String(query || '').trim().toLowerCase().split(/\s+/).filter(Boolean);
    var pool = giveItems || [];
    var out = [];
    for (var i = 0; i < pool.length && out.length < MAX_ITEM_RESULTS; i++) {
      var e = pool[i];
      if (!itemInFamily(e, fam)) continue;
      var ok = true;
      for (var t = 0; t < tokens.length; t++) {
        if (e._h.indexOf(tokens[t]) === -1) { ok = false; break; }
      }
      if (ok) out.push(e);
    }
    return out;
  }

  /* One result row. Every catalog-derived value reaches an attribute through
   * escapeAttr (escapeHTML leaves the double quote raw, so it is only safe
   * between tags). */
  function itemOptHtml(e, active) {
    var meta = [];
    if (e.label && e.label !== e.template_id) meta.push(e.template_id);
    if (e.tier !== null && e.tier !== undefined && e.tier !== '') meta.push('T' + e.tier);
    if (e.cat) meta.push(e.cat);
    var tags = '';
    if (e.is_gradeable) tags += '<span class="v2-items-batch__tag v2-items-batch__tag--grade">GRADEABLE</span>';
    if (e.mtx) tags += '<span class="v2-items-batch__tag v2-items-batch__tag--warn">MTX</span>';
    if (e.non_tradeable) tags += '<span class="v2-items-batch__tag">NO-TRADE</span>';
    if (e.unknown) tags += '<span class="v2-items-batch__tag v2-items-batch__tag--warn">NOT IN THE CATALOG</span>';
    return '<div class="v2-items-batch__opt' + (active ? ' is-active' : '') + '" role="option"' +
      ' data-raw="' + escapeAttr(e.template_id) + '"' +
      ' data-stack="' + escapeAttr(e.pak_max_stack || '') + '"' +
      ' data-mtx="' + escapeAttr(e.mtx ? '1' : '') + '"' +
      ' data-nt="' + escapeAttr(e.non_tradeable ? '1' : '') + '"' +
      ' data-gradeable="' + escapeAttr(e.is_gradeable ? '1' : '') + '"' +
      ' data-unknown="' + escapeAttr(e.unknown ? '1' : '') + '">' +
      '<span class="v2-items-batch__opt-name">' + escapeHTML(e.label || e.template_id) + '</span>' +
      tags +
      (meta.length ? '<span class="v2-items-batch__opt-meta">' + escapeHTML(meta.join(' / ')) + '</span>' : '') +
      '</div>';
  }

  var itemRowSeq = 0;

  /* One items_batch row: a full-width search line, then grade pills + stack +
   * remove. Serializes through the hidden .v2-items-batch__tpl / __qual inputs
   * and the __stack number, which is exactly what readFields() already reads. */
  function buildItemRow(f, onRemove, preset) {
    var uid = 'wb-ib-' + (++itemRowSeq);
    var stackMin = (f.stack_min != null) ? f.stack_min : 1;
    var stackMax = (f.stack_max != null) ? f.stack_max : STACK_SERVER_CAP;
    var gradeMax = (f.quality_max != null) ? f.quality_max : (GRADE_LABELS.length - 1);
    var stackDefault = (f.stack_default != null) ? f.stack_default : 1;

    var pills = '';
    for (var g = 0; g < GRADE_LABELS.length && g <= gradeMax; g++) {
      pills += '<button type="button" class="v2-items-batch__pill' + (g === 0 ? ' is-active' : '') +
               '" data-grade="' + g + '">' + GRADE_LABELS[g] + '</button>';
    }

    var row = document.createElement('div');
    row.className = 'v2-items-batch__row';
    row.innerHTML =
      '<div class="v2-items-batch__find">' +
        '<label class="v2-items-batch__label" for="' + uid + '-search">Item</label>' +
        '<input type="text" id="' + uid + '-search" class="v2-items-batch__search" ' +
          'autocomplete="off" spellcheck="false" role="combobox" aria-expanded="false" ' +
          'aria-autocomplete="list" aria-controls="' + uid + '-results" ' +
          'placeholder="Search the item catalog, or type a raw template id">' +
        '<div class="v2-items-batch__chips" role="group" aria-label="Filter items by category"></div>' +
        '<div class="v2-items-batch__results" id="' + uid + '-results" role="listbox" hidden></div>' +
        '<div class="v2-items-batch__picked" aria-live="polite"></div>' +
        '<input type="hidden" class="v2-items-batch__tpl" value="">' +
      '</div>' +
      '<div class="v2-items-batch__ctl">' +
        '<div class="v2-items-batch__field">' +
          '<span class="v2-items-batch__label" id="' + uid + '-gradelabel">Grade</span>' +
          '<div class="v2-items-batch__grade" role="group" data-locked="1" ' +
            'aria-labelledby="' + uid + '-gradelabel">' + pills + '</div>' +
          '<input type="hidden" class="v2-items-batch__qual" value="0">' +
        '</div>' +
        '<div class="v2-items-batch__field">' +
          '<label class="v2-items-batch__label" for="' + uid + '-stack">Stack size</label>' +
          '<div class="v2-items-batch__stackline">' +
            '<input type="number" id="' + uid + '-stack" class="v2-items-batch__stack" ' +
              'min="' + stackMin + '" max="' + stackMax + '" value="' + stackDefault + '" ' +
              'data-cap="' + stackMax + '" data-cap-src="server">' +
            '<button type="button" class="v2-items-batch__max" ' +
              'aria-label="set the stack to its maximum">max</button>' +
          '</div>' +
        '</div>' +
        '<button type="button" class="v2-items-batch__rm" aria-label="remove this item">x</button>' +
      '</div>' +
      '<div class="v2-items-batch__hint"></div>';

    var search = row.querySelector('.v2-items-batch__search');
    var chips = row.querySelector('.v2-items-batch__chips');
    var results = row.querySelector('.v2-items-batch__results');
    var picked = row.querySelector('.v2-items-batch__picked');
    var tpl = row.querySelector('.v2-items-batch__tpl');
    var grade = row.querySelector('.v2-items-batch__grade');
    var qual = row.querySelector('.v2-items-batch__qual');
    var stack = row.querySelector('.v2-items-batch__stack');
    var hint = row.querySelector('.v2-items-batch__hint');
    var fam = '';
    var activeIdx = -1;
    var chipsKey = null;

    function stackCap() { return parseInt(stack.getAttribute('data-cap'), 10) || stackMax; }

    function stackNote() {
      return (stack.getAttribute('data-cap-src') === 'pak')
        ? 'Max ' + stackCap().toLocaleString() + ' (pak max stack; live config may differ).'
        : 'Max ' + stackCap().toLocaleString() + ' (server cap).';
    }

    /* Never stack graded items: routers/dune_grant.py refuses it, so the row
     * mirrors the rule rather than letting the refusal be the first thing the
     * admin hears about it. */
    function refreshRowState() {
      var g = parseInt(qual.value, 10) || 0;
      var locked = grade.getAttribute('data-locked') === '1';
      if (g > 0) {
        stack.value = '1';
        stack.disabled = true;
        hint.textContent = 'Graded items never stack: stack forced to 1.';
        return;
      }
      stack.disabled = false;
      if (!tpl.value) {
        hint.textContent = 'Pick an item first: only gradeable items carry a grade.';
        return;
      }
      hint.textContent = (locked
        ? 'No grades on this item: it stays at Base. '
        : 'Base is grade 0, a real grade, not "unset". ') + stackNote();
    }

    function setGrade(g) {
      if (grade.getAttribute('data-locked') === '1' && g !== 0) return;
      qual.value = String(g);
      var btns = grade.querySelectorAll('.v2-items-batch__pill');
      for (var i = 0; i < btns.length; i++) {
        btns[i].classList.toggle('is-active', btns[i].getAttribute('data-grade') === String(g));
      }
      refreshRowState();
    }

    function setGradeMode(unlocked) {
      grade.setAttribute('data-locked', unlocked ? '0' : '1');
      if (!unlocked) setGrade(0);
      else refreshRowState();
    }

    function selectEntry(e) {
      tpl.value = e.template_id;
      search.value = e.template_id;
      var pakMax = parseInt(e.pak_max_stack, 10);
      var cap = (pakMax >= 1) ? Math.min(pakMax, stackMax) : stackMax;
      stack.setAttribute('max', String(cap));
      stack.setAttribute('data-cap', String(cap));
      stack.setAttribute('data-cap-src', (pakMax >= 1) ? 'pak' : 'server');
      if ((parseInt(stack.value, 10) || 1) > cap) stack.value = String(cap);
      var bits = [];
      if (e.label && e.label !== e.template_id) bits.push(e.label);
      bits.push('id ' + e.template_id);
      if (e.unknown) {
        bits.push('not in the catalog: sent as a raw template id, so its grades ' +
                  'and stack cap are not checked here');
      }
      if (e.mtx) bits.push('MTX: event or paid content');
      if (e.non_tradeable) bits.push('no-trade: cannot be listed on the exchange');
      picked.textContent = bits.join(' / ');
      // The picked template's own is_gradeable drives the pills; an uncatalogued
      // id keeps them open because nothing here can check it.
      setGradeMode(!!e.is_gradeable || !!e.unknown);
    }

    function clearSelection() {
      tpl.value = '';
      picked.textContent = '';
      stack.setAttribute('max', String(stackMax));
      stack.setAttribute('data-cap', String(stackMax));
      stack.setAttribute('data-cap-src', 'server');
      setGradeMode(false);
    }

    function entryFromOpt(optEl) {
      var raw = optEl.getAttribute('data-raw');
      var known = giveItemById[raw];
      if (known) return known;
      return {
        template_id: raw, label: raw, cat: '', tier: null, pak_max_stack: 0,
        is_gradeable: optEl.getAttribute('data-gradeable') === '1',
        mtx: false, non_tradeable: false,
        unknown: optEl.getAttribute('data-unknown') === '1',
      };
    }

    function rawEntry(id) {
      return {
        template_id: id, label: id, cat: '', tier: null, pak_max_stack: 0,
        is_gradeable: false, mtx: false, non_tradeable: false, unknown: true,
      };
    }

    function closeResults() {
      results.hidden = true;
      results.innerHTML = '';
      activeIdx = -1;
      search.setAttribute('aria-expanded', 'false');
    }

    function renderChips() {
      // Only when something actually changed: every keystroke calls this, and a
      // re-render between a chip's mousedown and its click detaches the node the
      // click was headed for.
      var list = itemFamilyChips();
      var key = fam + '|' + list.map(function (c) { return c.key; }).join(',');
      if (key === chipsKey) return;
      chipsKey = key;
      chips.innerHTML = list.map(function (c) {
        return '<button type="button" class="v2-items-batch__chip' +
          (c.key === fam ? ' is-active' : '') + '" data-cat="' + escapeAttr(c.key) + '">' +
          escapeHTML(c.label) + '</button>';
      }).join('');
    }

    function renderResults() {
      if (giveItems === null) {
        results.innerHTML = '<div class="v2-items-batch__empty">Loading the item catalog...</div>';
        results.hidden = false;
        return;
      }
      if (giveItemsError) {
        results.innerHTML = '<div class="v2-items-batch__empty">Item catalog unavailable (' +
          escapeHTML(giveItemsError) + '). Type the raw template id instead.</div>';
        results.hidden = false;
        return;
      }
      var q = search.value.trim();
      var hits = searchItems(q, fam);
      if (!hits.length) {
        // Raw-id escape hatch: the catalog is operator shorthand, not a
        // whitelist, so a template it does not carry can still be granted and
        // the route stays warn-only for it.
        if (/^[A-Za-z0-9_]+$/.test(q)) {
          activeIdx = 0;
          results.innerHTML = itemOptHtml(rawEntry(q), true);
        } else {
          activeIdx = -1;
          results.innerHTML = '<div class="v2-items-batch__empty">No items match.</div>';
        }
      } else {
        activeIdx = 0;
        results.innerHTML = hits.map(function (e, i) { return itemOptHtml(e, i === 0); }).join('');
      }
      results.hidden = false;
      search.setAttribute('aria-expanded', 'true');
    }

    function moveActive(delta) {
      var opts = results.querySelectorAll('.v2-items-batch__opt');
      if (!opts.length) return;
      if (activeIdx >= 0 && opts[activeIdx]) opts[activeIdx].classList.remove('is-active');
      activeIdx = (activeIdx < 0)
        ? (delta > 0 ? 0 : opts.length - 1)
        : (activeIdx + delta + opts.length) % opts.length;
      opts[activeIdx].classList.add('is-active');
      if (opts[activeIdx].scrollIntoView) opts[activeIdx].scrollIntoView({ block: 'nearest' });
    }

    search.addEventListener('input', function () {
      var q = search.value.trim();
      if (tpl.value && q !== tpl.value) clearSelection();
      loadGiveItems(function () {
        var exact = giveItemById[search.value.trim()];
        if (exact && !tpl.value) selectEntry(exact);
        renderChips();
        renderResults();
      });
    });

    search.addEventListener('focus', function () {
      loadGiveItems(function () { renderChips(); renderResults(); });
    });

    search.addEventListener('blur', function () {
      // Tabbing away from a typed id resolves it rather than dropping the row.
      // A pick never lands here: the results handler is mousedown + preventDefault,
      // so focus never leaves the input.
      var q = search.value.trim();
      if (!tpl.value && /^[A-Za-z0-9_]+$/.test(q)) selectEntry(giveItemById[q] || rawEntry(q));
      closeResults();
    });

    search.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { closeResults(); return; }
      if (e.key === 'ArrowDown') { e.preventDefault(); moveActive(1); return; }
      if (e.key === 'ArrowUp') { e.preventDefault(); moveActive(-1); return; }
      if (e.key !== 'Enter') return;
      var opts = results.querySelectorAll('.v2-items-batch__opt');
      if (results.hidden || activeIdx < 0 || !opts[activeIdx]) return;
      e.preventDefault();
      selectEntry(entryFromOpt(opts[activeIdx]));
      closeResults();
    });

    // mousedown, not click: a click handler fires after blur, which would have
    // closed the list out from under the pointer.
    results.addEventListener('mousedown', function (e) {
      var opt = e.target.closest('.v2-items-batch__opt');
      if (!opt) return;
      e.preventDefault();
      selectEntry(entryFromOpt(opt));
      closeResults();
    });

    chips.addEventListener('click', function (e) {
      var chip = e.target.closest('.v2-items-batch__chip');
      if (!chip) return;
      fam = chip.getAttribute('data-cat') || '';
      renderChips();
      renderResults();
    });

    grade.addEventListener('click', function (e) {
      var pill = e.target.closest('.v2-items-batch__pill');
      if (!pill) return;
      setGrade(parseInt(pill.getAttribute('data-grade'), 10) || 0);
    });

    row.querySelector('.v2-items-batch__max').addEventListener('click', function () {
      if (stack.disabled) return;
      stack.value = String(stackCap());
    });

    row.querySelector('.v2-items-batch__rm').addEventListener('click', function () {
      if (row.parentNode) row.parentNode.removeChild(row);
      onRemove();
    });

    refreshRowState();
    loadGiveItems(function () { renderChips(); });

    if (preset && preset.template_id) {
      loadGiveItems(function () {
        selectEntry(giveItemById[preset.template_id] || rawEntry(preset.template_id));
        setGrade(parseInt(preset.quality, 10) || 0);
        if (!stack.disabled && preset.stack_size != null) stack.value = String(preset.stack_size);
        renderChips();
      });
    }
    return row;
  }

  /* ----------------------------- Config panel ---------------------------- */

  function fieldChoiceValues(f) {
    // Normalize choices to [{value, label}] regardless of input shape.
    return (f.choices || []).map(function (c) {
      if (c !== null && typeof c === 'object') return { value: c.value, label: c.label != null ? c.label : String(c.value) };
      return { value: c, label: String(c) };
    });
  }

  function renderField(container, f) {
    var wrap = document.createElement('div');
    wrap.className = 'v2-preset-param';
    var lbl = document.createElement('div');
    lbl.className = 'v2-preset-param__label';
    lbl.innerHTML = escapeHTML(f.label || f.name) +
                    (f.help ? ' <span class="v2-catalog__field-help">' + escapeHTML(f.help) + '</span>' : '');
    wrap.appendChild(lbl);

    var name = 'cf-' + f.name;
    var useSelect = (f.type === 'choice') && (f.widget === 'select' || fieldChoiceValues(f).length > 12);

    if (f.type === 'choice' && useSelect) {
      var choices = fieldChoiceValues(f);
      var sel = document.createElement('select');
      sel.name = name;
      sel.className = 'v2-preset-param__select';
      choices.forEach(function (c, idx) {
        var opt = document.createElement('option');
        opt.value = String(c.value);
        opt.textContent = c.label;
        if (f.default !== undefined ? String(c.value) === String(f.default) : idx === 0) opt.selected = true;
        sel.appendChild(opt);
      });
      var customInput = null;
      if (f.allow_custom) {
        var optC = document.createElement('option');
        optC.value = '__custom__';
        optC.textContent = 'Other (type manually)';
        if (!choices.length) optC.selected = true;
        sel.appendChild(optC);
        customInput = document.createElement('input');
        customInput.type = 'text';
        customInput.name = name + '-custom';
        customInput.className = 'v2-preset-param__custom';
        customInput.placeholder = 'type a value';
        customInput.hidden = sel.value !== '__custom__';
        sel.addEventListener('change', function () { customInput.hidden = sel.value !== '__custom__'; });
      }
      wrap.appendChild(sel);
      if (customInput) wrap.appendChild(customInput);
    } else if (f.type === 'choice') {
      fieldChoiceValues(f).forEach(function (c, idx) {
        var defaulted = (f.default !== undefined) ? (String(c.value) === String(f.default)) : (idx === 0);
        var radio = document.createElement('label');
        radio.className = 'v2-preset-param__choice';
        radio.innerHTML = '<input type="radio" name="' + name + '" value="' + escapeAttr(String(c.value)) + '"' +
                          (defaulted ? ' checked' : '') + ' /> <span>' + escapeHTML(c.label) + '</span>';
        wrap.appendChild(radio);
      });
    } else if (f.type === 'bool') {
      var box = document.createElement('label');
      box.className = 'v2-preset-param__choice';
      box.innerHTML = '<input type="checkbox" name="' + name + '"' + (f.default ? ' checked' : '') + ' /> <span>enable</span>';
      wrap.appendChild(box);
    } else if (f.type === 'items_batch') {
      // Repeating-row builder -> detail.items = [{template_id, stack_size, quality}].
      // Rows are built from the give-item catalog (see buildItemRow); the field's
      // own item_choices list is ignored and no longer sent.
      var ib = document.createElement('div');
      ib.className = 'v2-items-batch';
      ib.dataset.field = f.name;
      var maxRows = f.max_rows || 30;
      var rowsEl = document.createElement('div');
      rowsEl.className = 'v2-items-batch__rows';
      ib.appendChild(rowsEl);
      var addBtn = document.createElement('button');
      addBtn.type = 'button';
      addBtn.className = 'btn btn-sm btn-ghost v2-items-batch__add';
      addBtn.textContent = '+ add item';
      function ibSyncAdd() { addBtn.disabled = rowsEl.children.length >= maxRows; }
      function ibAddRow(preset) {
        if (rowsEl.children.length >= maxRows) return;
        rowsEl.appendChild(buildItemRow(f, ibSyncAdd, preset));
        ibSyncAdd();
      }
      addBtn.addEventListener('click', function () { ibAddRow(null); });
      ib.appendChild(addBtn);
      // Rebuild the rows of a cart line being edited.
      ib.wbSetRows = function (items) {
        rowsEl.innerHTML = '';
        (items || []).forEach(function (it) { ibAddRow(it); });
        if (!rowsEl.children.length) ibAddRow(null);
      };
      ibAddRow(null);
      wrap.appendChild(ib);
    } else {
      var inp = document.createElement('input');
      inp.name = name;
      inp.type = (f.type === 'int') ? 'number' : 'text';
      if (f.min !== undefined) inp.min = f.min;
      if (f.max !== undefined) inp.max = f.max;
      if (f.default !== undefined) inp.value = String(f.default);
      if (f.required) inp.required = true;
      wrap.appendChild(inp);
    }
    container.appendChild(wrap);
  }

  function readFields(fields) {
    var out = {};
    fields.forEach(function (f) {
      var name = 'cf-' + f.name;
      if (f.type === 'choice') {
        var useSelect = (f.widget === 'select' || fieldChoiceValues(f).length > 12);
        var raw;
        if (useSelect) {
          var sel = document.querySelector('select[name="' + name + '"]');
          if (!sel) return;
          raw = sel.value;
          if (raw === '__custom__') {
            var ci = document.querySelector('input[name="' + name + '-custom"]');
            raw = ci ? ci.value.trim() : '';
            if (raw !== '') out[f.name] = raw;   // custom value is a string
            return;
          }
        } else {
          var r = document.querySelector('input[name="' + name + '"]:checked');
          if (!r) return;
          raw = r.value;
        }
        // Coerce back to the original (possibly numeric) choice value.
        var match = fieldChoiceValues(f).find(function (c) { return String(c.value) === raw; });
        out[f.name] = match ? match.value : raw;
      } else if (f.type === 'bool') {
        var cb = document.querySelector('input[name="' + name + '"]');
        out[f.name] = !!(cb && cb.checked);
      } else if (f.type === 'int') {
        var ni = document.querySelector('input[name="' + name + '"]');
        if (ni && ni.value !== '') out[f.name] = parseInt(ni.value, 10);
      } else if (f.type === 'items_batch') {
        var ib = document.querySelector('.v2-items-batch[data-field="' + f.name + '"]');
        var arr = [];
        if (ib) {
          var rws = ib.querySelectorAll('.v2-items-batch__row');
          for (var ri = 0; ri < rws.length; ri++) {
            var row = rws[ri];
            var tplEl = row.querySelector('.v2-items-batch__tpl');
            var stEl = row.querySelector('.v2-items-batch__stack');
            var qlEl = row.querySelector('.v2-items-batch__qual');
            var tplv = tplEl ? tplEl.value : '';
            if (!tplv) continue;
            var sv = (stEl && stEl.value !== '') ? parseInt(stEl.value, 10) : 1;
            var qv = (qlEl && qlEl.value !== '') ? parseInt(qlEl.value, 10) : 0;
            arr.push({ template_id: tplv, stack_size: isNaN(sv) ? 1 : sv, quality: isNaN(qv) ? 0 : qv });
          }
        }
        out[f.name] = arr;
      } else {
        var ti = document.querySelector('input[name="' + name + '"]');
        if (ti) out[f.name] = ti.value.trim();
      }
    });
    return out;
  }

  function validateFields(fields, values) {
    for (var i = 0; i < fields.length; i++) {
      var f = fields[i];
      var v = values[f.name];
      var missing = (v === undefined || v === '' || v === null);
      if (f.required && missing) return 'missing required field: ' + (f.label || f.name);
      if (f.type === 'int' && !missing) {
        if (typeof v !== 'number' || isNaN(v)) return (f.label || f.name) + ' must be a number';
        if (f.min !== undefined && v < f.min) return (f.label || f.name) + ' below min (' + f.min + ')';
        if (f.max !== undefined && v > f.max) return (f.label || f.name) + ' above max (' + f.max + ')';
      }
      if (f.type === 'items_batch') {
        if (!Array.isArray(v) || v.length < 1) {
          if (f.required) return (f.label || f.name) + ': add at least one item';
        } else {
          var maxRows = f.max_rows || 30;
          if (v.length > maxRows) return (f.label || f.name) + ': max ' + maxRows + ' items';
          var smin = f.stack_min != null ? f.stack_min : 1, smax = f.stack_max != null ? f.stack_max : 10000;
          var qmin = f.quality_min != null ? f.quality_min : 0, qmax = f.quality_max != null ? f.quality_max : 5;
          for (var k = 0; k < v.length; k++) {
            var it = v[k];
            if (!it.template_id) return (f.label || f.name) + ': item ' + (k + 1) + ' has no template';
            if (it.stack_size < smin || it.stack_size > smax) return (f.label || f.name) + ': item ' + (k + 1) + ' stack out of range (' + smin + '..' + smax + ')';
            if (it.quality < qmin || it.quality > qmax) return (f.label || f.name) + ': item ' + (k + 1) + ' quality out of range (' + qmin + '..' + qmax + ')';
          }
        }
      }
    }
    return null;
  }

  function openConfig(source, key) {
    loadIndex(function () {
      var it = findIndexItem(source, key);
      var panel = $('wb-config');
      if (!it || !panel) return;
      panel.dataset.source = source;
      panel.dataset.key = String(key);

      var title = $('wb-config-title');
      if (title) title.textContent = it.label;
      var desc = $('wb-config-desc');
      if (desc) {
        desc.textContent = (source === 'preset')
          ? (it.entry.description || '')
          : (it.entry.category || '');
      }
      var offline = $('wb-config-offline');
      if (offline) offline.hidden = !it.ramFragile;

      var fieldsBox = $('wb-config-fields');
      if (fieldsBox) {
        fieldsBox.innerHTML = '';
        var fields = (source === 'preset') ? (it.entry.parameters || []) : (it.entry.fields || []);
        fields.forEach(function (f) { renderField(fieldsBox, f); });
      }

      // Prefill when editing.
      if (editingId != null) {
        var line = cart.find(function (l) { return l.id === editingId; });
        if (line) prefillConfig(it, source, line);
      }

      var addBtn = $('wb-config-add');
      if (addBtn) addBtn.textContent = (editingId != null) ? 'Update line' : 'Add to cart';
      setConfigStatus('');
      panel.hidden = false;
    });
  }

  function prefillConfig(it, source, line) {
    var values = (source === 'preset') ? (line.parameters || {}) : (line.detail || {});
    var fields = (source === 'preset') ? (it.entry.parameters || []) : (it.entry.fields || []);
    fields.forEach(function (f) {
      if (!(f.name in values)) return;
      var name = 'cf-' + f.name;
      var v = values[f.name];
      if (f.type === 'choice') {
        var useSelect = (f.widget === 'select' || fieldChoiceValues(f).length > 12);
        if (useSelect) {
          var sel = document.querySelector('select[name="' + name + '"]');
          if (!sel) return;
          var known = fieldChoiceValues(f).some(function (c) { return String(c.value) === String(v); });
          if (known) { sel.value = String(v); }
          else if (f.allow_custom) {
            sel.value = '__custom__';
            var ci = document.querySelector('input[name="' + name + '-custom"]');
            if (ci) { ci.value = String(v); ci.hidden = false; }
          }
        } else {
          var r = document.querySelector('input[name="' + name + '"][value="' + escapeAttr(String(v)) + '"]');
          if (r) r.checked = true;
        }
      } else if (f.type === 'bool') {
        var cb = document.querySelector('input[name="' + name + '"]');
        if (cb) cb.checked = !!v;
      } else if (f.type === 'items_batch') {
        var ib = document.querySelector('.v2-items-batch[data-field="' + f.name + '"]');
        if (ib && ib.wbSetRows && Array.isArray(v)) ib.wbSetRows(v);
      } else {
        var inp = document.querySelector('input[name="' + name + '"]');
        if (inp) inp.value = String(v);
      }
    });
  }

  function setConfigStatus(msg, kind) {
    var el = $('wb-config-status');
    if (el) { el.textContent = msg || ''; el.className = 'msg' + (kind ? ' ' + kind : ''); }
  }

  function closeConfig() {
    var panel = $('wb-config');
    if (panel) panel.hidden = true;
    editingId = null;
  }

  function commitConfig() {
    var panel = $('wb-config');
    if (!panel) return;
    var source = panel.dataset.source;
    var key = panel.dataset.key;
    var it = findIndexItem(source, key);
    if (!it) { setConfigStatus('grant no longer available', 'err'); return; }

    if (source === 'preset') {
      var params = it.entry.parameters || [];
      var values = readFields(params);
      var err = validateFields(params, values);
      if (err) { setConfigStatus(err, 'err'); return; }
      addOrUpdateLine(makeLine('preset', it, values));
    } else {
      var fields = it.entry.fields || [];
      var detail = readFields(fields);
      var err2 = validateFields(fields, detail);
      if (err2) { setConfigStatus(err2, 'err'); return; }
      addOrUpdateLine(makeLine('custom', it, detail));
    }
    closeConfig();
  }

  /* ------------------------------- Cart ---------------------------------- */

  function makeLine(kind, it, values) {
    var line = {
      id: (editingId != null) ? editingId : (++cartSeq),
      kind: kind,
      key: it.key,
      label: it.label,
      ramFragile: !!it.ramFragile,
    };
    if (kind === 'preset') {
      line.parameters = values;
      line.opsCount = (it.entry.ops || []).length || 1;
      line.summary = summariseDetail(values) || (line.opsCount + ' ops');
    } else {
      line.detail = values;
      line.opsCount = 1;
      line.summary = summariseDetail(values);
    }
    return line;
  }

  function addOrUpdateLine(line) {
    if (editingId != null) {
      var i = cart.findIndex(function (l) { return l.id === editingId; });
      if (i !== -1) cart[i] = line; else cart.push(line);
      editingId = null;
    } else {
      if (cart.length >= MAX_CART) { setConfigStatus('cart is full (max ' + MAX_CART + ')', 'err'); return; }
      cart.push(line);
    }
    renderCart();
    onStateChange();
  }

  function removeLine(id) {
    cart = cart.filter(function (l) { return l.id !== id; });
    if (editingId === id) { editingId = null; closeConfig(); }
    renderCart();
    onStateChange();
  }

  function editLine(id) {
    var line = cart.find(function (l) { return l.id === id; });
    if (!line) return;
    editingId = id;
    // A line's kind is preset|custom; the search index's source is
    // preset|catalog. Passing the kind straight through made findIndexItem miss
    // every custom line, so edit opened nothing and left editingId set.
    openConfig(line.kind === 'preset' ? 'preset' : 'catalog', line.key);
  }

  function renderCart() {
    var list = $('wb-cart-lines');
    var count = $('wb-cart-count');
    var clear = $('wb-cart-clear');
    if (count) count.textContent = String(cart.length);
    if (clear) clear.hidden = cart.length === 0;
    if (!list) return;
    list.innerHTML = '';
    if (!cart.length) {
      var empty = document.createElement('li');
      empty.className = 'v2-cart--empty';
      empty.textContent = 'Cart is empty. Search or browse a grant to add it.';
      list.appendChild(empty);
      return;
    }
    cart.forEach(function (line) {
      var li = document.createElement('li');
      li.className = 'v2-cart__line';
      li.setAttribute('data-id', line.id);
      var flag = line.ramFragile ? '<span class="v2-cart__line-flag">offline</span>' : '';
      var dflag = line.destructive ? '<span class="v2-cart__line-flag v2-cart__line-flag--danger">destructive</span>' : '';
      li.innerHTML =
        '<div class="v2-cart__line-meta">' +
          '<div class="v2-cart__line-title">' + escapeHTML(line.label) +
          (line.kind === 'preset' ? ' <code>preset</code>' : '') + '</div>' +
          '<div class="v2-cart__line-summary">' + escapeHTML(line.summary || '') + '</div>' +
          flag + dflag +
        '</div>' +
        '<button type="button" class="v2-cart__line-edit" aria-label="edit">edit</button>' +
        '<button type="button" class="v2-cart__line-x" aria-label="remove">x</button>';
      li.querySelector('.v2-cart__line-edit').addEventListener('click', function () { editLine(line.id); });
      li.querySelector('.v2-cart__line-x').addEventListener('click', function () { removeLine(line.id); });
      list.appendChild(li);
    });
  }

  function clearCart() {
    cart = [];
    editingId = null;
    renderCart();
    onStateChange();
  }

  /* --------------------------- Live query view --------------------------- */

  function totalFires() {
    var perRecipient = cart.reduce(function (a, l) { return a + (l.opsCount || 1); }, 0);
    return batch.length * perRecipient;
  }

  function offlineQueuedFires() {
    var perRecipient = cart.reduce(function (a, l) { return a + (l.ramFragile ? (l.opsCount || 1) : 0); }, 0);
    return batch.length * perRecipient;
  }

  function buildQueryObject() {
    return {
      recipients: batch.map(function (r) { return r.aid; }),
      lines: cart.map(function (l) {
        return (l.kind === 'preset')
          ? { kind: 'preset', preset_name: l.key, parameters: l.parameters || {} }
          : { kind: 'custom', grant_type: l.key, detail: l.detail || {} };
      }),
    };
  }

  function onlineRecipients() {
    return batch.filter(function (r) { return r.online_status === 'online' || r.in_grace_period; });
  }

  function renderQuery() {
    var body = $('wb-query-body');
    if (body) body.textContent = JSON.stringify(buildQueryObject(), null, 2);
    var summary = $('wb-query-summary');
    if (summary) {
      var t = totalFires();
      var k = offlineQueuedFires();
      summary.textContent = batch.length + ' recipients x ' + cart.length + ' lines = ' +
                            t + ' fires; ' + k + ' offline-queued';
    }
  }

  /* ------------------------------ Fire bar ------------------------------- */

  function offlineWarningText() {
    var online = onlineRecipients();
    var hasOfflineLine = cart.some(function (l) { return l.ramFragile; });
    if (!online.length || !hasOfflineLine) return null;
    return online.length + ' of ' + batch.length + ' recipients are online; offline-only grants ' +
           'queue until logout + grace (30s safe zones / ~5 min Deep Desert/PvP).';
  }

  function renderFireBar() {
    var btn = $('wb-fire-btn');
    var summary = $('wb-fire-summary');
    var bar = $('wb-fire-bar');
    var t = totalFires();
    var reason = '';
    if (!batch.length) reason = 'add at least one recipient';
    else if (!cart.length) reason = 'add at least one grant to the cart';
    else if (t > MAX_TOTAL_FIRES) reason = 'too many fires (' + t + ' > ' + MAX_TOTAL_FIRES + ')';
    if (btn) {
      btn.disabled = reason !== '';
      btn.textContent = reason ? ('Fire (' + reason + ')') : ('Fire ' + t + ' grant' + (t === 1 ? '' : 's'));
    }
    var warn = offlineWarningText();
    // Leave #wb-fire-summary's own class intact; the warn colour is driven by
    // the bar's .v2-fire--warn toggle (dev-ui CSS).
    if (summary) summary.textContent = warn || (reason ? '' : (batch.length + ' recipients, ' + cart.length + ' lines'));
    if (bar) bar.classList.toggle('v2-fire--warn', !!warn);
  }

  function onStateChange() {
    renderCart();
    renderQuery();
    renderFireBar();
    refreshPickerState();
  }

  /* --------------------------- Confirm + fire ---------------------------- */

  function openCartConfirm() {
    var overlay = $('cart-confirm-overlay');
    if (!overlay) return;
    if (!batch.length || !cart.length) return;

    var t = totalFires();
    if (t > MAX_TOTAL_FIRES) { renderFireBar(); return; }

    var title = $('cart-confirm-title');
    if (title) title.textContent = 'Fire ' + t + ' grant' + (t === 1 ? '' : 's');
    var desc = $('cart-confirm-desc');
    if (desc) desc.textContent = batch.length + ' recipients x ' + cart.length + ' cart lines';

    var ops = $('cart-confirm-ops');
    if (ops) {
      ops.innerHTML = '';
      cart.forEach(function (line) {
        var li = document.createElement('li');
        li.className = 'v2-preset-op';
        var flag = line.ramFragile ? ' <span class="v2-cart__line-flag">offline</span>' : '';
        var body = (line.kind === 'preset' ? 'preset ' : '') + escapeHTML(line.label);
        var sub = line.summary ? ' <span class="v2-preset-op__detail">' + escapeHTML(line.summary) + '</span>' : '';
        li.innerHTML = '<code>' + escapeHTML(String(line.key)) + '</code> ' + body + sub + flag;
        ops.appendChild(li);
      });
    }

    var rlist = $('cart-confirm-recipient-list');
    if (rlist) {
      rlist.innerHTML = '';
      batch.forEach(function (r) {
        var li = document.createElement('li');
        var dot = r.online_status === 'online' ? ' (online)' : (r.in_grace_period ? ' (grace)' : '');
        li.innerHTML = '<span>' + escapeHTML(r.name) + '</span> <code>#' + r.aid + '</code>' + escapeHTML(dot);
        rlist.appendChild(li);
      });
    }
    var rcount = $('cart-confirm-recipient-count');
    if (rcount) rcount.textContent = batch.length;

    var offline = $('cart-confirm-offline');
    if (offline) {
      var warn = offlineWarningText();
      offline.textContent = warn || '';
      offline.hidden = !warn;
    }

    var token = $('cart-confirm-token');
    if (token) token.value = '';
    setCartStatus('');

    overlay.classList.add('active');
    setTimeout(function () { var tk = $('cart-confirm-token'); if (tk) tk.focus(); }, 60);
  }

  function closeCartConfirm() {
    var overlay = $('cart-confirm-overlay');
    if (overlay) overlay.classList.remove('active');
  }
  window.closeCartConfirm = closeCartConfirm;

  function setCartStatus(msg, kind) {
    var el = $('cart-confirm-status');
    if (el) { el.textContent = msg || ''; el.className = 'msg' + (kind ? ' ' + kind : ''); }
  }

  function submitCartFire() {
    var token = $('cart-confirm-token');
    var tokenVal = token ? token.value.trim() : '';
    if (!batch.length) { setCartStatus('add at least one recipient', 'err'); return; }
    if (!cart.length) { setCartStatus('cart is empty', 'err'); return; }
    if (tokenVal !== 'FIRE') { setCartStatus('type FIRE to confirm', 'err'); return; }

    var t = totalFires();
    if (t > SOFT_CONFIRM_FIRES && t <= MAX_TOTAL_FIRES) {
      if (!window.confirm('This will fire ' + t + ' grants. Continue?')) return;
    }

    setCartStatus('firing...', null);
    var btn = $('cart-confirm-submit');
    if (btn) btn.disabled = true;

    var payload = buildQueryObject();
    payload.confirm_token = 'FIRE';
    var liveEl = $('cart-confirm-live');
    payload.live_delivery = !!(liveEl && liveEl.checked);

    fetch('/admin/v2/players/grants/cart/fire', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': (typeof csrfToken !== 'undefined') ? csrfToken : '',
      },
      body: JSON.stringify(payload),
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
      .then(function (res) {
        if (btn) btn.disabled = false;
        if (!res.ok) {
          setCartStatus((res.data && (res.data.detail || res.data.message)) || 'fire failed', 'err');
          return;
        }
        var d = res.data;
        var msg = 'fired ' + d.ops_fired + ' op' + (d.ops_fired === 1 ? '' : 's');
        if (d.ops_failed) msg += ' / ' + d.ops_failed + ' failed';
        if (d.batch_id) msg += ' / batch ' + d.batch_id.slice(0, 8) + '...';
        setCartStatus(msg, d.ops_failed ? 'err' : 'ok');

        var aid = window.WORKBENCH_DEFAULT_AID;
        if (aid && window.htmx) {
          var recent = $('workbench-recent');
          if (recent) window.htmx.ajax('GET', '/admin/v2/players/grants/_recent?aid=' + aid, recent);
        }
        if (!d.ops_failed) {
          clearCart();
          setTimeout(closeCartConfirm, 1400);
        }
      })
      .catch(function (e) {
        if (btn) btn.disabled = false;
        setCartStatus('network error: ' + e.message, 'err');
      });
  }

  /* ---------------------- Spec / Skill grant pickers --------------------- */
  /* The two "Specializations" / "Skills / Abilities" catalog tiles carry an
   * `opens` marker (spec_picker / skill_picker). Clicking one loads a rich modal
   * fragment instead of the generic #wb-config form. The modal pushes the SAME
   * custom cart lines the cart already fires (keystone, spec_xp, spec_unlock_*,
   * grant_full_job_tree, grant_skill_block, reset_full_skill_area). The shipped
   * cart fire/preview path is untouched. */

  function catalogRamFragile(grantType) {
    for (var i = 0; i < indexData.catalog.length; i++) {
      if (indexData.catalog[i].grant_type === grantType) return !!indexData.catalog[i].ram_fragile;
    }
    return true;   // safe default: badge unknowns as offline-only
  }

  function pickerAid() {
    if (batch.length === 1) return batch[0].aid;
    if (window.WORKBENCH_DEFAULT_AID) return window.WORKBENCH_DEFAULT_AID;
    return null;
  }

  function pickerOverlay() { return $('wb-picker-overlay'); }

  function lineExists(grantType, detail) {
    var sig = JSON.stringify(detail || {});
    return cart.some(function (l) {
      return l.kind === 'custom' && l.key === grantType && JSON.stringify(l.detail || {}) === sig;
    });
  }

  function findKeystoneLine(kid) {
    return cart.find(function (l) {
      return l.kind === 'custom' && l.key === 'keystone' && l.detail &&
             Number(l.detail.keystone_id) === Number(kid);
    });
  }

  function findBlockLine(block) {
    return cart.find(function (l) {
      return l.kind === 'custom' && l.key === 'grant_skill_block' && l.detail && l.detail.block === block;
    });
  }

  function addPickerCartLine(grantType, label, detail, opts) {
    opts = opts || {};
    if (cart.length >= MAX_CART) { flashPicker('cart is full (max ' + MAX_CART + ')'); return false; }
    if (lineExists(grantType, detail)) { flashPicker('already in cart'); return false; }
    cart.push({
      id: ++cartSeq,
      kind: 'custom',
      key: grantType,
      label: label,
      ramFragile: catalogRamFragile(grantType),
      detail: detail,
      opsCount: 1,
      summary: opts.summary || summariseDetail(detail),
      destructive: !!opts.destructive,
    });
    renderCart();
    onStateChange();
    return true;
  }
  // Public hook (lead-requested) for any fragment-side cart push.
  window.workbenchAddCartLine = function (gt, label, detail, opts) {
    return addPickerCartLine(gt, label, detail, opts || {});
  };

  function flashPicker(msg) {
    var ov = pickerOverlay();
    if (!ov) return;
    ov.querySelectorAll('[data-picker-cartcount]').forEach(function (el) { el.textContent = msg; });
    setTimeout(refreshPickerState, 1600);
  }

  function refreshPickerState() {
    var ov = pickerOverlay();
    if (!ov || !ov.classList.contains('active')) return;
    ov.querySelectorAll('[data-picker-cartcount]').forEach(function (el) {
      el.textContent = 'Cart: ' + cart.length + ' line' + (cart.length === 1 ? '' : 's');
    });
    ov.querySelectorAll('button[data-action="spec-trait-add"]').forEach(function (btn) {
      var inCart = !!findKeystoneLine(btn.getAttribute('data-keystone-id'));
      btn.textContent = inCart ? 'remove' : '+ add';
      btn.classList.toggle('is-incart', inCart);
      var row = btn.closest('.v2-spec-row');
      if (row) row.classList.toggle('v2-spec-row--incart', inCart);
    });
    ov.querySelectorAll('button[data-action="skill-block-add"]').forEach(function (btn) {
      var inCart = !!findBlockLine(btn.getAttribute('data-block'));
      btn.textContent = inCart ? 'remove' : '+ add';
      btn.classList.toggle('is-incart', inCart);
      var item = btn.closest('.v2-skill-item');
      if (item) item.classList.toggle('is-incart', inCart);
    });
  }

  function setActiveTab(el) {
    var parent = el.parentNode;
    if (!parent) return;
    parent.querySelectorAll('.v2-picker__tab').forEach(function (b) { b.classList.remove('is-active'); });
    el.classList.add('is-active');
  }

  function specAddXp(t) {
    var track = t.getAttribute('data-track');
    var xpEl = document.getElementById('spec-xp-input');
    var lvlEl = document.getElementById('spec-level-input');
    var advEl = document.getElementById('spec-advanced');
    var warn = document.getElementById('spec-decrease-warn');
    var xp = (xpEl && xpEl.value !== '') ? parseInt(xpEl.value, 10) : null;
    var lvl = (lvlEl && lvlEl.value !== '') ? parseInt(lvlEl.value, 10) : null;
    if (xp === null && lvl === null) { flashPicker('enter XP or level'); return; }
    // E4: always send an explicit mode. Default set; add only behind Advanced.
    var mode = (advEl && advEl.checked) ? 'add' : 'set';
    var container = t.closest('.v2-spec-traits');
    var curXp = container ? parseInt(container.getAttribute('data-track-xp'), 10) : NaN;
    if (mode === 'set' && xp !== null && !isNaN(curXp) && xp < curXp) {
      if (warn) { warn.hidden = false; warn.textContent = 'Set XP ' + xp + ' is below current ' + curXp + '; this DECREASES the track. Click again to confirm.'; }
      if (!t.dataset.confirmDecrease) { t.dataset.confirmDecrease = '1'; return; }
    }
    if (warn) warn.hidden = true;
    delete t.dataset.confirmDecrease;
    var detail = { track_type: track, mode: mode };
    if (xp !== null) detail.xp = xp;
    if (lvl !== null) detail.level = lvl;
    var sum = track + ' | ' + (xp !== null ? ('xp ' + xp) : 'xp -') + ' | ' +
              (lvl !== null ? ('level ' + lvl) : 'level -') + ' | ' + mode;
    addPickerCartLine('spec_xp', 'Specialization XP', detail, { summary: sum });
  }

  function specTraitToggle(t) {
    var kid = parseInt(t.getAttribute('data-keystone-id'), 10);
    var name = t.getAttribute('data-name') || ('id ' + kid);
    var track = t.getAttribute('data-track') || '';
    var override = t.getAttribute('data-override') === '1';
    var existing = findKeystoneLine(kid);
    if (existing) { removeLine(existing.id); }
    else {
      var summary = track + ' / id=' + kid + ' ' + name + (override ? ' [above level: admin override]' : '');
      addPickerCartLine('keystone', 'Trait Unlock', { keystone_id: kid }, { summary: summary });
    }
    refreshPickerState();
  }

  function skillBlockToggle(t) {
    var block = t.getAttribute('data-block');
    var label = t.getAttribute('data-label') || block;
    var existing = findBlockLine(block);
    if (existing) { removeLine(existing.id); }
    else { addPickerCartLine('grant_skill_block', 'Grant Skill Block', { block: block }, { summary: label + ' (' + block + ')' }); }
    refreshPickerState();
  }

  function handlePickerClick(e) {
    var t = e.target.closest('[data-action]');
    if (!t) return;
    var ov = pickerOverlay();
    if (!ov || !ov.contains(t)) return;
    var action = t.getAttribute('data-action');
    if (action === 'picker-close') { closePicker(); return; }
    if (action === 'spec-track-tab' || action === 'skill-job-tab') { setActiveTab(t); return; }
    if (action === 'spec-max') {
      var mtrack = t.getAttribute('data-track');
      addPickerCartLine('spec_xp', 'Specialization XP',
        { track_type: mtrack, xp: 44182, level: 100, mode: 'set' },
        { summary: mtrack + ' | xp 44182 | level 100 | set' });
      return;
    }
    if (action === 'spec-add-xp') { specAddXp(t); return; }
    if (action === 'spec-unlock-track') {
      var utrack = t.getAttribute('data-track');
      addPickerCartLine('spec_unlock_track', 'Unlock Track Traits',
        { track_type: utrack }, { summary: utrack + ' (41 traits)' });
      return;
    }
    if (action === 'spec-unlock-all') {
      addPickerCartLine('spec_unlock_all', 'Unlock All Traits', {}, { summary: 'all 205 traits' });
      return;
    }
    if (action === 'spec-reset') {
      var rsel = document.getElementById('spec-reset-track');
      var scope = rsel ? rsel.value : 'all';
      // E3 labels: single track = XP/level only; "all" = XP + traits.
      var rlabel = (scope === 'all') ? 'Reset ALL specs (XP + traits)' : ('Reset track XP/level (' + scope + ')');
      addPickerCartLine('reset_specs', 'Reset Specializations',
        { track_type: scope }, { summary: rlabel, destructive: true });
      return;
    }
    if (action === 'spec-trait-add') { specTraitToggle(t); return; }
    if (action === 'skill-full-tree') {
      var fjob = t.getAttribute('data-job'), fjl = t.getAttribute('data-job-label') || fjob;
      addPickerCartLine('grant_full_job_tree', 'Grant Full Skill Tree',
        { job: fjob }, { summary: fjl + ' (all 6 blocks)' });
      return;
    }
    if (action === 'skill-block-add') { skillBlockToggle(t); return; }
    if (action === 'skill-reset') {
      var rjob = t.getAttribute('data-job'), rjl = t.getAttribute('data-job-label') || rjob;
      addPickerCartLine('reset_full_skill_area', 'Reset Skill Area',
        { job: rjob }, { summary: rjl + ' (full tree nuke)', destructive: true });
      return;
    }
  }

  function openPicker(kind) {
    var overlay = pickerOverlay();
    var body = $('wb-picker-body');
    if (!overlay || !body) return;
    var aid = pickerAid();
    var ep = (kind === 'skill_picker') ? '_skill_picker' : '_spec_picker';
    var url = '/admin/v2/players/grants/' + ep + (aid ? ('?aid=' + aid) : '');
    body.innerHTML = '<div class="msg loading">loading picker...</div>';
    overlay.classList.add('active');
    if (window.htmx) {
      window.htmx.ajax('GET', url, { target: '#wb-picker-body', swap: 'innerHTML' })
        .then(refreshPickerState)
        .catch(function () { body.innerHTML = '<div class="msg error">failed to load picker</div>'; });
    } else {
      fetch(url, { credentials: 'same-origin' })
        .then(function (r) { return r.text(); })
        .then(function (html) { body.innerHTML = html; refreshPickerState(); })
        .catch(function () { body.innerHTML = '<div class="msg error">failed to load picker</div>'; });
    }
  }

  function closePicker() {
    var overlay = pickerOverlay();
    if (overlay) overlay.classList.remove('active');
  }
  window.closePicker = closePicker;

  // Route a catalog tile/suggestion to the picker when it carries an `opens`
  // marker; otherwise fall through to the generic config panel.
  function activate(source, key) {
    if (!indexLoaded) { loadIndex(function () { activate(source, key); }); return; }
    var it = findIndexItem(source, key);
    if (it && source === 'catalog' && it.entry && it.entry.opens) { openPicker(it.entry.opens); return; }
    openConfig(source, key);
  }

  function bindPickerControls() {
    var ov = pickerOverlay();
    if (ov && ov.dataset.bound !== '1') { ov.dataset.bound = '1'; ov.addEventListener('click', handlePickerClick); }
  }

  /* -------------------------------- init --------------------------------- */

  function bindConfigControls() {
    var add = $('wb-config-add');
    if (add && add.dataset.bound !== '1') { add.dataset.bound = '1'; add.addEventListener('click', commitConfig); }
    var cancel = $('wb-config-cancel');
    if (cancel && cancel.dataset.bound !== '1') { cancel.dataset.bound = '1'; cancel.addEventListener('click', closeConfig); }
    var cartClear = $('wb-cart-clear');
    if (cartClear && cartClear.dataset.bound !== '1') { cartClear.dataset.bound = '1'; cartClear.addEventListener('click', clearCart); }
    var fireBtn = $('wb-fire-btn');
    if (fireBtn && fireBtn.dataset.bound !== '1') { fireBtn.dataset.bound = '1'; fireBtn.addEventListener('click', openCartConfirm); }
    var fireSubmit = $('cart-confirm-submit');
    if (fireSubmit && fireSubmit.dataset.bound !== '1') { fireSubmit.dataset.bound = '1'; fireSubmit.addEventListener('click', submitCartFire); }
  }

  function bindBrowseTiles() {
    // Tiles are created by renderBrowse with listeners already bound; this is a
    // safety net for any server-rendered tiles inside #wb-browse.
    var tiles = document.querySelectorAll('#wb-browse .v2-catalog__tile');
    tiles.forEach(function (tile) {
      if (tile.dataset.bound === '1') return;
      tile.dataset.bound = '1';
      tile.addEventListener('click', function () {
        activate(tile.getAttribute('data-source'), tile.getAttribute('data-key'));
      });
    });
  }

  function bindPresetShelf() {
    // The server-rendered preset shelf cards route to the cart config panel
    // (not the legacy preset-confirm overlay).
    var cards = document.querySelectorAll('.v2-preset-card');
    cards.forEach(function (card) {
      if (card.dataset.wbBound === '1') return;
      card.dataset.wbBound = '1';
      card.addEventListener('click', function (e) {
        var name = card.getAttribute('data-preset-name');
        if (!name) return;
        e.stopPropagation();
        openConfig('preset', name);
      }, true);
    });
  }

  function init() {
    bindBatchSearch();
    bindBatchControls();
    bindGrantSearch();
    bindConfigControls();
    bindPresetShelf();
    bindBrowseTiles();
    bindPickerControls();
    renderBatch();
    renderCart();
    renderQuery();
    renderFireBar();

    loadPlayers();
    loadIndex();

    if (window.WORKBENCH_DEFAULT_AID) {
      var addCurrent = $('batch-add-current');
      var name = addCurrent ? addCurrent.getAttribute('data-name') : null;
      addToBatch(window.WORKBENCH_DEFAULT_AID, name);
    }

    document.body.addEventListener('htmx:afterSwap', function () {
      bindBatchSearch();
      bindBatchControls();
      bindGrantSearch();
      bindConfigControls();
      bindPresetShelf();
      bindBrowseTiles();
      bindPickerControls();
      renderBatch();
      renderCart();
      renderFireBar();
      refreshPickerState();
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        closeCartConfirm();
        closeConfig();
        closePicker();
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
