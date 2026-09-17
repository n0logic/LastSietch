/* My Orders — tab switching + Cancel / Relist write actions for /portal/my-orders.
   Tabs (Active / Completed / History) are rendered server-side; this toggles them
   and wires the Cancel (active rows) + Relist (canceled Completed rows) actions.
   Both engine procs are online-safe, so there is no offline gating here. */
(function () {
  'use strict';

  var CSRF_COOKIE = 'ls_portal_csrf';
  var CSRF_HEADER = 'X-Portal-CSRF-Token';

  function getCookie(name) {
    var m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : '';
  }
  function fmt(n) { return (n || 0).toLocaleString('en-US'); }

  /* Listing fee, matching the writer's authoritative integer half-up math:
     (price*(days+1)+50)//100 + 20*days. */
  function listFee(price, days) {
    if (!price || price < 1) return 0;
    return Math.floor((price * (days + 1) + 50) / 100) + 20 * days;
  }

  /* ---- tab switching ---- */
  var tabs = Array.prototype.slice.call(document.querySelectorAll('[data-orders-tab]'));
  var panels = Array.prototype.slice.call(document.querySelectorAll('[data-orders-panel]'));
  function showTab(name) {
    for (var i = 0; i < panels.length; i++) {
      panels[i].hidden = panels[i].getAttribute('data-orders-panel') !== name;
    }
    for (var j = 0; j < tabs.length; j++) {
      var on = tabs[j].getAttribute('data-orders-tab') === name;
      tabs[j].classList.toggle('orders-tab--active', on);
      tabs[j].setAttribute('aria-selected', on ? 'true' : 'false');
    }
  }
  for (var k = 0; k < tabs.length; k++) {
    tabs[k].addEventListener('click', function () {
      showTab(this.getAttribute('data-orders-tab'));
    });
  }

  /* ---- shared message slot at the top of the card ---- */
  var msgEl = document.getElementById('orders-msg');
  function topMsg(text, kind) {
    if (!msgEl) return;
    msgEl.textContent = text || '';
    msgEl.className = 'orders-msg' + (kind ? ' orders-msg--' + kind : '');
    msgEl.hidden = !text;
  }

  function csrfHeaders() {
    var h = { 'Accept': 'text/html', 'Content-Type': 'application/x-www-form-urlencoded' };
    h[CSRF_HEADER] = getCookie(CSRF_COOKIE);
    return h;
  }

  function resultOk(html) {
    return html && html.indexOf('market-buy-result--ok') !== -1;
  }

  /* ---- Cancel (active rows): confirm -> POST -> reload ---- */
  function wireCancel() {
    var btns = document.querySelectorAll('[data-order-cancel]');
    for (var i = 0; i < btns.length; i++) {
      btns[i].addEventListener('click', function () {
        var btn = this;
        var name = btn.getAttribute('data-order-name') || 'this listing';
        if (!window.confirm('Cancel the listing for ' + name +
            '?\nThe listing fee is forfeited and the item moves to your Completed tab.')) {
          return;
        }
        btn.setAttribute('disabled', 'disabled');
        btn.setAttribute('aria-busy', 'true');
        topMsg('Canceling…', '');
        var body = new URLSearchParams();
        body.set('order_id', btn.getAttribute('data-order-id') || '');
        body.set('revision', btn.getAttribute('data-order-rev') || '');
        fetch('/portal/my-orders/cancel',
              { method: 'POST', credentials: 'same-origin', headers: csrfHeaders(), body: body.toString() })
          .then(function (r) { return r.text(); })
          .then(function (html) {
            var ok = resultOk(html);
            topMsg(ok ? 'Listing canceled — refreshing…' : 'Could not cancel. Refresh and try again.',
                   ok ? 'ok' : 'warn');
            if (ok) { window.setTimeout(function () { window.location.reload(); }, 1200); }
            else { btn.removeAttribute('disabled'); btn.removeAttribute('aria-busy'); }
          })
          .catch(function () {
            topMsg('Could not cancel. Please try again.', 'warn');
            btn.removeAttribute('disabled'); btn.removeAttribute('aria-busy');
          });
      });
    }
  }

  /* ---- Relist (canceled Completed rows): modal -> POST -> reload ---- */
  var modal = document.getElementById('relist-modal');
  var overlay = document.getElementById('relist-modal-overlay');
  var closeBtn = document.getElementById('relist-modal-close');
  var cancelBtn = document.getElementById('relist-cancel');
  var confirmBtn = document.getElementById('relist-confirm');
  var nameEl = modal && modal.querySelector('[data-relist-name]');
  var priceEl = modal && modal.querySelector('[data-relist-price]');
  var feeEl = modal && modal.querySelector('[data-relist-fee]');
  var durBtns = modal ? modal.querySelectorAll('[data-relist-dur]') : [];
  var relistMsg = document.getElementById('relist-msg');
  var relistResult = document.getElementById('relist-result');
  var cur = { order: 0, rev: 0, days: 7 };
  var lastFocus = null;

  function relistNote(text, kind) {
    if (!relistMsg) return;
    relistMsg.textContent = text || '';
    relistMsg.className = 'sell-modal__msg' + (kind ? ' sell-modal__msg--' + kind : '');
  }
  function priceVal() {
    var v = parseInt((priceEl.value || '').replace(/,/g, '').trim(), 10);
    return (!v || v < 1) ? 0 : v;
  }
  function recompute() {
    if (feeEl) feeEl.textContent = fmt(listFee(priceVal(), cur.days));
    if (confirmBtn) confirmBtn.disabled = priceVal() < 1;
  }
  function setDur(days) {
    cur.days = days;
    for (var i = 0; i < durBtns.length; i++) {
      var on = parseInt(durBtns[i].getAttribute('data-relist-dur'), 10) === days;
      durBtns[i].classList.toggle('sell-modal__dur--active', on);
      durBtns[i].setAttribute('aria-pressed', on ? 'true' : 'false');
    }
    recompute();
  }
  function openModal(btn) {
    if (!modal) return;
    cur.order = parseInt(btn.getAttribute('data-order-id'), 10) || 0;
    cur.rev = parseInt(btn.getAttribute('data-order-rev'), 10) || 0;
    if (!cur.order || !cur.rev) return;
    if (nameEl) nameEl.textContent = btn.getAttribute('data-order-name') || 'item';
    if (priceEl) priceEl.value = btn.getAttribute('data-order-price') || '1000';
    if (relistResult) relistResult.innerHTML = '';
    if (confirmBtn) confirmBtn.removeAttribute('aria-busy');
    relistNote('', '');
    setDur(7);
    lastFocus = btn;
    overlay.hidden = false;
    modal.hidden = false;
    window.requestAnimationFrame(function () {
      modal.classList.add('ls-modal--open');
      overlay.classList.add('ls-modal-overlay--open');
    });
    if (priceEl) priceEl.focus();
  }
  function closeModal() {
    if (!modal || modal.hidden) return;
    modal.classList.remove('ls-modal--open');
    overlay.classList.remove('ls-modal-overlay--open');
    window.setTimeout(function () { modal.hidden = true; overlay.hidden = true; }, 180);
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }
  function submitRelist() {
    var price = priceVal();
    if (price < 1) { relistNote('Enter a price of 1 Solari or more.', 'warn'); return; }
    if (!cur.order || !cur.rev) { relistNote('That order changed. Refresh and try again.', 'warn'); return; }
    confirmBtn.setAttribute('disabled', 'disabled');
    confirmBtn.setAttribute('aria-busy', 'true');
    relistNote('Relisting…', '');
    var body = new URLSearchParams();
    body.set('order_id', cur.order);
    body.set('revision', cur.rev);
    body.set('price', price);
    body.set('duration_days', cur.days);
    fetch('/portal/my-orders/relist',
          { method: 'POST', credentials: 'same-origin', headers: csrfHeaders(), body: body.toString() })
      .then(function (r) { return r.text(); })
      .then(function (html) {
        if (relistResult) relistResult.innerHTML = html;
        var ok = resultOk(html);
        relistNote('', '');
        if (ok) { window.setTimeout(function () { window.location.reload(); }, 1400); }
        else {
          confirmBtn.removeAttribute('disabled');
          confirmBtn.removeAttribute('aria-busy');
        }
      })
      .catch(function () {
        relistNote('Relist failed. Please try again.', 'warn');
        confirmBtn.removeAttribute('disabled');
        confirmBtn.removeAttribute('aria-busy');
      });
  }

  function wireRelist() {
    var btns = document.querySelectorAll('[data-order-relist]');
    for (var i = 0; i < btns.length; i++) {
      btns[i].addEventListener('click', function () { openModal(this); });
    }
    if (priceEl) priceEl.addEventListener('input', recompute);
    for (var d = 0; d < durBtns.length; d++) {
      durBtns[d].addEventListener('click', function () {
        setDur(parseInt(this.getAttribute('data-relist-dur'), 10) || 7);
      });
    }
    if (closeBtn) closeBtn.addEventListener('click', closeModal);
    if (cancelBtn) cancelBtn.addEventListener('click', closeModal);
    if (overlay) overlay.addEventListener('click', closeModal);
    if (confirmBtn) confirmBtn.addEventListener('click', submitRelist);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeModal();
    });
  }

  wireCancel();
  wireRelist();
})();
