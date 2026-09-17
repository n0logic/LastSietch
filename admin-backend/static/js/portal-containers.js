/* Last Sietch portal — storage container browser.
 * Hover a container tile -> tooltip with a first-5-items peek ("click for details").
 * Click a tile -> slide-in drawer that fetches the container's items fragment
 * (icon + name + qty/quality/durability), with pagination + on-page search.
 * Top-of-page "Find an item" box -> cross-container search.
 * Vanilla, no deps. Fetched fragments are our own server-rendered HTML (Jinja-
 * escaped), injected via innerHTML. Portal CSP is script-src 'self', so the image
 * fallback is wired here in JS, not via an inline onerror attribute.
 * A shared page-1 cache backs both the hover peek and the drawer open.
 */
(function () {
  'use strict';

  var grid = document.querySelector('.cont-grid');
  var drawer = document.getElementById('cont-drawer');
  var overlay = document.getElementById('cont-drawer-overlay');
  var drawerTitle = document.getElementById('cont-drawer-title');
  var drawerBody = document.getElementById('cont-drawer-body');
  var drawerClose = document.getElementById('cont-drawer-close');
  var tooltip = document.getElementById('cont-tooltip');
  if (!grid || !drawer) return;

  var FALLBACK = '/admin/static/img/dune-icons/T_UI_IconItemUnknownS_D.png';
  var lastFocused = null;
  var currentId = null;
  var page1Cache = {};      // containerId -> page-1 fragment HTML (shared: hover + drawer)
  var hoverTimer = null;
  var hoverId = null;

  function tileFor(el) { return el.closest ? el.closest('.cont-tile') : null; }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
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

  function wireSearch(scope) {
    var input = scope.querySelector('.cont-items__search');
    var emptyMsg = scope.querySelector('.cont-items__empty-filter');
    if (!input) return;
    input.addEventListener('input', function () {
      var needle = (input.value || '').trim().toLowerCase();
      var rows = scope.querySelectorAll('.cont-item');
      var shown = 0;
      for (var i = 0; i < rows.length; i++) {
        var hay = rows[i].getAttribute('data-filter') || '';
        var match = !needle || hay.indexOf(needle) !== -1;
        rows[i].hidden = !match;
        if (match) shown++;
      }
      if (emptyMsg) emptyMsg.hidden = shown !== 0;
    });
  }

  function wirePager(scope) {
    var btns = scope.querySelectorAll('[data-cont-page]');
    for (var i = 0; i < btns.length; i++) {
      btns[i].addEventListener('click', function () {
        if (this.disabled) return;
        var page = parseInt(this.getAttribute('data-cont-page'), 10);
        var id = this.getAttribute('data-cont-id') || currentId;
        if (!isNaN(page) && id) loadItems(id, page);
      });
    }
  }

  function setBody(html) {
    drawerBody.innerHTML = html;
    wireIconFallbacks(drawerBody);
    wireSearch(drawerBody);
    wirePager(drawerBody);
    wireSell(drawerBody);
  }

  /* Fetch page 1 once; cache for both the hover peek and the drawer. */
  function fetchPage1(id) {
    if (page1Cache[id]) return Promise.resolve(page1Cache[id]);
    return fetch('/portal/containers/' + encodeURIComponent(id) + '/items?page=1',
                 { credentials: 'same-origin', headers: { 'Accept': 'text/html' } })
      .then(function (r) {
        if (r.status === 404) throw new Error('not_found');
        if (!r.ok) throw new Error('http_' + r.status);
        return r.text();
      })
      .then(function (html) { page1Cache[id] = html; return html; });
  }

  /* ---- hover peek tooltip ---- */
  function firstItems(html, n) {
    var tmp = document.createElement('div');
    tmp.innerHTML = html;
    var rows = tmp.querySelectorAll('.cont-item');
    var out = [];
    for (var i = 0; i < rows.length && out.length < n; i++) {
      var nm = rows[i].querySelector('.cont-item__name');
      var qty = rows[i].querySelector('.cont-item__qty');
      out.push({ name: nm ? nm.textContent : '', qty: qty ? qty.textContent : '' });
    }
    return out;
  }

  function buildTooltip(tile, items, count) {
    if (!tooltip) return;
    tooltip.textContent = '';
    var type = tile.getAttribute('data-type') || 'Container';
    var name = tile.getAttribute('data-name') || '';
    tooltip.appendChild(el('div', 'ls-tooltip__name', name || type));
    if (name) tooltip.appendChild(el('div', 'cont-tip__type', type));
    if (!count) {
      tooltip.appendChild(el('div', 'ls-tooltip__empty', 'Empty'));
    } else {
      var ul = el('ul', 'cont-tip__items');
      for (var i = 0; i < items.length; i++) {
        var li = el('li', 'cont-tip__item');
        li.appendChild(el('span', 'cont-tip__item-name', items[i].name));
        if (items[i].qty) li.appendChild(el('span', 'cont-tip__item-qty', items[i].qty));
        ul.appendChild(li);
      }
      tooltip.appendChild(ul);
      if (count > items.length) {
        tooltip.appendChild(el('div', 'cont-tip__more', '+' + (count - items.length) + ' more'));
      }
      tooltip.appendChild(el('div', 'ls-tooltip__hint', 'Click for full details'));
    }
    tooltip.hidden = false;
    positionTooltip(tile);
  }

  function positionTooltip(tile) {
    var r = tile.getBoundingClientRect();
    var tt = tooltip.getBoundingClientRect();
    var m = 8;
    var left = r.left + (r.width / 2) - (tt.width / 2);
    left = Math.max(m, Math.min(left, window.innerWidth - tt.width - m));
    var top = r.bottom + m;
    if (top + tt.height > window.innerHeight - m) top = r.top - tt.height - m;
    tooltip.style.left = Math.round(left) + 'px';
    tooltip.style.top = Math.round(Math.max(m, top)) + 'px';
  }

  function hideTooltip() { if (tooltip) tooltip.hidden = true; }

  function showHoverFor(tile) {
    var id = tile.getAttribute('data-container-id');
    if (!id || !tooltip) return;
    var count = parseInt(tile.getAttribute('data-count'), 10) || 0;
    if (!count) { buildTooltip(tile, [], 0); return; }
    fetchPage1(id).then(function (html) {
      if (hoverId !== id) return;   // pointer moved on before fetch resolved
      buildTooltip(tile, firstItems(html, 5), count);
    }).catch(function () {});
  }

  /* ---- drawer ---- */
  function loadItems(id, page) {
    currentId = id;
    drawerBody.scrollTop = 0;
    if (page === 1 && page1Cache[id]) { setBody(page1Cache[id]); return; }
    setBody('<div class="cont-drawer__loading">Loading…</div>');
    var url = '/portal/containers/' + encodeURIComponent(id) + '/items?page=' + (page || 1);
    fetch(url, { credentials: 'same-origin', headers: { 'Accept': 'text/html' } })
      .then(function (r) {
        if (r.status === 404) throw new Error('not_found');
        if (!r.ok) throw new Error('http_' + r.status);
        return r.text();
      })
      .then(function (html) {
        if (page === 1) page1Cache[id] = html;
        setBody(html);
      })
      .catch(function (err) {
        var msg = err && err.message === 'not_found'
          ? 'This container is no longer available.'
          : "Couldn't load this container right now. Please try again.";
        drawerBody.innerHTML = '';
        var p = el('div', 'portal-msg portal-msg--warning', msg);
        p.setAttribute('role', 'status');
        drawerBody.appendChild(p);
      });
  }

  function openDrawer(tile) {
    var id = tile.getAttribute('data-container-id');
    if (!id) return;
    hideTooltip();
    var name = tile.getAttribute('data-name') || '';
    var type = tile.getAttribute('data-type') || 'Container';
    drawerTitle.textContent = name || type;
    if (name) drawerTitle.title = type;
    lastFocused = tile;
    overlay.hidden = false;
    drawer.hidden = false;
    window.requestAnimationFrame(function () {
      drawer.classList.add('ls-drawer--open');
      overlay.classList.add('ls-drawer-overlay--open');
    });
    if (drawerClose) drawerClose.focus();
    loadItems(id, 1);
  }

  function closeDrawer() {
    if (!drawer || drawer.hidden) return;
    drawer.classList.remove('ls-drawer--open');
    overlay.classList.remove('ls-drawer-overlay--open');
    window.setTimeout(function () {
      drawer.hidden = true;
      overlay.hidden = true;
      drawerBody.innerHTML = '';
    }, 200);
    if (lastFocused && lastFocused.focus) lastFocused.focus();
  }

  grid.addEventListener('click', function (e) {
    var tile = tileFor(e.target);
    if (tile) openDrawer(tile);
  });
  grid.addEventListener('mouseover', function (e) {
    var tile = tileFor(e.target);
    if (!tile) return;
    hoverId = tile.getAttribute('data-container-id');
    if (hoverTimer) window.clearTimeout(hoverTimer);
    hoverTimer = window.setTimeout(function () { showHoverFor(tile); }, 300);
  });
  grid.addEventListener('mouseout', function (e) {
    var to = e.relatedTarget;
    if (!to || !to.closest || !to.closest('.cont-tile')) {
      hoverId = null;
      if (hoverTimer) window.clearTimeout(hoverTimer);
      hideTooltip();
    }
  });
  window.addEventListener('scroll', hideTooltip, true);

  if (drawerClose) drawerClose.addEventListener('click', closeDrawer);
  if (overlay) overlay.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    // The sell modal sits above the drawer; let it consume Escape first.
    if (sellModal && !sellModal.hidden) return;
    closeDrawer();
  });

  /* ---- cross-container item search ---- */
  var searchInput = document.getElementById('cont-search-input');
  var searchResults = document.getElementById('cont-search-results');
  var searchTimer = null;

  function runSearch(q) {
    searchResults.innerHTML = '<div class="cont-drawer__loading">Searching…</div>';
    fetch('/portal/containers/search?q=' + encodeURIComponent(q),
          { credentials: 'same-origin', headers: { 'Accept': 'text/html' } })
      .then(function (r) { if (!r.ok) throw new Error('http_' + r.status); return r.text(); })
      .then(function (html) { searchResults.innerHTML = html; wireIconFallbacks(searchResults); })
      .catch(function () {
        searchResults.innerHTML = '';
        var p = el('div', 'portal-msg portal-msg--warning', 'Search failed. Please try again.');
        p.setAttribute('role', 'status');
        searchResults.appendChild(p);
      });
  }

  if (searchInput && searchResults) {
    searchInput.addEventListener('input', function () {
      var q = searchInput.value.trim();
      if (searchTimer) window.clearTimeout(searchTimer);
      if (q.length < 2) { searchResults.innerHTML = ''; return; }
      searchTimer = window.setTimeout(function () { runSearch(q); }, 250);
    });
  }

  /* ---- list-on-exchange (sell) modal ----
   * Opened from a storage item's "List" button. Quantity (1..stack), price per
   * unit, duration (1/3/7/14d) -> a LIVE exchange fee using the CONFIRMED formula
   * fee = round(0.01*price*(days+1)) + 20*days. Confirm POSTs to
   * /portal/market/sell with CSRF; the seller's controller_id + the owned-item
   * check are enforced server-side, so the client only carries item_id +
   * container_id + the listing terms. The fee is display-only (the writer debits
   * the authoritative amount it computes). */
  var CSRF_COOKIE = 'ls_portal_csrf';
  var CSRF_HEADER = 'X-Portal-CSRF-Token';

  function getCookie(name) {
    var m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : '';
  }

  function fmt(n) { return (n || 0).toLocaleString('en-US'); }

  var sellModal = document.getElementById('sell-modal');
  var sellOverlay = document.getElementById('sell-modal-overlay');
  var sellClose = document.getElementById('sell-modal-close');
  var sellCancel = document.getElementById('sell-cancel');
  var sellConfirm = document.getElementById('sell-confirm');
  var sellQty = sellModal && sellModal.querySelector('[data-sell-qty]');
  var sellPrice = sellModal && sellModal.querySelector('[data-sell-price]');
  var sellTotalEl = sellModal && sellModal.querySelector('[data-sell-total]');
  var sellFeeEl = sellModal && sellModal.querySelector('[data-sell-fee]');
  var sellNameEl = sellModal && sellModal.querySelector('[data-sell-itemname]');
  var sellStackEls = sellModal
    ? sellModal.querySelectorAll('[data-sell-stackmax], [data-sell-stackmax2]') : [];
  var sellDurBtns = sellModal ? sellModal.querySelectorAll('[data-sell-dur]') : [];
  var sellMsg = document.getElementById('sell-msg');
  var sellResult = document.getElementById('sell-result');
  var sellLastFocus = null;

  var sellCur = { item: 0, container: '', tpl: '', name: 'item', stack: 1, days: 7 };

  function sellFee(price, days) {
    if (!price || price < 1) return 0;
    return Math.round(0.01 * price * (days + 1)) + 20 * days;
  }

  // Read the clamped quantity WITHOUT rewriting the field, so typing a value
  // above the stack doesn't yank the input back to the max mid-keystroke.
  function sellQtyVal() {
    var v = parseInt((sellQty.value || '').trim(), 10);
    if (!v || v < 1) v = 1;
    if (v > sellCur.stack) v = sellCur.stack;
    return v;
  }

  // Snap the field itself to the clamped value (blur/submit only).
  function sellClampQtyField() {
    sellQty.value = sellQtyVal();
  }

  function sellPriceVal() {
    var v = parseInt((sellPrice.value || '').replace(/,/g, '').trim(), 10);
    return (!v || v < 1) ? 0 : v;
  }

  function setSellMsg(text, kind) {
    if (!sellMsg) return;
    sellMsg.textContent = text || '';
    sellMsg.className = 'sell-modal__msg' + (kind ? ' sell-modal__msg--' + kind : '');
  }

  function sellRecompute() {
    var v = sellQtyVal();
    var price = sellPriceVal();
    if (sellTotalEl) sellTotalEl.textContent = fmt(v * price);
    if (sellFeeEl) sellFeeEl.textContent = fmt(sellFee(price, sellCur.days));
    if (sellConfirm) sellConfirm.disabled = price < 1;
  }

  function setSellDuration(days) {
    sellCur.days = days;
    for (var i = 0; i < sellDurBtns.length; i++) {
      var on = parseInt(sellDurBtns[i].getAttribute('data-sell-dur'), 10) === days;
      sellDurBtns[i].classList.toggle('sell-modal__dur--active', on);
      sellDurBtns[i].setAttribute('aria-pressed', on ? 'true' : 'false');
    }
    sellRecompute();
  }

  function openSell(btn) {
    if (!sellModal) return;
    sellCur.item = parseInt(btn.getAttribute('data-sell-item'), 10) || 0;
    sellCur.container = btn.getAttribute('data-sell-container') || '';
    sellCur.tpl = btn.getAttribute('data-sell-tpl') || '';
    sellCur.name = btn.getAttribute('data-sell-name') || 'item';
    sellCur.stack = parseInt(btn.getAttribute('data-sell-stack'), 10) || 1;
    if (!sellCur.item || !sellCur.container) return;
    if (sellNameEl) sellNameEl.textContent = sellCur.name;
    for (var i = 0; i < sellStackEls.length; i++) sellStackEls[i].textContent = fmt(sellCur.stack);
    if (sellQty) { sellQty.value = '1'; sellQty.max = sellCur.stack; }
    if (sellPrice) sellPrice.value = '1000';   // never blank/zero; seller adjusts
    if (sellResult) sellResult.innerHTML = '';
    if (sellConfirm) sellConfirm.removeAttribute('aria-busy');
    setSellMsg('', '');
    setSellDuration(7);
    sellLastFocus = btn;
    sellOverlay.hidden = false;
    sellModal.hidden = false;
    window.requestAnimationFrame(function () {
      sellModal.classList.add('ls-modal--open');
      sellOverlay.classList.add('ls-modal-overlay--open');
    });
    if (sellPrice) sellPrice.focus();
  }

  function closeSell() {
    if (!sellModal || sellModal.hidden) return;
    sellModal.classList.remove('ls-modal--open');
    sellOverlay.classList.remove('ls-modal-overlay--open');
    window.setTimeout(function () {
      sellModal.hidden = true;
      sellOverlay.hidden = true;
    }, 180);
    if (sellLastFocus && sellLastFocus.focus) sellLastFocus.focus();
  }

  function submitSell() {
    sellClampQtyField();
    var v = sellQtyVal();
    var price = sellPriceVal();
    if (price < 1) { setSellMsg('Enter a price of 1 Solari or more.', 'warn'); return; }
    if (!sellCur.item || !sellCur.container) {
      setSellMsg('That item is no longer available. Refresh and try again.', 'warn');
      return;
    }
    sellConfirm.setAttribute('disabled', 'disabled');
    sellConfirm.setAttribute('aria-busy', 'true');
    setSellMsg('Listing…', '');
    var body = new URLSearchParams();
    body.set('item_id', sellCur.item);
    body.set('container_id', sellCur.container);
    body.set('count', v);
    body.set('price', price);
    body.set('duration_days', sellCur.days);
    body.set('tpl', sellCur.tpl);
    var headers = { 'Accept': 'text/html', 'Content-Type': 'application/x-www-form-urlencoded' };
    headers[CSRF_HEADER] = getCookie(CSRF_COOKIE);
    fetch('/portal/market/sell',
          { method: 'POST', credentials: 'same-origin', headers: headers, body: body.toString() })
      .then(function (r) { return r.text(); })
      .then(function (html) {
        setSellMsg('', '');
        if (sellResult) sellResult.innerHTML = html;
        // A listing moves the item to escrow, so the cached storage pages are stale.
        page1Cache = {};
        var okEl = sellResult && sellResult.querySelector('.market-buy-result--ok');
        if (okEl) {
          // Listed — keep the result visible and lock the form against a double list.
          sellConfirm.setAttribute('disabled', 'disabled');
          // Re-render the open container so the now-reduced stack is reflected
          // immediately (cache already cleared above), avoiding a stale re-list.
          if (currentId) loadItems(currentId, 1);
        } else {
          sellConfirm.removeAttribute('disabled');
        }
        sellConfirm.removeAttribute('aria-busy');
      })
      .catch(function () {
        sellConfirm.removeAttribute('disabled');
        sellConfirm.removeAttribute('aria-busy');
        setSellMsg('Listing failed. Please try again.', 'warn');
      });
  }

  if (sellModal) {
    if (sellQty) {
      sellQty.addEventListener('input', sellRecompute);
      sellQty.addEventListener('blur', sellClampQtyField);
    }
    if (sellPrice) sellPrice.addEventListener('input', sellRecompute);
    for (var sd = 0; sd < sellDurBtns.length; sd++) {
      sellDurBtns[sd].addEventListener('click', function () {
        setSellDuration(parseInt(this.getAttribute('data-sell-dur'), 10) || 7);
      });
    }
    if (sellClose) sellClose.addEventListener('click', closeSell);
    if (sellCancel) sellCancel.addEventListener('click', closeSell);
    if (sellOverlay) sellOverlay.addEventListener('click', closeSell);
    if (sellConfirm) sellConfirm.addEventListener('click', submitSell);
  }

  function wireSell(scope) {
    if (!sellModal) return;
    var btns = scope.querySelectorAll('.cont-item__sell');
    for (var i = 0; i < btns.length; i++) {
      btns[i].addEventListener('click', function (e) {
        e.stopPropagation();
        openSell(this);
      });
    }
  }
})();
