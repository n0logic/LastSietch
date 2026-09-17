/* W6: Spice-spawn toggle JS (VC2 P2).
 *
 * Vanilla JS, no new deps. Fetches the 8 dune.spicefield_types rows, renders a
 * boolean toggle per row, and POSTs flips back with the CSRF header. Optimistic
 * UI: flip the checkbox immediately, then re-fetch on response (or revert on
 * error). Online-safe per Decision A; off only suppresses the next spawn-tick.
 */

(function () {
  'use strict';

  var LIST_URL = '/admin/api/dune/v2/spice/types';
  var TOGGLE_BASE = '/admin/api/dune/v2/spice/types/';

  var bodyEl = null;
  var statusEl = null;
  var tsEl = null;

  function setStatus(text, kind) {
    if (!statusEl) return;
    statusEl.textContent = text || '';
    statusEl.className = 'spice-status' + (kind ? ' spice-status--' + kind : '');
  }

  function markRefreshed() {
    if (!tsEl) return;
    var now = new Date();
    tsEl.dataset.ts = String(Math.floor(now.getTime() / 1000));
    tsEl.textContent = 'refreshed ' + now.toLocaleTimeString();
  }

  function renderRows(types) {
    bodyEl.innerHTML = '';
    if (!types || !types.length) {
      var empty = document.createElement('tr');
      empty.className = 'spice-table__empty';
      var td = document.createElement('td');
      td.colSpan = 5;
      td.textContent = 'No spice field types found.';
      empty.appendChild(td);
      bodyEl.appendChild(empty);
      return;
    }
    types.forEach(function (t) {
      var tr = document.createElement('tr');
      tr.className = 'spice-table__row';
      tr.dataset.typeId = String(t.id);

      tr.appendChild(cell(t.field_type));
      tr.appendChild(cell(t.map_name));
      tr.appendChild(cell(t.dimension_index));
      tr.appendChild(cell(t.current_globally_active));

      var toggleTd = document.createElement('td');
      var label = document.createElement('label');
      label.className = 'spice-toggle';
      var input = document.createElement('input');
      input.type = 'checkbox';
      input.className = 'spice-toggle__input';
      input.checked = !!t.is_spawning_active;
      input.dataset.typeId = String(t.id);
      input.addEventListener('change', onToggle);
      var slider = document.createElement('span');
      slider.className = 'spice-toggle__slider';
      label.appendChild(input);
      label.appendChild(slider);
      toggleTd.appendChild(label);
      tr.appendChild(toggleTd);

      bodyEl.appendChild(tr);
    });
  }

  function cell(value) {
    var td = document.createElement('td');
    td.textContent = (value === null || value === undefined) ? '-' : String(value);
    return td;
  }

  function fetchList() {
    fetch(LIST_URL, { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data && data.available === false) {
          setStatus(data.error || 'Spice data unavailable', 'err');
          return;
        }
        renderRows(data.types || []);
        markRefreshed();
        if (data && data.stale) setStatus('Showing cached data (relay unreachable)', 'err');
        else setStatus('', null);
      })
      .catch(function (err) {
        setStatus(String(err && err.message || err), 'err');
      });
  }

  function onToggle(e) {
    var input = e.currentTarget;
    var typeId = input.dataset.typeId;
    var newValue = input.checked;

    input.disabled = true;
    setStatus('Saving…', null);

    fetch(TOGGLE_BASE + encodeURIComponent(typeId) + '/spawning', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': (typeof csrfToken !== 'undefined') ? csrfToken : '',
      },
      body: JSON.stringify({ new_value: newValue }),
    })
      .then(function (r) { return r.json().then(function (data) { return { ok: r.ok, data: data }; }); })
      .then(function (res) {
        if (!res.ok || !res.data || res.data.ok === false) {
          var msg = (res.data && (res.data.detail || res.data.error)) || 'Toggle failed';
          setStatus(msg, 'err');
          input.checked = !newValue;  // revert optimistic flip
          input.disabled = false;
          return;
        }
        setStatus('Spawning ' + (newValue ? 'enabled' : 'disabled') + ' for type ' + typeId, 'ok');
        input.disabled = false;
        // Re-fetch to reflect the source-of-truth boolean + live counts.
        fetchList();
      })
      .catch(function (err) {
        setStatus(String(err && err.message || err), 'err');
        input.checked = !newValue;
        input.disabled = false;
      });
  }

  function init() {
    bodyEl = document.getElementById('spice-table-body');
    statusEl = document.getElementById('spice-status');
    tsEl = document.getElementById('panel-spice-ts');
    if (!bodyEl) return;  // panel not on this page
    fetchList();
  }

  document.addEventListener('DOMContentLoaded', init);
}());
