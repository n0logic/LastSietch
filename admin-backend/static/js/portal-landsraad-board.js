/* Last Sietch portal — Landsraad LIVE TERM BOARD interactions.
 * Two jobs:
 *   (a) tick the term countdown from the served end_utc (no re-fetch), and
 *   (b) click a house tile -> slide-in drawer with both-faction progress, the
 *       reward ladder, and (when signed in) the player's personal contribution.
 * Vanilla, no deps. DOM built with createElement + textContent only (no innerHTML),
 * matching portal.js. Reads the #lb-board-data JSON island (CSP-safe; not executed).
 * Distinct from portal-landsraad.js, which drives the separate "Your rewards" board.
 */
(function () {
  'use strict';

  /* ---- bare timestamp = UTC (same convention as time-local.js) ---- */
  function toUtcDate(s) {
    if (!s) return null;
    var str = String(s).trim().replace(' ', 'T');
    if (!/[Zz]$/.test(str) && !/[+-]\d\d:?\d\d$/.test(str)) str += 'Z';
    var d = new Date(str);
    return isNaN(d.getTime()) ? null : d;
  }

  /* ===================== term countdown ===================== */
  (function countdown() {
    var box = document.querySelector('.lb__countdown');
    var out = document.getElementById('lb-countdown');
    if (!box || !out) return;
    var end = toUtcDate(box.getAttribute('data-end-utc'));
    if (!end) { out.textContent = ''; return; }

    function fmt(ms) {
      if (ms <= 0) return null;
      var s = Math.floor(ms / 1000);
      var d = Math.floor(s / 86400); s -= d * 86400;
      var h = Math.floor(s / 3600);  s -= h * 3600;
      var m = Math.floor(s / 60);    s -= m * 60;
      if (d > 0) return d + 'd ' + h + 'h ' + m + 'm';
      if (h > 0) return h + 'h ' + m + 'm ' + s + 's';
      return m + 'm ' + s + 's';
    }

    var timer = null;
    function tick() {
      var left = fmt(end.getTime() - Date.now());
      if (left === null) {
        out.textContent = 'Term ended';
        box.classList.add('lb__countdown--ended');
        if (timer) window.clearInterval(timer);
        return;
      }
      out.textContent = left;
    }
    tick();
    timer = window.setInterval(tick, 1000);
  })();

  /* ===================== tile detail drawer ===================== */
  var dataEl = document.getElementById('lb-board-data');
  var board = document.querySelector('.lb-board');
  if (!dataEl || !board) return;

  var TILES;
  try {
    TILES = JSON.parse(dataEl.textContent || '[]');
  } catch (e) {
    return;
  }

  var drawer = document.getElementById('lb-drawer');
  var overlay = document.getElementById('lb-drawer-overlay');
  var drawerTitle = document.getElementById('lb-drawer-title');
  var drawerBody = document.getElementById('lb-drawer-body');
  var drawerClose = document.getElementById('lb-drawer-close');
  var lastFocused = null;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function tileFor(target) {
    var t = target.closest ? target.closest('.lb-tile') : null;
    if (!t) return null;
    var idx = parseInt(t.getAttribute('data-tile-index'), 10);
    return isNaN(idx) ? null : { el: t, data: TILES[idx] };
  }

  function sectionTitle(text) {
    return el('h3', 'lb-drawer__section', text);
  }

  /* both-faction progress block (amount / goal + bar per faction) */
  function buildProgress(t) {
    var wrap = el('div', 'lb-prog');
    var head = el('div', 'lb-prog__goal');
    head.appendChild(el('span', 'lb-prog__goal-label', 'Goal'));
    head.appendChild(el('span', 'lb-prog__goal-val mono', t.goal_display));
    wrap.appendChild(head);
    for (var i = 0; i < (t.factions || []).length; i++) {
      var f = t.factions[i];
      var row = el('div', 'lb-prog__row lb-prog__row--' + f.slug + (f.leading ? ' is-leading' : ''));
      var label = el('div', 'lb-prog__rowhead');
      label.appendChild(el('span', 'lb-prog__faction', 'House ' + f.name));
      label.appendChild(el('span', 'lb-prog__amt mono', f.amount_display + ' / ' + t.goal_display));
      row.appendChild(label);
      var bar = el('div', 'lb-prog__bar');
      var fill = el('div', 'lb-prog__bar-fill');
      fill.style.width = f.pct + '%';
      bar.appendChild(fill);
      row.appendChild(bar);
      wrap.appendChild(row);
    }
    return wrap;
  }

  /* term reward ladder (threshold -> reward; reached marks when signed in) */
  function buildLadder(t) {
    var list = el('ul', 'lb-ladder');
    for (var i = 0; i < t.rewards.length; i++) {
      var r = t.rewards[i];
      var li = el('li', 'lb-ladder__row' + (r.reached ? ' lb-ladder__row--reached' : ''));
      li.appendChild(el('span', 'lb-ladder__mark', r.reached ? '✓' : ''));
      li.appendChild(el('span', 'lb-ladder__threshold mono', r.threshold_display));
      var rw = el('span', 'lb-ladder__reward');
      if (!r.is_solari && r.amount > 1) rw.appendChild(el('span', 'lb-ladder__qty mono', r.amount + '× '));
      rw.appendChild(el('span', 'lb-ladder__reward-name',
        r.is_solari ? (r.amount_display + ' Solari') : r.name));
      li.appendChild(rw);
      list.appendChild(li);
    }
    return list;
  }

  function openDrawer(ctx) {
    if (!drawer) return;
    var t = ctx.data;
    drawerTitle.textContent = t.name;
    drawerBody.textContent = '';

    // status chip row
    var meta = el('div', 'lb-drawer__meta');
    if (t.winner) {
      meta.appendChild(el('span', 'lb-drawer__chip lb-drawer__chip--' + t.winner.slug,
        'Won by House ' + t.winner.name));
    } else {
      meta.appendChild(el('span', 'lb-drawer__chip', 'Contested'));
    }
    if (t.sysselraad) meta.appendChild(el('span', 'lb-drawer__chip lb-drawer__chip--syss', 'Sysselraad line'));
    drawerBody.appendChild(meta);

    if (t.rep_location) {
      var rep = el('div', 'lb-drawer__rep');
      rep.appendChild(el('span', 'lb-drawer__rep-label', 'Rep location'));
      rep.appendChild(el('span', 'lb-drawer__rep-loc', t.rep_location));
      drawerBody.appendChild(rep);
    }

    drawerBody.appendChild(sectionTitle('Progress'));
    drawerBody.appendChild(buildProgress(t));

    if (t.my_contribution_display != null) {
      var mine = el('div', 'lb-drawer__mine');
      mine.appendChild(el('span', 'lb-drawer__mine-label', 'Your contribution'));
      mine.appendChild(el('span', 'lb-drawer__mine-val mono', t.my_contribution_display));
      drawerBody.appendChild(mine);
    }

    if (t.rewards && t.rewards.length) {
      drawerBody.appendChild(sectionTitle("This term's rewards"));
      drawerBody.appendChild(buildLadder(t));
    }

    lastFocused = ctx.el;
    overlay.hidden = false;
    drawer.hidden = false;
    window.requestAnimationFrame(function () {
      drawer.classList.add('lb-drawer--open');
      overlay.classList.add('lb-drawer-overlay--open');
    });
    if (drawerClose) drawerClose.focus();
  }

  function closeDrawer() {
    if (!drawer || drawer.hidden) return;
    drawer.classList.remove('lb-drawer--open');
    overlay.classList.remove('lb-drawer-overlay--open');
    window.setTimeout(function () {
      drawer.hidden = true;
      overlay.hidden = true;
    }, 200);
    if (lastFocused && lastFocused.focus) lastFocused.focus();
  }

  board.addEventListener('click', function (e) {
    var ctx = tileFor(e.target);
    if (ctx && ctx.data) openDrawer(ctx);
  });
  // tiles are <button>, so Enter/Space already fire click.
  if (drawerClose) drawerClose.addEventListener('click', closeDrawer);
  if (overlay) overlay.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeDrawer();
  });
})();
