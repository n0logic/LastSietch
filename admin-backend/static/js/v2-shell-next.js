/* H0 shell behaviour: jump-to-page dialog, live online count, dismissable guide
 * cards, phone tab bar scroll-to-active. Loaded only when the shell flag is on.
 * Presentation only: the one network call is the public read-only pulse. */
(function () {
  'use strict';

  function store(op, key, value) {
    try {
      if (op === 'get') return window.localStorage.getItem(key);
      window.localStorage.setItem(key, value);
    } catch (e) { /* private window or blocked storage: the page works without it */ }
    return null;
  }

  // ---- Jump to a page ------------------------------------------------------
  var dlg, input, list, empty, openBtn, items, sel = -1, lastFocus = null;

  function visible() {
    return items.filter(function (li) { return !li.hidden; });
  }

  function mark() {
    var v = visible();
    items.forEach(function (li) { li.firstElementChild.classList.remove('is-sel'); });
    if (sel >= 0 && v[sel]) v[sel].firstElementChild.classList.add('is-sel');
  }

  function filter() {
    var q = input.value.trim().toLowerCase();
    items.forEach(function (li) {
      li.hidden = q !== '' && li.textContent.toLowerCase().indexOf(q) === -1;
    });
    empty.hidden = visible().length > 0;
    sel = visible().length ? 0 : -1;
    mark();
  }

  function openJump() {
    lastFocus = document.activeElement;
    dlg.hidden = false;
    input.value = '';
    filter();
    input.focus();
  }

  function closeJump() {
    dlg.hidden = true;
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }

  function initJump() {
    dlg = document.getElementById('ls-jumpdlg');
    if (!dlg) return;
    input = document.getElementById('ls-jump-q');
    list = document.getElementById('ls-jump-list');
    empty = document.getElementById('ls-jump-empty');
    openBtn = document.getElementById('ls-jump-open');
    items = Array.prototype.slice.call(list.children);

    openBtn.addEventListener('click', openJump);
    input.addEventListener('input', filter);
    dlg.addEventListener('click', function (e) { if (e.target === dlg) closeJump(); });
    input.addEventListener('keydown', function (e) {
      var v = visible();
      if (e.key === 'ArrowDown' && v.length) { sel = (sel + 1) % v.length; mark(); e.preventDefault(); }
      else if (e.key === 'ArrowUp' && v.length) { sel = (sel - 1 + v.length) % v.length; mark(); e.preventDefault(); }
      else if (e.key === 'Enter' && v[sel]) { window.location.href = v[sel].firstElementChild.getAttribute('href'); }
    });
    document.addEventListener('keydown', function (e) {
      var t = e.target, typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable);
      if (e.key === 'Escape' && !dlg.hidden) { closeJump(); return; }
      if (e.key === '/' && !typing && dlg.hidden && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        openJump();
      }
    });
  }

  // ---- Live online count ---------------------------------------------------
  function initOnline() {
    var box = document.getElementById('ls-online');
    var num = document.getElementById('ls-online-n');
    if (!box || !num) return;

    function tick() {
      if (document.hidden) return;
      fetch('/admin/api/dune/pulse', { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error('pulse ' + r.status)); })
        .then(function (d) {
          var n = d && typeof d.online === 'number' ? d.online : null;
          num.textContent = n === null ? '-' : String(n);
          box.classList.toggle('is-stale', n === null);
        })
        .catch(function () { num.textContent = '-'; box.classList.add('is-stale'); });
    }
    tick();
    window.setInterval(tick, 60000);
    document.addEventListener('visibilitychange', function () { if (!document.hidden) tick(); });
  }

  // ---- Guide cards: dismiss and remember -----------------------------------
  function initGuides() {
    Array.prototype.forEach.call(document.querySelectorAll('.ls-guide[data-guide]'), function (card) {
      var key = 'ls-guide-' + card.getAttribute('data-guide');
      if (store('get', key) === '1') { card.hidden = true; return; }
      var x = card.querySelector('.ls-guide__x');
      if (x) x.addEventListener('click', function () { card.hidden = true; store('set', key, '1'); });
    });
  }

  function initTabbar() {
    var on = document.querySelector('.ls-tabbar__i.is-active');
    if (on && on.scrollIntoView) {
      try { on.scrollIntoView({ block: 'nearest', inline: 'center' }); } catch (e) { /* older browsers */ }
    }
  }

  function init() { initJump(); initOnline(); initGuides(); initTabbar(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
