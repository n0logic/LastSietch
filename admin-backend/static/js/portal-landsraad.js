/* Last Sietch portal — Landsraad board interactions.
 * Reads the #ls-board-data JSON island (CSP-safe; not executed) and wires:
 *   (a) hover a house tile  -> floating tooltip with a quick item peek
 *   (b) click a house tile  -> slide-in drawer with the full reward detail
 * Vanilla, no deps. Builds DOM with createElement + textContent only
 * (no innerHTML concat), matching portal.js. Degrades to the <noscript> list.
 */
(function () {
  'use strict';

  var dataEl = document.getElementById('ls-board-data');
  var board = document.querySelector('.ls-board');
  if (!dataEl || !board) return;

  var HOUSES;
  try {
    HOUSES = JSON.parse(dataEl.textContent || '[]');
  } catch (e) {
    return;
  }

  var tooltip = document.getElementById('ls-tooltip');
  var drawer = document.getElementById('ls-drawer');
  var overlay = document.getElementById('ls-drawer-overlay');
  var drawerTitle = document.getElementById('ls-drawer-title');
  var drawerBody = document.getElementById('ls-drawer-body');
  var drawerClose = document.getElementById('ls-drawer-close');
  var lastFocused = null;

  function houseFor(el) {
    var tile = el.closest ? el.closest('.ls-tile') : null;
    if (!tile) return null;
    var idx = parseInt(tile.getAttribute('data-house-index'), 10);
    return isNaN(idx) ? null : { tile: tile, data: HOUSES[idx] };
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  /* ---- item list builder (shared by tooltip + drawer) ---- */
  function buildItemList(h, limit) {
    var ul = el('ul', 'ls-items');
    var shown = 0;
    if (h.solari_display) {
      var sLi = el('li', 'ls-items__row ls-items__row--solari');
      sLi.appendChild(el('span', 'ls-items__name', 'Solari'));
      sLi.appendChild(el('span', 'ls-items__amt mono', h.solari_display));
      ul.appendChild(sLi);
      shown++;
    }
    var items = h.items || [];
    for (var i = 0; i < items.length; i++) {
      if (limit && shown >= limit) {
        var more = el('li', 'ls-items__more', '+' + (items.length - (shown - (h.solari_display ? 1 : 0))) + ' more');
        ul.appendChild(more);
        break;
      }
      var li = el('li', 'ls-items__row');
      li.appendChild(el('span', 'ls-items__name', items[i].name || ''));
      if (items[i].amount && items[i].amount > 1) {
        li.appendChild(el('span', 'ls-items__amt mono', '×' + items[i].amount));
      }
      ul.appendChild(li);
      shown++;
    }
    return ul;
  }

  /* ---- tooltip (hover quick-peek) ---- */
  function showTooltip(ctx) {
    if (!tooltip) return;
    var h = ctx.data;
    tooltip.textContent = '';
    tooltip.appendChild(el('div', 'ls-tooltip__name', h.name));
    if (!h.has_rewards) {
      tooltip.appendChild(el('div', 'ls-tooltip__empty', 'No rewards waiting'));
    } else {
      tooltip.appendChild(buildItemList(h, 6));
      tooltip.appendChild(el('div', 'ls-tooltip__hint', 'Select for full details'));
    }
    tooltip.hidden = false;
    positionTooltip(ctx.tile);
  }

  function positionTooltip(tile) {
    var r = tile.getBoundingClientRect();
    var tt = tooltip.getBoundingClientRect();
    var margin = 8;
    var left = r.left + (r.width / 2) - (tt.width / 2);
    left = Math.max(margin, Math.min(left, window.innerWidth - tt.width - margin));
    var top = r.bottom + margin;
    if (top + tt.height > window.innerHeight - margin) {
      top = r.top - tt.height - margin; // flip above when near the bottom
    }
    tooltip.style.left = Math.round(left) + 'px';
    tooltip.style.top = Math.round(Math.max(margin, top)) + 'px';
  }

  function hideTooltip() {
    if (tooltip) tooltip.hidden = true;
  }

  /* ---- section heading ---- */
  function sectionTitle(text) {
    return el('h3', 'ls-drawer__section', text);
  }

  /* ---- current-term reward ladder (in-game board view) ---- */
  function buildLadder(lad, shortName) {
    var wrap = el('div', 'ls-ladder');
    // progress bar: this player's contribution toward the house goal
    var prog = el('div', 'ls-ladder__progress');
    var head = el('div', 'ls-ladder__progress-head');
    head.appendChild(el('span', 'ls-ladder__progress-label', 'Your contribution'));
    head.appendChild(el('span', 'ls-ladder__progress-val mono',
      lad.my_contribution_display + ' / ' + lad.goal_display));
    prog.appendChild(head);
    var bar = el('div', 'ls-ladder__bar');
    var fill = el('div', 'ls-ladder__bar-fill');
    fill.style.width = lad.pct + '%';
    bar.appendChild(fill);
    prog.appendChild(bar);
    wrap.appendChild(prog);

    // threshold -> reward rows, marked reached/locked
    var list = el('ul', 'ls-ladder__rows');
    for (var i = 0; i < lad.rewards.length; i++) {
      var r = lad.rewards[i];
      var li = el('li', 'ls-ladder__row' + (r.reached ? ' ls-ladder__row--reached' : ''));
      var mark = el('span', 'ls-ladder__mark', r.reached ? '✓' : '');
      li.appendChild(mark);
      li.appendChild(el('span', 'ls-ladder__threshold mono', r.threshold_display));
      var rw = el('span', 'ls-ladder__reward');
      if (!r.is_solari && r.amount > 1) rw.appendChild(el('span', 'ls-ladder__qty mono', r.amount + '× '));
      rw.appendChild(el('span', 'ls-ladder__reward-name', r.is_solari ? (r.amount_display + ' Solari') : r.name));
      li.appendChild(rw);
      list.appendChild(li);
    }
    wrap.appendChild(list);
    return wrap;
  }

  /* ---- drawer (click detail) ---- */
  function openDrawer(ctx) {
    if (!drawer) return;
    var h = ctx.data;
    drawerTitle.textContent = h.name;
    drawerBody.textContent = '';

    if (h.rep_location) {
      var rep = el('div', 'ls-drawer__rep');
      rep.appendChild(el('span', 'ls-drawer__rep-label', 'Rep location'));
      rep.appendChild(el('span', 'ls-drawer__rep-loc', h.rep_location));
      drawerBody.appendChild(rep);
    }

    // --- Awaiting pickup ---
    if (!h.has_rewards) {
      drawerBody.appendChild(el('p', 'ls-drawer__empty', 'Nothing waiting to collect at this house right now.'));
    } else {
      drawerBody.appendChild(sectionTitle('Awaiting pickup'));
      var meta = el('div', 'ls-drawer__meta');
      if (h.solari_display) {
        meta.appendChild(el('span', 'ls-drawer__chip ls-drawer__chip--solari', h.solari_display + ' Solari'));
      }
      var cnt = (h.item_count || 0);
      if (cnt) meta.appendChild(el('span', 'ls-drawer__chip', cnt + (cnt === 1 ? ' item' : ' items')));
      drawerBody.appendChild(meta);
      drawerBody.appendChild(el('p', 'ls-drawer__hint', 'Collect these in-game at the House ' + h.short + ' rep.'));
      drawerBody.appendChild(buildItemList(h, 0));
    }

    // --- This term's reward ladder (in-game board view) ---
    if (h.ladder && h.ladder.rewards && h.ladder.rewards.length) {
      drawerBody.appendChild(sectionTitle("This term's rewards"));
      drawerBody.appendChild(buildLadder(h.ladder, h.short));
    }

    // --- Previously collected (history; amount-0 rows the rep no longer shows) ---
    if (h.claimed && h.claimed.length) {
      drawerBody.appendChild(sectionTitle('Previously collected'));
      var cul = el('ul', 'ls-items ls-items--claimed');
      for (var ci = 0; ci < h.claimed.length; ci++) {
        var c = h.claimed[ci];
        var cli = el('li', 'ls-items__row ls-items__row--claimed');
        cli.appendChild(el('span', 'ls-items__name', c.name || ''));
        if (c.claimed_days != null) {
          cli.appendChild(el('span', 'ls-items__amt mono',
            c.claimed_days === 0 ? 'today' : c.claimed_days + 'd ago'));
        }
        cul.appendChild(cli);
      }
      drawerBody.appendChild(cul);
    }

    hideTooltip();
    lastFocused = ctx.tile;
    overlay.hidden = false;
    drawer.hidden = false;
    // allow the hidden->shown transition to animate
    window.requestAnimationFrame(function () {
      drawer.classList.add('ls-drawer--open');
      overlay.classList.add('ls-drawer-overlay--open');
    });
    if (drawerClose) drawerClose.focus();
  }

  function closeDrawer() {
    if (!drawer || drawer.hidden) return;
    drawer.classList.remove('ls-drawer--open');
    overlay.classList.remove('ls-drawer-overlay--open');
    window.setTimeout(function () {
      drawer.hidden = true;
      overlay.hidden = true;
    }, 200);
    if (lastFocused && lastFocused.focus) lastFocused.focus();
  }

  /* ---- wiring (event delegation on the board) ---- */
  board.addEventListener('mouseover', function (e) {
    var ctx = houseFor(e.target);
    if (ctx && ctx.data) showTooltip(ctx);
  });
  board.addEventListener('mouseout', function (e) {
    var to = e.relatedTarget;
    if (!to || !to.closest || !to.closest('.ls-tile')) hideTooltip();
  });
  board.addEventListener('click', function (e) {
    var ctx = houseFor(e.target);
    if (ctx && ctx.data) openDrawer(ctx);
  });
  // keyboard: tiles are <button>, so Enter/Space already fire click.

  if (drawerClose) drawerClose.addEventListener('click', closeDrawer);
  if (overlay) overlay.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeDrawer();
  });
  window.addEventListener('scroll', hideTooltip, true);
})();
