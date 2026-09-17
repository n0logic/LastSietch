/* ============================================================================
 * V2 Player Tools — tiny helper.
 * HTMX handles every fetch + swap. This file only wires:
 *   1. Audit-style expand toggle for the Grants tab (hidden JSON rows)
 *   2. Client-side sort for the Containers table (backend serves id-asc only)
 *   3. Sub-tab `active` class sync (HTMX swaps body only, not the nav buttons)
 *
 * Wired via event delegation on document body so the handlers survive every
 * HTMX swap without re-binding.
 * ============================================================================ */
(function () {

  /* --- Grant detail row expand/collapse --- */
  function toggleExpand(btn) {
    var targetId = btn.getAttribute('data-expand-target') || btn.getAttribute('aria-controls');
    if (!targetId) return;
    var row = document.getElementById(targetId);
    if (!row) return;
    var isOpen = btn.getAttribute('aria-expanded') === 'true';
    if (isOpen) {
      row.setAttribute('hidden', '');
      btn.setAttribute('aria-expanded', 'false');
      btn.textContent = '▸';
    } else {
      row.removeAttribute('hidden');
      btn.setAttribute('aria-expanded', 'true');
      btn.textContent = '▾';
    }
  }

  /* --- Containers table client-side sort --- */
  function sortContainers(th) {
    var table = th.closest('table[data-sortable="true"]');
    if (!table) return;
    var key = th.getAttribute('data-sort-key');
    if (!key) return;
    var current = th.getAttribute('aria-sort') || 'none';
    var nextDir = (current === 'ascending') ? 'descending' : 'ascending';

    // Reset other headers
    var headers = table.querySelectorAll('th[data-sort-key]');
    headers.forEach(function (h) {
      h.setAttribute('aria-sort', 'none');
      var arrow = h.querySelector('.v2-player-containers__sort-arrow');
      if (arrow) arrow.textContent = '';
    });
    th.setAttribute('aria-sort', nextDir);
    var thisArrow = th.querySelector('.v2-player-containers__sort-arrow');
    if (thisArrow) thisArrow.textContent = (nextDir === 'ascending') ? '▲' : '▼';

    var tbody = table.querySelector('tbody');
    if (!tbody) return;
    var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
    var attr = 'data-sort-' + key;
    var asNumber = th.getAttribute('data-sort-type') === 'number';

    rows.sort(function (a, b) {
      var av = a.querySelector('[' + attr + ']');
      var bv = b.querySelector('[' + attr + ']');
      var ax = av ? av.getAttribute(attr) : '';
      var bx = bv ? bv.getAttribute(attr) : '';
      if (asNumber) {
        ax = parseFloat(ax) || 0;
        bx = parseFloat(bx) || 0;
        return (nextDir === 'ascending') ? (ax - bx) : (bx - ax);
      }
      ax = (ax || '').toLowerCase();
      bx = (bx || '').toLowerCase();
      if (ax < bx) return (nextDir === 'ascending') ? -1 : 1;
      if (ax > bx) return (nextDir === 'ascending') ? 1 : -1;
      return 0;
    });
    rows.forEach(function (r) { tbody.appendChild(r); });
  }

  /* --- Sub-tab active-class sync --- */
  function setActiveSubtab(btn) {
    var bar = btn.closest('.v2-player-drilldown__subtabs');
    if (!bar) return;
    var siblings = bar.querySelectorAll('.v2-player-drilldown__subtab');
    for (var i = 0; i < siblings.length; i++) {
      siblings[i].classList.remove('active');
      siblings[i].setAttribute('aria-selected', 'false');
    }
    btn.classList.add('active');
    btn.setAttribute('aria-selected', 'true');
  }

  function syncSubtabFromUrl() {
    var bar = document.querySelector('.v2-player-drilldown__subtabs');
    if (!bar) return;
    var match = window.location.search.match(/[?&]tab=([^&]+)/);
    var tab = match ? decodeURIComponent(match[1]) : 'identity';
    var btn = bar.querySelector('.v2-player-drilldown__subtab[data-subtab="' + tab + '"]');
    if (btn) setActiveSubtab(btn);
  }

  /* --- Containers master/detail: highlight selected row --- */
  function setSelectedContainerRow(row) {
    var tbody = row.parentNode;
    if (!tbody) return;
    var rows = tbody.querySelectorAll('tr[data-container-id]');
    for (var i = 0; i < rows.length; i++) rows[i].classList.remove('is-selected');
    row.classList.add('is-selected');
  }

  /* --- DM (whisper) composer ---
   * Reuses the shipped whisper path (POST /api/dune/chat/send) + the online
   * player picker (/api/dune/chat/players, which carries funcom_id). The
   * drilldown context has no funcom_id, so on open we match this player against
   * the online list by name; if that fails (offline, duplicate, or picker
   * error) we fall back to a manual FuncomId input. Whisper reaches online
   * players only — that constraint is surfaced, not hidden. */
  var FUNCOM_ID_RE = /^[^\s#]{1,32}#[0-9]{1,10}$/;
  var dmState = { funcomId: null };

  function dmEl(id) { return document.getElementById(id); }

  function closeV2Dm() {
    var o = dmEl('v2-dm-overlay');
    if (o) o.classList.remove('active');
  }
  window.closeV2Dm = closeV2Dm;

  function dmRecipient() {
    var manualRow = dmEl('v2-dm-manual-row');
    if (manualRow && !manualRow.hidden) {
      var v = (dmEl('v2-dm-recipient').value || '').trim();
      if (v) return v;
    }
    return dmState.funcomId;
  }

  function openV2Dm() {
    var actions = dmEl('v2-player-actions');
    if (!actions) return;
    var name = actions.getAttribute('data-player-name') || 'player';
    var online = actions.getAttribute('data-online') === '1';
    dmState.funcomId = null;

    dmEl('v2-dm-title').textContent = 'Whisper ' + name;
    dmEl('v2-dm-message').value = '';
    dmEl('v2-dm-charcount').textContent = '0';
    dmEl('v2-dm-recipient').value = '';
    dmEl('v2-dm-manual-row').hidden = true;
    hideMsg('v2-dm-msg');
    var recipLine = dmEl('v2-dm-recipient-line');
    var sendBtn = dmEl('v2-dm-send');
    recipLine.textContent = 'Resolving recipient…';
    sendBtn.disabled = true;
    dmEl('v2-dm-overlay').classList.add('active');

    apiCall('GET', '/admin/api/dune/chat/players').then(function (d) {
      var players = (d.players || []).filter(function (p) { return p.funcom_id; });
      var lname = name.toLowerCase();
      var matches = players.filter(function (p) { return (p.name || '').toLowerCase() === lname; });
      if (matches.length === 1) {
        dmState.funcomId = matches[0].funcom_id;
        recipLine.textContent = 'To ' + matches[0].name + ' (' + matches[0].funcom_id + ')';
      } else {
        if (matches.length > 1) {
          recipLine.textContent = 'Multiple online players named "' + name + '" — enter the exact FuncomId:';
        } else if (online) {
          recipLine.textContent = 'Could not match "' + name + '" in the online list — enter their FuncomId:';
        } else {
          recipLine.textContent = name + ' is not currently online. Whispers reach online players only — enter a FuncomId to try anyway:';
        }
        dmEl('v2-dm-manual-row').hidden = false;
      }
      sendBtn.disabled = false;
    }).catch(function () {
      recipLine.textContent = 'Could not load the online-player list — enter a FuncomId:';
      dmEl('v2-dm-manual-row').hidden = false;
      sendBtn.disabled = false;
    });
  }

  function sendV2Dm() {
    hideMsg('v2-dm-msg');
    var recipient = dmRecipient();
    var message = dmEl('v2-dm-message').value;
    if (!recipient || !FUNCOM_ID_RE.test(recipient)) {
      showMsg('v2-dm-msg', 'A valid recipient FuncomId (Display#tag) is required.', 'error');
      return;
    }
    if (!message.trim()) {
      showMsg('v2-dm-msg', 'Message is required.', 'error');
      return;
    }
    showConfirm('Send this whisper LIVE to ' + recipient + '?', function () {
      showMsg('v2-dm-msg', 'Sending…', 'info');
      apiCall('POST', '/admin/api/dune/chat/send', {
        scope: 'whisper', recipient: recipient, message: message, mode: 'apply'
      }).then(function (d) {
        var ok = d && d.success;
        var detail = (d && d.results && d.results[0] && d.results[0].detail) || 'unknown error';
        showMsg('v2-dm-msg', ok ? 'Whisper sent.' : ('Failed: ' + detail), ok ? 'success' : 'error');
        if (ok) { dmEl('v2-dm-message').value = ''; dmEl('v2-dm-charcount').textContent = '0'; }
      }).catch(function (e) {
        showMsg('v2-dm-msg', 'Error: ' + e.message, 'error');
      });
    });
  }

  /* Static modal elements (present on full-page render, not HTMX-swapped). */
  var dmMessageEl = document.getElementById('v2-dm-message');
  if (dmMessageEl) {
    dmMessageEl.addEventListener('input', function () {
      var c = document.getElementById('v2-dm-charcount');
      if (c) c.textContent = String(this.value.length);
    });
  }

  document.addEventListener('click', function (e) {
    if (e.target.closest('#v2-action-dm')) {
      e.preventDefault();
      openV2Dm();
      return;
    }
    if (e.target.closest('#v2-dm-send')) {
      e.preventDefault();
      sendV2Dm();
      return;
    }
    var expandBtn = e.target.closest('.audit-row__expand-btn');
    if (expandBtn && expandBtn.hasAttribute('data-expand-target')) {
      e.preventDefault();
      toggleExpand(expandBtn);
      return;
    }
    var sortBtn = e.target.closest('.v2-player-containers__sort-btn');
    if (sortBtn) {
      var th = sortBtn.closest('th[data-sort-key]');
      if (th) {
        e.preventDefault();
        sortContainers(th);
      }
      return;
    }
    var subtabBtn = e.target.closest('.v2-player-drilldown__subtab[data-subtab]');
    if (subtabBtn) {
      setActiveSubtab(subtabBtn);
      /* don't preventDefault — HTMX still fires the GET + body swap */
      return;
    }
    var containerRow = e.target.closest('tr[data-container-id]');
    if (containerRow) {
      setSelectedContainerRow(containerRow);
      /* don't preventDefault — HTMX fires the GET for items pane */
    }
  });

  /* Keyboard Enter on container row mirrors the click visual update
     (HTMX already fires the GET via hx-trigger; browsers don't synthesize
     a click for Enter on tr[role=button], so the selected-row outline
     would otherwise lag the GET). */
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      var dmOverlay = document.getElementById('v2-dm-overlay');
      if (dmOverlay && dmOverlay.classList.contains('active')) { closeV2Dm(); return; }
    }
    if (e.key !== 'Enter') return;
    var containerRow = e.target.closest('tr[data-container-id]');
    if (containerRow) setSelectedContainerRow(containerRow);
  });

  /* --- Instant client-side filter for the container list + the item rows.
     Event-delegated so it keeps working after HTMX swaps the fragment in.
     No server round-trip: type-to-filter the already-loaded rows. --- */
  function applyRowFilter(input, rowSelector, countClass) {
    var q = (input.value || '').trim().toLowerCase();
    var rows = document.querySelectorAll(rowSelector);
    var shown = 0;
    rows.forEach(function (r) {
      var hay = r.getAttribute('data-filter') || '';
      var match = !q || hay.indexOf(q) !== -1;
      r.style.display = match ? '' : 'none';
      if (match) shown++;
    });
    var row = input.closest('.v2-player-containers__filter-row, .v2-player-container-items__filter-row');
    var label = row ? row.querySelector('.' + countClass) : null;
    if (label) label.textContent = q ? (shown + ' / ' + rows.length) : '';
  }

  document.addEventListener('input', function (e) {
    if (e.target.classList && e.target.classList.contains('v2-container-filter')) {
      applyRowFilter(e.target, '.v2-player-containers__table tbody tr[data-container-id]', 'v2-container-filter-count');
    } else if (e.target.classList && e.target.classList.contains('v2-items-filter')) {
      applyRowFilter(e.target, '.v2-player-container-items tbody tr[data-filter]', 'v2-items-filter-count');
    }
  });

  /* Browser back/forward through hx-push-url history — re-sync active class. */
  window.addEventListener('popstate', syncSubtabFromUrl);
})();
