/* P8 — CVars tab JS.
 *
 * Vanilla JS, no new deps. Three concerns:
 *   1. Confirm-modal handler: type "CONFIRM" gate + PUT submit
 *   2. User/All toggle persistence via ls_cvars_view cookie
 *   3. Catalog full-text filter (client-side row hiding)
 *
 * HTMX handles sub-tab swaps; sub-tab "active" highlight follows aria-selected.
 * Re-bind row triggers + filters after every HTMX swap (htmx:afterSwap event).
 */

(function () {
  'use strict';

  var CONFIRM_TOKEN = 'CONFIRM';
  var COOKIE_VIEW = 'ls_cvars_view';
  var UPDATE_URL = '/admin/v2/server/cvars/write';

  /* -------------------- Modal state -------------------- */

  var modal = null;
  var fieldSection = null;
  var fieldKey = null;
  var fieldSource = null;
  var fieldOld = null;
  var fieldNew = null;
  var inputNewval = null;
  var inputToken = null;
  var inputReason = null;
  var btnSubmit = null;
  var btnCancel = null;
  var statusEl = null;

  function cacheModal() {
    modal = document.getElementById('cvars-confirm-overlay');
    if (!modal) return false;
    fieldSection = document.getElementById('cvars-confirm-section');
    fieldKey = document.getElementById('cvars-confirm-key');
    fieldSource = document.getElementById('cvars-confirm-source');
    fieldOld = document.getElementById('cvars-confirm-old');
    fieldNew = document.getElementById('cvars-confirm-new');
    inputNewval = document.getElementById('cvars-confirm-newval');
    inputToken = document.getElementById('cvars-confirm-token');
    inputReason = document.getElementById('cvars-confirm-reason');
    btnSubmit = document.getElementById('cvars-confirm-submit');
    btnCancel = document.getElementById('cvars-confirm-cancel');
    statusEl = document.getElementById('cvars-confirm-status');
    return true;
  }

  function openModal(trigger) {
    var section = trigger.getAttribute('data-cvar-section') || '';
    var key = trigger.getAttribute('data-cvar-key') || '';
    var oldVal = trigger.getAttribute('data-cvar-old') || '';
    var layer = trigger.getAttribute('data-cvar-layer') || '';

    fieldSection.textContent = section;
    fieldKey.textContent = key;
    fieldSource.textContent = layer || '—';
    fieldOld.textContent = oldVal || '(unset)';
    fieldNew.textContent = '—';
    inputNewval.value = oldVal;
    inputToken.value = '';
    inputReason.value = '';
    statusEl.textContent = '';
    statusEl.className = 'cvars-confirm-dialog__status';
    btnSubmit.disabled = true;
    btnSubmit.dataset.section = section;
    btnSubmit.dataset.key = key;

    modal.hidden = false;
    setTimeout(function () { inputNewval.focus(); }, 0);
  }

  function closeModal() {
    if (!modal) return;
    modal.hidden = true;
    btnSubmit.disabled = true;
  }

  function refreshNewPreview() {
    if (fieldNew) fieldNew.textContent = inputNewval.value || '(cleared)';
  }

  function refreshSubmitEnabled() {
    var ok = inputToken.value.trim() === CONFIRM_TOKEN;
    btnSubmit.disabled = !ok;
  }

  function setStatus(text, kind) {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.className = 'cvars-confirm-dialog__status' + (kind ? ' cvars-confirm-dialog__status--' + kind : '');
  }

  function submitWrite() {
    if (btnSubmit.disabled) return;
    var section = btnSubmit.dataset.section;
    var key = btnSubmit.dataset.key;
    var newVal = inputNewval.value;
    var reason = inputReason.value.trim() || null;
    var token = inputToken.value.trim();

    btnSubmit.disabled = true;
    setStatus('Submitting…', null);

    // Server-side route is POST /v2/server/cvars/write (CvarWriteRequest:
    // section, key, value, reason?, confirm_token?). Hand-rolled fetch
    // keeps csrf header threading consistent with base.html's apiCall.
    fetch(UPDATE_URL, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': (typeof csrfToken !== 'undefined') ? csrfToken : '',
      },
      body: JSON.stringify({
        section: section,
        key: key,
        value: newVal === '' ? '' : newVal,
        reason: reason,
        confirm_token: token,
      }),
    })
      .then(function (r) { return r.json().then(function (data) { return { ok: r.ok, data: data }; }); })
      .then(function (res) {
        if (!res.ok || !res.data.success) {
          var msg = (res.data && (res.data.message || res.data.detail)) || 'Write failed';
          setStatus(msg, 'err');
          btnSubmit.disabled = false;
          return;
        }
        // NEVER show "Saved." Server banner copy is load-bearing.
        var banner = res.data.banner || 'Queued — applies at next maintenance restart';
        // If the DB INSERT raced after the file write succeeded, the change
        // IS applied but won't show in History until manual reconciliation.
        // Operators need to know this explicitly.
        if (res.data.audit_db_failed) {
          banner += ' — WARNING: change_id INSERT failed; manual reconciliation needed';
          setStatus(banner, 'err');
        } else {
          setStatus(banner, 'ok');
        }
        // After 2s, close + refresh the active sub-tab so the operator sees
        // the change reflected in the layer-walk.
        setTimeout(function () {
          closeModal();
          var active = document.querySelector('.v2-cvars__subtab.active');
          if (active && typeof htmx !== 'undefined') {
            htmx.trigger(active, 'click');
          }
        }, 1500);
      })
      .catch(function (err) {
        setStatus(String(err && err.message || err), 'err');
        btnSubmit.disabled = false;
      });
  }

  /* -------------------- Bindings -------------------- */

  function bindEditTriggers(root) {
    var triggers = (root || document).querySelectorAll('.cvars-edit-trigger');
    for (var i = 0; i < triggers.length; i++) {
      triggers[i].addEventListener('click', function (e) {
        openModal(e.currentTarget);
      });
    }
  }

  function bindCatalogFilter() {
    var input = document.getElementById('cvars-catalog-search');
    if (!input) return;
    input.addEventListener('input', function () {
      var needle = input.value.trim().toLowerCase();
      var rows = document.querySelectorAll('.v2-cvars-catalog__row');
      for (var i = 0; i < rows.length; i++) {
        var key = (rows[i].getAttribute('data-key') || '').toLowerCase();
        var label = (rows[i].getAttribute('data-label') || '').toLowerCase();
        var match = !needle || key.indexOf(needle) !== -1 || label.indexOf(needle) !== -1;
        rows[i].classList.toggle('v2-cvars-catalog__row--hidden', !match);
      }
    });
  }

  function bindSubtabActive() {
    // One active class at any moment; aria-selected follows.
    var tabs = document.querySelectorAll('.v2-cvars__subtab');
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].addEventListener('click', function (e) {
        for (var j = 0; j < tabs.length; j++) {
          tabs[j].classList.remove('active');
          tabs[j].setAttribute('aria-selected', 'false');
        }
        e.currentTarget.classList.add('active');
        e.currentTarget.setAttribute('aria-selected', 'true');
      });
    }
  }

  function bindModalInputs() {
    if (!modal) return;
    inputNewval.addEventListener('input', refreshNewPreview);
    inputToken.addEventListener('input', refreshSubmitEnabled);
    btnSubmit.addEventListener('click', submitWrite);
    btnCancel.addEventListener('click', closeModal);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !modal.hidden) closeModal();
    });
  }

  /* -------------------- User/All view cookie -------------------- */

  function getCookie(name) {
    var m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : null;
  }

  function setCookie(name, value) {
    document.cookie = name + '=' + encodeURIComponent(value) +
      '; path=/admin; max-age=31536000; SameSite=Lax';
  }

  function bindViewToggle() {
    var toggle = document.getElementById('cvars-live-view-toggle');
    if (!toggle) return;
    var current = getCookie(COOKIE_VIEW) || 'user';
    toggle.value = current;
    toggle.addEventListener('change', function () {
      setCookie(COOKIE_VIEW, toggle.value);
      // Trigger a re-fetch of the Live fragment so the server filters.
      if (typeof htmx !== 'undefined') {
        var live = document.getElementById('subtab-cvars-live');
        if (live) htmx.trigger(live, 'click');
      }
    });
  }

  /* -------------------- Boot + re-bind on HTMX swap -------------------- */

  function init() {
    cacheModal();
    bindModalInputs();
    bindSubtabActive();
    bindEditTriggers();
    bindCatalogFilter();
    bindViewToggle();
  }

  document.addEventListener('DOMContentLoaded', init);
  document.body.addEventListener('htmx:afterSwap', function (e) {
    if (e.target && e.target.id === 'cvars-subtab-body') {
      bindEditTriggers(e.target);
      bindCatalogFilter();
      bindViewToggle();
    }
  });
}());
