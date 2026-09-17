/* Last Sietch portal — Storage Manager (Phase 1).
 * LEFT: CHOAM bank Solari transfer control (Withdraw Credit->Coin, Deposit
 * Coin->Credit, Deposit-all sweep). MIDDLE: owned-container list. RIGHT: the
 * selected container's slot grid (read-only this phase; no drag-drop yet).
 * Writes are OFFLINE-only — the server hard-gates online_status, and the page
 * renders the controls disabled while online; this script also fails closed.
 * Vanilla, no deps. Fetched fragments are our own server-rendered (Jinja-escaped)
 * HTML; CSP is script-src 'self', so the icon fallback is wired here, not inline.
 */
(function () {
  'use strict';

  var layout = document.querySelector('.storage-layout');
  if (!layout) return;

  var CSRF_COOKIE = 'ls_portal_csrf';
  var CSRF_HEADER = 'X-Portal-CSRF-Token';
  var FALLBACK = '/admin/static/img/dune-icons/T_UI_IconItemUnknownS_D.png';

  var canWrite = layout.getAttribute('data-can-write') === '1';
  var withdrawCap = parseInt(layout.getAttribute('data-withdraw-cap'), 10) || 100000;

  function getCookie(name) {
    var m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : '';
  }
  function csrfHeaders() {
    var h = { 'Accept': 'text/html', 'Content-Type': 'application/x-www-form-urlencoded' };
    h[CSRF_HEADER] = getCookie(CSRF_COOKIE);
    return h;
  }
  function resultOk(html) {
    return html && html.indexOf('market-buy-result--ok') !== -1;
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

  /* -------------------- transfer control -------------------- */
  var amountEl = layout.querySelector('[data-xfer-amount]');
  var msgEl = document.getElementById('xfer-msg');
  var resultEl = document.getElementById('xfer-result');
  var xferBtns = layout.querySelectorAll('[data-xfer]');

  function note(text, kind) {
    if (!msgEl) return;
    msgEl.textContent = text || '';
    msgEl.className = 'storage-xfer__msg' + (kind ? ' storage-xfer__msg--' + kind : '');
  }
  function amountVal() {
    if (!amountEl) return 0;
    var v = parseInt((amountEl.value || '').replace(/,/g, '').trim(), 10);
    return (!v || v < 1) ? 0 : v;
  }
  function setBusy(busy) {
    for (var i = 0; i < xferBtns.length; i++) {
      if (busy) { xferBtns[i].setAttribute('disabled', 'disabled'); }
      else if (canWrite) { xferBtns[i].removeAttribute('disabled'); }
    }
  }

  function submitTransfer(action) {
    if (!canWrite) {
      note('Log out of the game first — storage edits only apply while offline.', 'warn');
      return;
    }
    var body = new URLSearchParams();
    var url;
    if (action === 'withdraw') {
      var amt = amountVal();
      if (amt < 1) { note('Enter a whole number of Solari above 0.', 'warn'); return; }
      if (amt > withdrawCap) {
        note('Withdraw caps at ' + withdrawCap.toLocaleString('en-US') + ' Solari per transfer.', 'warn');
        return;
      }
      url = '/portal/storage/withdraw';
      body.set('amount', amt);
    } else if (action === 'deposit') {
      var dep = amountVal();
      if (dep < 1) { note('Enter a whole number of Solari above 0.', 'warn'); return; }
      url = '/portal/storage/deposit';
      body.set('mode', 'amount');
      body.set('amount', dep);
    } else if (action === 'sweep') {
      if (!window.confirm('Deposit every Solari Coin across your storage to your bank?')) return;
      url = '/portal/storage/deposit';
      body.set('mode', 'sweep');
    } else {
      return;
    }

    setBusy(true);
    note('Working…', '');
    fetch(url, { method: 'POST', credentials: 'same-origin', headers: csrfHeaders(),
                 body: body.toString() })
      .then(function (r) { return r.text(); })
      .then(function (html) {
        if (resultEl) resultEl.innerHTML = html;
        var ok = resultOk(html);
        note('', '');
        if (ok) {
          note('Done — refreshing balance…', 'ok');
          window.setTimeout(function () { window.location.reload(); }, 1400);
        } else {
          setBusy(false);
        }
      })
      .catch(function () {
        note('That transfer could not be completed. Please try again.', 'warn');
        setBusy(false);
      });
  }

  for (var b = 0; b < xferBtns.length; b++) {
    xferBtns[b].addEventListener('click', function () {
      submitTransfer(this.getAttribute('data-xfer'));
    });
  }

  /* -------------------- container selection (RIGHT panel) -------------------- */
  var detail = document.getElementById('storage-detail');
  var detailTitle = document.getElementById('storage-detail-title');
  var contBtns = layout.querySelectorAll('[data-storage-container]');
  var itemsCache = {};   // containerId -> fragment HTML
  var currentCid = null;

  function setActiveTile(btn) {
    for (var i = 0; i < contBtns.length; i++) {
      contBtns[i].classList.toggle('cont-tile--selected', contBtns[i] === btn);
    }
  }
  function renderDetail(html, onRendered) {
    if (!detail) return;
    detail.innerHTML = html;
    wireIconFallbacks(detail);
    if (onRendered) window.requestAnimationFrame(function () { onRendered(detail); });
  }
  function loadContainer(btn, onRendered) {
    var cid = btn.getAttribute('data-storage-container');
    if (!cid) return;
    currentCid = cid;
    setActiveTile(btn);
    repairOnSelect(cid);
    if (detailTitle) detailTitle.textContent = btn.getAttribute('data-type') || 'Container';
    if (itemsCache[cid]) { renderDetail(itemsCache[cid], onRendered); return; }
    if (detail) detail.innerHTML = '<p class="storage-grid__note">Loading…</p>';
    fetch('/portal/storage/' + encodeURIComponent(cid) + '/items',
          { credentials: 'same-origin', headers: { 'Accept': 'text/html' } })
      .then(function (r) {
        if (r.status === 404) throw new Error('not_owned');
        return r.text();
      })
      .then(function (html) {
        if (currentCid !== cid) return;   // a newer click won the race
        itemsCache[cid] = html;
        renderDetail(html, onRendered);
      })
      .catch(function () {
        if (currentCid !== cid) return;
        renderDetail('<div class="portal-msg portal-msg--warning" role="status">' +
          'Couldn’t load that container right now. Please try again.</div>');
      });
  }
  function findContTile(cid) {
    for (var i = 0; i < contBtns.length; i++) {
      if (contBtns[i].getAttribute('data-storage-container') === String(cid)) return contBtns[i];
    }
    return null;
  }

  for (var c = 0; c < contBtns.length; c++) {
    contBtns[c].addEventListener('click', function () { loadContainer(this); });
  }

  /* -------------------- container strip carousel (5-up + arrows) -------------------- */
  (function () {
    var vp = layout.querySelector('[data-conts-viewport]');
    var prev = layout.querySelector('[data-conts-prev]');
    var next = layout.querySelector('[data-conts-next]');
    if (!vp || !prev || !next) return;

    function maxScroll() { return Math.max(0, vp.scrollWidth - vp.clientWidth); }
    function update() {
      var overflow = maxScroll() > 2;
      // No overflow (<=5 containers): hide both arrows entirely.
      prev.hidden = !overflow;
      next.hidden = !overflow;
      if (!overflow) return;
      prev.disabled = vp.scrollLeft <= 1;
      next.disabled = vp.scrollLeft >= maxScroll() - 1;
    }
    function page(dir) {
      // Scroll roughly one screenful (5 tiles), keeping a tile of overlap.
      var step = Math.max(120, vp.clientWidth * 0.85);
      vp.scrollBy({ left: dir * step, behavior: 'smooth' });
    }
    prev.addEventListener('click', function () { page(-1); });
    next.addEventListener('click', function () { page(1); });
    vp.addEventListener('scroll', update, { passive: true });
    window.addEventListener('resize', update);
    update();
  })();

  /* -------------------- cross-container search (Find an item) -------------------- */
  var contsViewport = layout.querySelector('[data-conts-viewport]');

  function highlightSlot(scope, tpl) {
    if (!scope || !tpl) return;
    var slot = scope.querySelector('.storage-slot--filled[data-template-id="' + tpl + '"]');
    if (!slot) return;
    slot.scrollIntoView({ block: 'nearest' });
    slot.classList.add('storage-slot--highlight');
    window.setTimeout(function () { slot.classList.remove('storage-slot--highlight'); }, 1600);
  }

  function selectFromSearch(cid, tpl) {
    var tile = findContTile(cid);
    if (!tile) return;
    if (contsViewport) {
      contsViewport.scrollTo({
        left: tile.offsetLeft - contsViewport.clientWidth / 2,
        behavior: 'smooth'
      });
    }
    loadContainer(tile, function (host) { highlightSlot(host, tpl); });
  }

  (function () {
    var searchInput = document.getElementById('cont-search-input');
    var searchResults = document.getElementById('cont-search-results');
    if (!searchInput || !searchResults) return;
    var searchTimer = null;

    function runSearch(q) {
      searchResults.innerHTML = '<div class="cont-drawer__loading">Searching…</div>';
      fetch('/portal/containers/search?q=' + encodeURIComponent(q),
            { credentials: 'same-origin', headers: { 'Accept': 'text/html' } })
        .then(function (r) { if (!r.ok) throw new Error('http_' + r.status); return r.text(); })
        .then(function (html) {
          searchResults.innerHTML = html;
          wireIconFallbacks(searchResults);
        })
        .catch(function () {
          searchResults.innerHTML =
            '<div class="portal-msg portal-msg--warning" role="status">' +
            'Search failed. Please try again.</div>';
        });
    }

    searchInput.addEventListener('input', function () {
      var q = searchInput.value.trim();
      if (searchTimer) window.clearTimeout(searchTimer);
      if (q.length < 2) { searchResults.innerHTML = ''; return; }
      searchTimer = window.setTimeout(function () { runSearch(q); }, 250);
    });

    // Click a result row's container box -> open that container + highlight the item.
    searchResults.addEventListener('click', function (e) {
      var box = e.target.closest && e.target.closest('.cont-search__box[data-container-id]');
      if (!box) return;
      var cid = box.getAttribute('data-container-id');
      var group = box.closest('.cont-search__item');
      var tpl = group ? group.getAttribute('data-template-id') : '';
      if (!cid) return;
      selectFromSearch(cid, tpl);
    });
  })();

  /* -------------------- sell modal (List on Exchange) --------------------
   * Opened from a right-panel slot's popover. Quantity (1..stack), price per
   * unit, duration (1/3/7/14d) -> a LIVE exchange fee using the CONFIRMED formula
   * fee = round(0.01*price*(days+1)) + 20*days. Confirm POSTs to /portal/market/sell
   * with CSRF; the seller's controller_id + owned-item check are server-enforced. */
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
  function sellQtyVal() {
    var v = parseInt((sellQty.value || '').trim(), 10);
    if (!v || v < 1) v = 1;
    if (v > sellCur.stack) v = sellCur.stack;
    return v;
  }
  function sellClampQtyField() { sellQty.value = sellQtyVal(); }
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

  // Open the sell modal from a slot's data-* (item/tpl/name/stack) + container_id.
  function openSell(ctx, focusReturn) {
    if (!sellModal) return;
    sellCur.item = parseInt(ctx.item, 10) || 0;
    sellCur.container = ctx.container || '';
    sellCur.tpl = ctx.tpl || '';
    sellCur.name = ctx.name || 'item';
    sellCur.stack = parseInt(ctx.stack, 10) || 1;
    if (!sellCur.item || !sellCur.container) return;
    if (sellNameEl) sellNameEl.textContent = sellCur.name;
    for (var i = 0; i < sellStackEls.length; i++) sellStackEls[i].textContent = fmt(sellCur.stack);
    if (sellQty) { sellQty.value = '1'; sellQty.max = sellCur.stack; }
    if (sellPrice) sellPrice.value = '1000';
    if (sellResult) sellResult.innerHTML = '';
    if (sellConfirm) sellConfirm.removeAttribute('aria-busy');
    setSellMsg('', '');
    setSellDuration(7);
    sellLastFocus = focusReturn || null;
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
    fetch('/portal/market/sell',
          { method: 'POST', credentials: 'same-origin', headers: csrfHeaders(),
            body: body.toString() })
      .then(function (r) { return r.text(); })
      .then(function (html) {
        setSellMsg('', '');
        if (sellResult) { sellResult.innerHTML = html; wireIconFallbacks(sellResult); }
        if (resultOk(html)) {
          // A listing moves the item to escrow, so the cached grid is stale. Drop
          // the cache + re-render the open container so the reduced stack shows.
          sellConfirm.setAttribute('disabled', 'disabled');
          if (sellCur.container) {
            delete itemsCache[sellCur.container];
            var tile = findContTile(sellCur.container);
            if (tile) loadContainer(tile);
          }
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

  /* -------------------- per-slot action popover --------------------
   * Built once, repositioned per click. "List on Exchange" (sell) is shown on
   * RIGHT-panel slots only and gated client-side on data-can-write (server is
   * authority). "Add price alert" (watch) is on both right + bank slots. */
  var pop = null, popSlot = null, popPanel = null;

  function buildPopover() {
    pop = document.createElement('div');
    pop.className = 'storage-slot-pop';
    pop.setAttribute('role', 'menu');
    pop.setAttribute('tabindex', '-1');
    pop.hidden = true;
    pop.innerHTML =
      '<p class="storage-slot-pop__note" data-pop-note hidden></p>' +
      '<button type="button" class="storage-slot-pop__action" data-pop-action="sell" role="menuitem">List on Exchange</button>' +
      '<button type="button" class="storage-slot-pop__action" data-pop-action="watch" role="menuitem">Add price alert</button>' +
      '<div class="storage-slot-pop__watch" data-pop-watch hidden>' +
        '<label class="sell-modal__label" for="pop-watch-price">Alert at or below</label>' +
        '<div class="sell-modal__inputrow">' +
          '<input type="number" id="pop-watch-price" class="cont-search__input sell-modal__num" ' +
            'min="1" step="1" inputmode="numeric" placeholder="0" data-pop-watch-price ' +
            'aria-label="Alert price in Solari">' +
          '<button type="button" class="portal-btn portal-btn--primary portal-btn--sm" data-pop-watch-add>Add</button>' +
        '</div>' +
        '<p class="storage-slot-pop__msg" data-pop-watch-msg role="status" aria-live="polite"></p>' +
      '</div>';
    layout.appendChild(pop);

    pop.addEventListener('click', function (e) {
      var act = e.target.closest && e.target.closest('[data-pop-action]');
      if (act) {
        onPopAction(act.getAttribute('data-pop-action'));
        return;
      }
      if (e.target.closest && e.target.closest('[data-pop-watch-add]')) {
        submitWatch();
      }
    });
    pop.addEventListener('keydown', function (e) {
      var items = pop.querySelectorAll('[data-pop-action]');
      var idx = -1;
      for (var i = 0; i < items.length; i++) { if (items[i] === document.activeElement) idx = i; }
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        if (items.length) items[(idx + 1) % items.length].focus();
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        if (items.length) items[(idx - 1 + items.length) % items.length].focus();
      } else if (e.key === 'Escape') {
        e.preventDefault();
        var slot = popSlot;
        closePopover();
        if (slot && slot.focus) slot.focus();
      }
    });
  }

  function popContext(slot) {
    var rightHost = slot.closest('#storage-detail');
    var bankHost = slot.closest('#bank-grid');
    var isRight = !!rightHost;
    var container = '';
    if (isRight) {
      var wrap = slot.closest('[data-container-id]');
      container = wrap ? wrap.getAttribute('data-container-id') : '';
    } else if (bankHost) {
      container = bankHost.getAttribute('data-bank-container-id') || '';
    }
    return {
      isRight: isRight,
      item: slot.getAttribute('data-item-id') || '',
      tpl: slot.getAttribute('data-template-id') || '',
      name: slot.getAttribute('data-name') || 'item',
      stack: slot.getAttribute('data-stack') || '1',
      container: container
    };
  }

  function positionPopover(slot) {
    var r = slot.getBoundingClientRect();
    pop.hidden = false;            // measure with layout
    var pw = pop.offsetWidth || 200;
    var ph = pop.offsetHeight || 90;
    var left = r.left;
    var top = r.bottom + 6;
    if (left + pw > window.innerWidth - 8) left = window.innerWidth - pw - 8;
    if (left < 8) left = 8;
    if (top + ph > window.innerHeight - 8) top = r.top - ph - 6;   // flip above
    if (top < 8) top = 8;
    pop.style.position = 'fixed';
    pop.style.left = left + 'px';
    pop.style.top = top + 'px';
  }

  function openPopover(slot) {
    if (!pop) buildPopover();
    if (popSlot === slot && !pop.hidden) { closePopover(); return; }
    popSlot = slot;
    popPanel = popContext(slot);

    var sellBtn = pop.querySelector('[data-pop-action="sell"]');
    var watchBtn = pop.querySelector('[data-pop-action="watch"]');
    var watchWrap = pop.querySelector('[data-pop-watch]');
    var watchPrice = pop.querySelector('[data-pop-watch-price]');
    var watchMsg = pop.querySelector('[data-pop-watch-msg]');
    var note = pop.querySelector('[data-pop-note]');
    if (watchWrap) watchWrap.hidden = true;
    if (watchPrice) watchPrice.value = '';
    if (watchMsg) { watchMsg.textContent = ''; watchMsg.className = 'storage-slot-pop__msg'; }

    // Items the game flags non-tradeable can never reach the exchange, so neither
    // listing nor a price alert is meaningful: show a plain note instead.
    var notTradeable = slot.getAttribute('data-tradeable') === '0';
    if (sellBtn) sellBtn.hidden = notTradeable || !popPanel.isRight;  // sell is RIGHT-panel only
    if (watchBtn) watchBtn.hidden = notTradeable;
    if (note) {
      note.hidden = !notTradeable;
      note.classList.remove('storage-slot-pop__note--warn');  // clear any prior gate notice
      if (notTradeable) note.textContent = 'This item cannot be sold on the Exchange.';
    }

    positionPopover(slot);
    pop.classList.add('storage-slot-pop--open');
    var first = pop.querySelector('[data-pop-action]:not([hidden])');
    if (first) { first.focus(); } else { pop.focus(); }
  }

  function closePopover() {
    if (!pop || pop.hidden) return;
    pop.classList.remove('storage-slot-pop--open');
    pop.hidden = true;
    popSlot = null;
    popPanel = null;
  }

  function onPopAction(action) {
    if (!popPanel) return;
    if (action === 'sell') {
      if (!popPanel.isRight) { closePopover(); return; }
      if (!canWrite) {
        // Mirror the transfer guard: server is authority, fail closed client-side.
        // Show the gate notice in the popover's own note area — NOT by revealing the
        // price-alert panel, which made it look like the wrong dialog opened.
        var gateNote = pop.querySelector('[data-pop-note]');
        if (gateNote) {
          gateNote.hidden = false;
          gateNote.textContent = 'Log out of the game first — storage edits only apply while offline.';
          gateNote.classList.add('storage-slot-pop__note--warn');
        }
        positionPopover(popSlot);   // note added height; reposition
        return;
      }
      var slot = popSlot;
      var ctx = popPanel;
      closePopover();
      openSell(ctx, slot);
    } else if (action === 'watch') {
      var ww = pop.querySelector('[data-pop-watch]');
      var wp = pop.querySelector('[data-pop-watch-price]');
      if (ww) ww.hidden = false;
      if (wp) wp.focus();
      positionPopover(popSlot);   // height grew; reposition
    }
  }

  function submitWatch() {
    if (!popPanel) return;
    var wp = pop.querySelector('[data-pop-watch-price]');
    var add = pop.querySelector('[data-pop-watch-add]');
    var msg = pop.querySelector('[data-pop-watch-msg]');
    function show(text, kind) {
      if (!msg) return;
      msg.textContent = text || '';
      msg.className = 'storage-slot-pop__msg' + (kind ? ' storage-slot-pop__msg--' + kind : '');
    }
    var val = (wp && wp.value || '').trim();
    if (!/^\d+$/.test(val) || parseInt(val, 10) <= 0) {
      show('Enter a whole number price above 0.', 'warn');
      return;
    }
    if (!popPanel.tpl) { show('That item cannot be watched.', 'warn'); return; }
    if (add) { add.setAttribute('disabled', 'disabled'); add.setAttribute('aria-busy', 'true'); }
    var body = new URLSearchParams();
    body.set('template_id', popPanel.tpl);
    body.set('max_price', val);
    var headers = { 'Accept': 'application/json',
                    'Content-Type': 'application/x-www-form-urlencoded' };
    headers[CSRF_HEADER] = getCookie(CSRF_COOKIE);
    fetch('/portal/watchlist/add',
          { method: 'POST', credentials: 'same-origin', headers: headers, body: body.toString() })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; },
                                                function () { return { ok: r.ok, j: null }; }); })
      .then(function (res) {
        if (add) { add.removeAttribute('aria-busy'); }
        if (res.ok && res.j && res.j.ok) {
          show('Watching. Alert at ' + res.j.max_price_display + ' Solari or lower.', 'ok');
        } else {
          show((res.j && res.j.error) || 'Could not add that watch.', 'warn');
          if (add) add.removeAttribute('disabled');
        }
      })
      .catch(function () {
        if (add) { add.removeAttribute('disabled'); add.removeAttribute('aria-busy'); }
        show('Could not add that watch. Please try again.', 'warn');
      });
  }

  // Delegate slot activation on both the right detail host and the bank grid.
  function wirePopoverHost(host) {
    if (!host) return;
    host.addEventListener('click', function (e) {
      var slot = e.target.closest && e.target.closest('.storage-slot--filled');
      if (!slot || !host.contains(slot)) return;
      openPopover(slot);
    });
    host.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      var slot = e.target.closest && e.target.closest('.storage-slot--filled');
      if (!slot || !host.contains(slot)) return;
      e.preventDefault();
      openPopover(slot);
    });
  }
  wirePopoverHost(detail);
  wirePopoverHost(document.getElementById('bank-grid'));

  // Dismiss: outside-click, Escape (after sell modal), scroll, sell-modal open.
  document.addEventListener('click', function (e) {
    if (!pop || pop.hidden) return;
    if (pop.contains(e.target)) return;
    if (popSlot && popSlot.contains(e.target)) return;
    closePopover();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    if (sellModal && !sellModal.hidden) { closeSell(); return; }
    if (pop && !pop.hidden) {
      var slot = popSlot;
      closePopover();
      if (slot && slot.focus) slot.focus();
    }
  });
  window.addEventListener('scroll', function () { closePopover(); }, true);

  /* -------------------- item repair (Repair / Refurbish) --------------------
   * Three tiers POSTing to /portal/repair/{box|gear|everything}. owner_ctrl is
   * resolved server-side from the session; we only send inv_id for the per-box
   * tier. box + gear share one rolling cap bucket (the server enforces it); the
   * "everything" tier is its own 24h bucket. Writes are OFFLINE-only (server
   * hard-gates); buttons fail closed client-side. Responses reuse the storage
   * result fragment so resultOk() works. */
  var repairBar = document.querySelector('.storage-repair');
  var repairMsg = document.getElementById('repair-msg');
  var repairResult = document.getElementById('repair-result');
  var repairGearBtn = repairBar && repairBar.querySelector('[data-repair="gear"]');
  var repairAllBtn = repairBar && repairBar.querySelector('[data-repair="everything"]');
  var repairBoxBtn = document.querySelector('.storage-repair__box-btn[data-repair="box"]');
  var repairHintBox = repairBar && repairBar.querySelector('[data-repair-hint-box]');
  var repairHintAll = repairBar && repairBar.querySelector('[data-repair-hint-all]');

  var boxCd = repairBar ? (parseInt(repairBar.getAttribute('data-repair-box-cd'), 10) || 0) : 0;
  var allCd = repairBar ? (parseInt(repairBar.getAttribute('data-repair-all-cd'), 10) || 0) : 0;
  var repairBusy = false;

  function mmss(secs) {
    secs = Math.max(0, secs | 0);
    var m = Math.floor(secs / 60), s = secs % 60;
    return (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
  }
  function relative(secs) {
    secs = Math.max(0, secs | 0);
    var h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60);
    if (h && m) return 'in ' + h + 'h ' + m + 'm';
    if (h) return 'in ' + h + 'h';
    if (m) return 'in ' + m + 'm';
    return 'shortly';
  }
  function repairNote(text, kind) {
    if (!repairMsg) return;
    repairMsg.textContent = text || '';
    repairMsg.className = 'storage-repair__msg' + (kind ? ' storage-repair__msg--' + kind : '');
  }
  // box + gear gate together (shared bucket); everything gates on its own bucket.
  function boxBucketLocked() { return !canWrite || boxCd > 0 || repairBusy; }
  function allBucketLocked() { return !canWrite || allCd > 0 || repairBusy; }

  function refreshRepairUI() {
    if (repairGearBtn) repairGearBtn.disabled = boxBucketLocked();
    if (repairAllBtn) repairAllBtn.disabled = allBucketLocked();
    if (repairBoxBtn && !repairBoxBtn.hidden) {
      repairBoxBtn.disabled = boxBucketLocked() || !currentCid;
    }
    if (repairHintBox) {
      if (!canWrite) repairHintBox.textContent = 'Repairs apply only while you are logged out of the game. If you are still in-game, log out and reload this page.';
      else if (boxCd > 0) repairHintBox.textContent = 'Repair is on cooldown. Next available in ' + mmss(boxCd) + '.';
      else repairHintBox.textContent = '';
    }
    if (repairHintAll) {
      if (canWrite && allCd > 0) repairHintAll.textContent = 'Full refurbish on cooldown. Next available ' + relative(allCd) + '.';
      else repairHintAll.textContent = '';
    }
  }

  // Called from loadContainer: reveal + gate the per-box "Repair Items" button.
  function repairOnSelect(cid) {
    if (!repairBoxBtn) return;
    repairBoxBtn.hidden = !cid;
    refreshRepairUI();
  }

  function submitRepair(tier) {
    if (repairBusy) return;
    if (!canWrite) {
      repairNote('Repairs only apply while you are logged out of the game. If you are already offline, reload this page and try again.', 'warn');
      return;
    }
    if (tier === 'everything' ? allBucketLocked() : boxBucketLocked()) return;
    var url, body = new URLSearchParams();
    if (tier === 'box') {
      if (!currentCid) { repairNote('Select a container first.', 'warn'); return; }
      url = '/portal/repair/box';
      body.set('inv_id', currentCid);
    } else if (tier === 'gear') {
      url = '/portal/repair/gear';
    } else if (tier === 'everything') {
      if (!window.confirm('Repair AND refurbish all of your gear back to factory durability? This is the once-per-day premium repair.')) return;
      url = '/portal/repair/everything';
    } else {
      return;
    }

    repairBusy = true;
    refreshRepairUI();
    repairNote('Repairing…', '');
    fetch(url, { method: 'POST', credentials: 'same-origin', headers: csrfHeaders(),
                 body: body.toString() })
      .then(function (r) {
        return r.text().then(function (html) { return { status: r.status, html: html }; });
      })
      .then(function (res) {
        repairBusy = false;
        if (repairResult) repairResult.innerHTML = res.html;
        if (tier === 'everything' && resultOk(res.html)) {
          repairNote('Repair saved. If any items still look damaged, log out and back in. Items in a guild or shared base update only after every guild member has logged out, then apply on their next login.', 'ok');
        } else {
          repairNote('', '');
        }
        if (!resultOk(res.html) && res.status === 429) {
          // Bucket just hit its cap; reload to pick up an accurate live countdown.
          window.setTimeout(function () { window.location.reload(); }, 2600);
          return;
        }
        refreshRepairUI();
      })
      .catch(function () {
        repairBusy = false;
        repairNote('That repair could not be completed. Please try again.', 'warn');
        refreshRepairUI();
      });
  }

  if (repairGearBtn) repairGearBtn.addEventListener('click', function () { submitRepair('gear'); });
  if (repairAllBtn) repairAllBtn.addEventListener('click', function () { submitRepair('everything'); });
  if (repairBoxBtn) repairBoxBtn.addEventListener('click', function () { submitRepair('box'); });

  // Live cooldown ticks: re-enable a bucket's buttons when it frees up.
  if (repairBar && (boxCd > 0 || allCd > 0)) {
    window.setInterval(function () {
      if (boxCd > 0) boxCd--;
      if (allCd > 0) allCd--;
      refreshRepairUI();
    }, 1000);
  }
  refreshRepairUI();

  /* initial icon fallbacks (bank grid + container tile icons) */
  wireIconFallbacks(layout);
})();
