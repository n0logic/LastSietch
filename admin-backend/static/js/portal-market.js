/* Last Sietch portal — CHOAM exchange browser (read-only).
 * Search by item name / category chips / sort all fetch
 * /portal/market/search?q=&sort=, which returns a server-rendered fragment.
 * Each result row opens a price-ladder drawer via /portal/market/item?tpl=.
 * Vanilla, no deps. CSP is script-src 'self', so icon fallbacks + handlers are
 * wired here in JS, not via inline attributes.
 */
(function () {
  'use strict';

  var input = document.getElementById('market-search-input');
  var results = document.getElementById('market-results');
  var tabs = document.querySelectorAll('.market-tab');
  var sortBtns = document.querySelectorAll('[data-market-sort]');
  var kindBtns = document.querySelectorAll('[data-market-kind]');
  if (!results) return;

  var FALLBACK = '/admin/static/img/dune-icons/T_UI_IconItemUnknownS_D.png';
  var searchTimer = null;
  var activeReq = 0;
  var curQuery = '';
  var curSort = 'active';
  var curCat = 'all';
  var curKind = 'all';

  var CSRF_COOKIE = 'ls_portal_csrf';
  var CSRF_HEADER = 'X-Portal-CSRF-Token';

  function getCookie(name) {
    var m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : '';
  }

  function wireIconFallbacks(scope) {
    var imgs = scope.querySelectorAll('img[data-icon-fallback]');
    for (var i = 0; i < imgs.length; i++) {
      imgs[i].addEventListener('error', function () {
        if (this.src.indexOf('T_UI_IconItemUnknownS_D') !== -1) return;
        this.src = FALLBACK;
      });
    }
  }

  /* Re-render the "Price alerts" card after a watch is added/removed so it
   * updates live without a full page reload. The remove forms carry a baked-in
   * CSRF token from the server render, so they still work after replacement. */
  function refreshWatches() {
    var card = document.getElementById('watches');
    if (!card) return;
    fetch('/portal/market/watches', { credentials: 'same-origin', headers: { 'Accept': 'text/html' } })
      .then(function (r) { if (!r.ok) throw new Error('http_' + r.status); return r.text(); })
      .then(function (html) { card.innerHTML = html; wireIconFallbacks(card); wireWatchSearch(card); })
      .catch(function () { /* leave the existing card; refresh is best-effort */ });
  }

  /* Wire the "add an alert" search box in the Price alerts card: type an item
   * name -> JSON item picker -> pick one -> set a target price -> POST to
   * /portal/watchlist/add. Re-wired after each refreshWatches() re-render. */
  function wireWatchSearch(scope) {
    var box = (scope || document).querySelector('[data-watch-add]');
    if (!box) return;
    var search = box.querySelector('[data-watch-search]');
    var resultsEl = box.querySelector('[data-watch-results]');
    var chosen = box.querySelector('[data-watch-chosen]');
    var chosenIcon = box.querySelector('[data-watch-chosen-icon]');
    var chosenName = box.querySelector('[data-watch-chosen-name]');
    var priceEl = box.querySelector('[data-watch-price]');
    var confirmBtn = box.querySelector('[data-watch-confirm]');
    var clearBtn = box.querySelector('[data-watch-clear]');
    var msg = box.querySelector('.watch-add__msg');
    if (!search || !resultsEl || !chosen) return;
    var timer = null;
    var selTpl = '';

    function note(text, kind) {
      if (!msg) return;
      msg.textContent = text || '';
      msg.className = 'watch-add__msg' + (kind ? ' watch-add__msg--' + kind : '');
    }
    function hideResults() { resultsEl.hidden = true; resultsEl.innerHTML = ''; }

    function pick(tpl, name, icon, cheapest) {
      selTpl = tpl;
      chosenName.textContent = name;
      if (chosenIcon) chosenIcon.src = '/admin/static/img/dune-icons/' + icon + '.png';
      if (priceEl) priceEl.value = cheapest || '';
      chosen.hidden = false;
      hideResults();
      search.value = name;
      if (priceEl) priceEl.focus();
    }
    function clearSel() {
      selTpl = ''; chosen.hidden = true; note(''); search.value = '';
      hideResults(); search.focus();
    }

    function renderResults(items) {
      if (!items || !items.length) { hideResults(); return; }
      var html = '';
      for (var i = 0; i < items.length; i++) {
        var it = items[i];
        var nameAttr = (it.name || '').replace(/"/g, '&quot;');
        html += '<li><button type="button" class="watch-add__result" ' +
          'data-tpl="' + it.template_id + '" data-name="' + nameAttr + '" ' +
          'data-icon="' + it.icon + '" data-cheapest="' + (it.cheapest || '') + '">' +
          '<img class="cont-item__icon" loading="lazy" alt="" data-icon-fallback ' +
          'src="/admin/static/img/dune-icons/' + it.icon + '.png">' +
          '<span class="watch-add__result-name">' + (it.name || '') +
          (it.is_schematic ? ' <span class="market-badge market-badge--schematic">Schematic</span>' : '') +
          '</span>' +
          (it.cheapest_display ? '<span class="watch-add__result-price mono">' + it.cheapest_display + '</span>' : '') +
          '</button></li>';
      }
      resultsEl.innerHTML = html;
      resultsEl.hidden = false;
      wireIconFallbacks(resultsEl);
      var btns = resultsEl.querySelectorAll('.watch-add__result');
      for (var k = 0; k < btns.length; k++) {
        btns[k].addEventListener('click', function () {
          pick(this.getAttribute('data-tpl'), this.getAttribute('data-name'),
               this.getAttribute('data-icon'), this.getAttribute('data-cheapest'));
        });
      }
    }

    search.addEventListener('input', function () {
      var q = search.value.trim();
      chosen.hidden = true; selTpl = ''; note('');
      if (timer) window.clearTimeout(timer);
      if (q.length < 2) { hideResults(); return; }
      timer = window.setTimeout(function () {
        fetch('/portal/market/item-search?q=' + encodeURIComponent(q),
              { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
          .then(function (r) { return r.json(); })
          .then(function (j) { renderResults(j && j.items); })
          .catch(function () { hideResults(); });
      }, 250);
    });

    if (clearBtn) clearBtn.addEventListener('click', clearSel);

    if (confirmBtn) confirmBtn.addEventListener('click', function () {
      if (!selTpl) { note('Pick an item first.', 'warn'); return; }
      var val = (priceEl.value || '').trim();
      if (!/^\d+$/.test(val) || parseInt(val, 10) <= 0) {
        note('Enter a whole number price above 0.', 'warn'); return;
      }
      confirmBtn.setAttribute('disabled', 'disabled');
      confirmBtn.setAttribute('aria-busy', 'true');
      var body = new URLSearchParams();
      body.set('template_id', selTpl);
      body.set('max_price', val);
      var headers = { 'Accept': 'application/json',
                      'Content-Type': 'application/x-www-form-urlencoded' };
      headers[CSRF_HEADER] = getCookie(CSRF_COOKIE);
      fetch('/portal/watchlist/add',
            { method: 'POST', credentials: 'same-origin', headers: headers, body: body.toString() })
        .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; },
                                                  function () { return { ok: r.ok, j: null }; }); })
        .then(function (res) {
          if (res.ok && res.j && res.j.ok) {
            refreshWatches();  // re-renders + re-wires the whole card
          } else {
            note((res.j && res.j.error) || 'Could not add that alert.', 'warn');
            confirmBtn.removeAttribute('disabled');
            confirmBtn.removeAttribute('aria-busy');
          }
        })
        .catch(function () {
          note('Could not add that alert. Please try again.', 'warn');
          confirmBtn.removeAttribute('disabled');
          confirmBtn.removeAttribute('aria-busy');
        });
    });
  }

  /* Copy-to-clipboard for the item name (the listing text isn't selectable).
   * Wired on each freshly-rendered drawer; shows a brief "Copied" state. */
  function wireCopyName(scope) {
    var btns = scope.querySelectorAll('[data-copy-name]');
    for (var i = 0; i < btns.length; i++) {
      btns[i].addEventListener('click', function () {
        var btn = this;
        var name = btn.getAttribute('data-copy-name') || '';
        if (!name) return;
        function done(ok) {
          btn.classList.add(ok ? 'market-copy--done' : 'market-copy--fail');
          var prev = btn.getAttribute('title');
          btn.setAttribute('title', ok ? 'Copied!' : 'Copy failed');
          window.setTimeout(function () {
            btn.classList.remove('market-copy--done', 'market-copy--fail');
            btn.setAttribute('title', prev || 'Copy item name');
          }, 1400);
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(name).then(function () { done(true); },
                                                    function () { done(false); });
        } else {
          // Legacy fallback: hidden textarea + execCommand.
          try {
            var ta = document.createElement('textarea');
            ta.value = name; ta.setAttribute('readonly', '');
            ta.style.position = 'absolute'; ta.style.left = '-9999px';
            document.body.appendChild(ta); ta.select();
            done(document.execCommand('copy'));
            document.body.removeChild(ta);
          } catch (e) { done(false); }
        }
      });
    }
  }

  /* Reveal + wire the "Watch this item" control inside the freshly-rendered
   * drawer. POSTs a price-alert watch to /portal/watchlist/add (read-only on the
   * exchange — it only sets a notification threshold). */
  function wireWatchAdd(scope) {
    var box = scope.querySelector('[data-market-watch]');
    if (!box) return;
    box.hidden = false;
    var btn = box.querySelector('[data-market-watch-add]');
    var price = box.querySelector('[data-market-watch-price]');
    var msg = box.querySelector('.market-watch__msg');
    var tpl = box.getAttribute('data-market-watch-tpl') || '';
    if (!btn || !price || !tpl) return;

    function show(text, kind) {
      if (!msg) return;
      msg.textContent = text;
      msg.className = 'market-watch__msg' + (kind ? ' market-watch__msg--' + kind : '');
    }

    btn.addEventListener('click', function () {
      var val = (price.value || '').trim();
      if (!/^\d+$/.test(val) || parseInt(val, 10) <= 0) {
        show('Enter a whole number price above 0.', 'warn');
        return;
      }
      btn.setAttribute('disabled', 'disabled');
      btn.setAttribute('aria-busy', 'true');
      var body = new URLSearchParams();
      body.set('template_id', tpl);
      body.set('max_price', val);
      var headers = { 'Accept': 'application/json',
                      'Content-Type': 'application/x-www-form-urlencoded' };
      headers[CSRF_HEADER] = getCookie(CSRF_COOKIE);
      fetch('/portal/watchlist/add',
            { method: 'POST', credentials: 'same-origin', headers: headers, body: body.toString() })
        .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; },
                                                  function () { return { ok: r.ok, j: null }; }); })
        .then(function (res) {
          if (res.ok && res.j && res.j.ok) {
            show('Watching — you will be alerted at ' + res.j.max_price_display + ' S or lower.', 'ok');
            refreshWatches();
          } else {
            show((res.j && res.j.error) || 'Could not add that watch.', 'warn');
            btn.removeAttribute('disabled');
            btn.removeAttribute('aria-busy');
          }
        })
        .catch(function () {
          show('Could not add that watch. Please try again.', 'warn');
          btn.removeAttribute('disabled');
          btn.removeAttribute('aria-busy');
        });
    });
  }

  /* Reveal + wire the Buy controls inside the freshly-rendered drawer. Each Buy
   * button opens the confirm panel pinned to that listing's order_id + revision;
   * Confirm POSTs to /portal/market/buy. The buyer's controller_id is resolved
   * server-side from the session — the client never sends it. */
  function wireBuy(scope) {
    var ladder = scope.querySelector('.market-ladder[data-buy-tpl]');
    var panel = scope.querySelector('[data-market-buy]');
    if (!ladder || !panel) return;

    var tpl = ladder.getAttribute('data-buy-tpl') || '';
    var name = ladder.getAttribute('data-buy-name') || 'item';
    var bankEl = scope.querySelector('[data-buy-bank]');
    var bank = bankEl ? parseInt(bankEl.getAttribute('data-buy-bank'), 10) : null;

    var qtyInput = panel.querySelector('[data-buy-qty]');
    var unitEl = panel.querySelector('[data-buy-unit]');
    var totalEl = panel.querySelector('[data-buy-total]');
    var stackMaxEl = panel.querySelector('[data-buy-stackmax]');
    var itemNameEl = panel.querySelector('[data-buy-itemname]');
    var bankLine = panel.querySelector('[data-buy-bankline]');
    var bankShow = panel.querySelector('[data-buy-bankshow]');
    var confirmBtn = panel.querySelector('[data-buy-confirm]');
    var cancelBtn = panel.querySelector('[data-buy-cancel]');
    var msg = panel.querySelector('.market-buy__msg');

    var cur = { order: 0, rev: 0, price: 0, stack: 1 };

    function fmt(n) { return (n || 0).toLocaleString('en-US'); }

    function show(text, kind) {
      if (!msg) return;
      msg.textContent = text || '';
      msg.className = 'market-buy__msg' + (kind ? ' market-buy__msg--' + kind : '');
    }

    function clampQty() {
      var v = parseInt((qtyInput.value || '').trim(), 10);
      if (!v || v < 1) v = 1;
      if (v > cur.stack) v = cur.stack;
      qtyInput.value = v;
      return v;
    }

    function recompute() {
      var v = clampQty();
      var total = v * cur.price;
      if (totalEl) totalEl.textContent = fmt(total);
      if (confirmBtn) {
        var tooPoor = (bank !== null && !isNaN(bank) && total > bank);
        confirmBtn.disabled = !!tooPoor;
        show(tooPoor ? 'That is more than your banked Solari.' : '', tooPoor ? 'warn' : '');
      }
    }

    function openPanel(btn) {
      cur.order = parseInt(btn.getAttribute('data-buy-order'), 10) || 0;
      cur.rev = parseInt(btn.getAttribute('data-buy-rev'), 10) || 0;
      cur.price = parseInt(btn.getAttribute('data-buy-price'), 10) || 0;
      cur.stack = parseInt(btn.getAttribute('data-buy-stack'), 10) || 1;
      if (itemNameEl) itemNameEl.textContent = name;
      if (unitEl) unitEl.textContent = fmt(cur.price);
      if (stackMaxEl) stackMaxEl.textContent = fmt(cur.stack);
      if (qtyInput) { qtyInput.value = '1'; qtyInput.max = cur.stack; }
      if (bank !== null && !isNaN(bank) && bankLine && bankShow) {
        bankShow.textContent = fmt(bank);
        bankLine.hidden = false;
      }
      if (confirmBtn) { confirmBtn.disabled = false; confirmBtn.removeAttribute('aria-busy'); }
      show('', '');
      panel.hidden = false;
      recompute();
      panel.scrollIntoView({ block: 'nearest' });
      if (qtyInput) qtyInput.focus();
    }

    var buyBtns = ladder.querySelectorAll('.market-ladder__buy');
    for (var i = 0; i < buyBtns.length; i++) {
      buyBtns[i].addEventListener('click', function () { openPanel(this); });
    }
    if (qtyInput) qtyInput.addEventListener('input', recompute);
    if (cancelBtn) cancelBtn.addEventListener('click', function () { panel.hidden = true; });

    if (confirmBtn) {
      confirmBtn.addEventListener('click', function () {
        var v = clampQty();
        if (!cur.order || !cur.rev) {
          show('This listing changed. Refresh and try again.', 'warn');
          return;
        }
        confirmBtn.setAttribute('disabled', 'disabled');
        confirmBtn.setAttribute('aria-busy', 'true');
        show('Purchasing…', '');
        var body = new URLSearchParams();
        body.set('order_id', cur.order);
        body.set('revision', cur.rev);
        body.set('count', v);
        body.set('tpl', tpl);
        var headers = { 'Accept': 'text/html',
                        'Content-Type': 'application/x-www-form-urlencoded' };
        headers[CSRF_HEADER] = getCookie(CSRF_COOKIE);
        fetch('/portal/market/buy',
              { method: 'POST', credentials: 'same-origin', headers: headers, body: body.toString() })
          .then(function (r) { return r.text(); })
          .then(function (html) {
            panel.hidden = true;
            var holder = document.createElement('div');
            holder.innerHTML = html;
            wireIconFallbacks(holder);
            // Place the result above the ladder so it is the first thing seen.
            ladder.parentNode.insertBefore(holder, ladder);
          })
          .catch(function () {
            confirmBtn.removeAttribute('disabled');
            confirmBtn.removeAttribute('aria-busy');
            show('Purchase failed. Please try again.', 'warn');
          });
      });
    }
  }

  function setActiveTab(cat) {
    for (var i = 0; i < tabs.length; i++) {
      var on = tabs[i].getAttribute('data-market-cat') === cat;
      tabs[i].classList.toggle('market-tab--active', on);
      tabs[i].setAttribute('aria-selected', on ? 'true' : 'false');
    }
  }

  function setActiveSort(s) {
    for (var i = 0; i < sortBtns.length; i++) {
      var on = sortBtns[i].getAttribute('data-market-sort') === s;
      sortBtns[i].classList.toggle('market-sort__btn--active', on);
      sortBtns[i].setAttribute('aria-pressed', on ? 'true' : 'false');
    }
  }

  function setActiveKind(k) {
    for (var i = 0; i < kindBtns.length; i++) {
      var on = kindBtns[i].getAttribute('data-market-kind') === k;
      kindBtns[i].classList.toggle('market-sort__btn--active', on);
      kindBtns[i].setAttribute('aria-pressed', on ? 'true' : 'false');
    }
  }

  function searchUrl(q, sort, cat, page, kind) {
    return '/portal/market/search?q=' + encodeURIComponent(q) +
           '&sort=' + encodeURIComponent(sort) +
           '&category=' + encodeURIComponent(cat) +
           '&kind=' + encodeURIComponent(kind || 'all') +
           '&page=' + page;
  }

  /* Wire the Load-more button (page 1 render contains it when has_more). */
  function wireLoadMore(scope) {
    var btn = scope.querySelector('.market-loadmore');
    if (btn) btn.addEventListener('click', function () { loadMore(btn); });
  }

  function loadMore(btn) {
    var next = parseInt(btn.getAttribute('data-next-page'), 10) || 2;
    btn.setAttribute('disabled', 'disabled');
    btn.setAttribute('aria-busy', 'true');
    fetch(searchUrl(curQuery, curSort, curCat, next, curKind),
          { credentials: 'same-origin', headers: { 'Accept': 'text/html' } })
      .then(function (r) { if (!r.ok) throw new Error('http_' + r.status); return r.text(); })
      .then(function (html) {
        var list = document.getElementById('market-list');
        var tmp = document.createElement('div');
        tmp.innerHTML = html;
        var meta = tmp.querySelector('.market-loadmore-meta');
        var hasMore = meta && meta.getAttribute('data-has-more') === 'true';
        var nextPage = meta ? meta.getAttribute('data-next-page') : null;
        if (meta) meta.parentNode.removeChild(meta);
        if (list) {
          wireIconFallbacks(tmp);
          while (tmp.firstChild) { list.appendChild(tmp.firstChild); }
        }
        wireRows(list || results);
        if (hasMore) {
          btn.setAttribute('data-next-page', nextPage || (next + 1));
          btn.removeAttribute('disabled');
          btn.removeAttribute('aria-busy');
        } else if (btn.parentNode) {
          btn.parentNode.removeChild(btn);
        }
      })
      .catch(function () {
        btn.removeAttribute('disabled');
        btn.removeAttribute('aria-busy');
        btn.textContent = 'Load more failed — tap to retry';
      });
  }

  function runSearch(q, sort, cat, kind) {
    curQuery = q; curSort = sort; curCat = cat; curKind = kind || 'all';
    var req = ++activeReq;
    results.innerHTML = '<div class="cont-drawer__loading">Loading the exchange…</div>';
    fetch(searchUrl(q, sort, cat, 1, curKind),
          { credentials: 'same-origin', headers: { 'Accept': 'text/html' } })
      .then(function (r) { if (!r.ok) throw new Error('http_' + r.status); return r.text(); })
      .then(function (html) {
        if (req !== activeReq) return;
        results.innerHTML = html;
        wireIconFallbacks(results);
        wireRows(results);
        wireLoadMore(results);
      })
      .catch(function () {
        if (req !== activeReq) return;
        results.innerHTML = '<div class="portal-msg portal-msg--warning" role="status">' +
          'Search failed. Please try again.</div>';
      });
  }

  /* ---- item price-ladder drawer ---- */
  var drawer = document.getElementById('market-drawer');
  var drawerOverlay = document.getElementById('market-drawer-overlay');
  var drawerBody = document.getElementById('market-drawer-body');
  var drawerTitle = document.getElementById('market-drawer-title');
  var drawerClose = document.getElementById('market-drawer-close');
  var lastFocus = null;

  function openDrawer(tpl, name) {
    if (!drawer) return;
    lastFocus = document.activeElement;
    drawerTitle.textContent = name || 'Item';
    drawerBody.innerHTML = '<div class="cont-drawer__loading">Loading listings…</div>';
    drawerOverlay.hidden = false;
    drawer.hidden = false;
    window.requestAnimationFrame(function () {
      drawer.classList.add('ls-drawer--open');
      drawerOverlay.classList.add('ls-drawer-overlay--open');
    });
    if (drawerClose) drawerClose.focus();
    fetch('/portal/market/item?tpl=' + encodeURIComponent(tpl),
          { credentials: 'same-origin', headers: { 'Accept': 'text/html' } })
      .then(function (r) { if (!r.ok) throw new Error('http_' + r.status); return r.text(); })
      .then(function (html) {
        drawerBody.innerHTML = html;
        wireIconFallbacks(drawerBody);
        wireCopyName(drawerBody);
        wireWatchAdd(drawerBody);
        wireBuy(drawerBody);
      })
      .catch(function () {
        drawerBody.innerHTML = '<div class="portal-msg portal-msg--warning" role="status">' +
          'Could not load listings. Please try again.</div>';
      });
  }

  function closeDrawer() {
    if (!drawer || drawer.hidden) return;
    drawer.classList.remove('ls-drawer--open');
    drawerOverlay.classList.remove('ls-drawer-overlay--open');
    window.setTimeout(function () {
      drawer.hidden = true;
      drawerOverlay.hidden = true;
      drawerBody.innerHTML = '';
    }, 200);
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }

  function wireRows(scope) {
    var rows = scope.querySelectorAll('.market-row--button');
    for (var i = 0; i < rows.length; i++) {
      rows[i].addEventListener('click', function () {
        var tpl = this.getAttribute('data-market-tpl');
        var nm = this.querySelector('.market-row__name');
        if (tpl) openDrawer(tpl, nm ? nm.textContent : 'Item');
      });
    }
  }

  if (drawerClose) drawerClose.addEventListener('click', closeDrawer);
  if (drawerOverlay) drawerOverlay.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeDrawer();
  });

  /* ---- category tabs + kind filter + sort + search wiring ---- */
  for (var i = 0; i < tabs.length; i++) {
    tabs[i].addEventListener('click', function () {
      var cat = this.getAttribute('data-market-cat') || 'all';
      setActiveTab(cat);
      runSearch(curQuery, curSort, cat, curKind);
    });
  }

  for (var k = 0; k < kindBtns.length; k++) {
    kindBtns[k].addEventListener('click', function () {
      var kind = this.getAttribute('data-market-kind') || 'all';
      setActiveKind(kind);
      runSearch(curQuery, curSort, curCat, kind);
    });
  }

  for (var s = 0; s < sortBtns.length; s++) {
    sortBtns[s].addEventListener('click', function () {
      var sort = this.getAttribute('data-market-sort') || 'active';
      setActiveSort(sort);
      runSearch(curQuery, sort, curCat, curKind);
    });
  }

  if (input) {
    input.addEventListener('input', function () {
      var q = input.value.trim();
      if (searchTimer) window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(function () { runSearch(q, curSort, curCat, curKind); }, 250);
    });
  }

  /* Browse-all on load so the page is never empty. */
  setActiveTab('all');
  setActiveKind('all');
  setActiveSort('active');
  runSearch('', 'active', 'all', 'all');

  /* Wire the price-alert "add an alert" search box (server-rendered on load). */
  wireWatchSearch(document);
})();
