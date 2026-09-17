// V2 Players > Bases: client-side filter + detail drawer.
// Event-delegated so it survives HTMX swaps of #v2-bases-body (Refresh/Retry).
(function () {
  'use strict';

  function applyFilter() {
    var input = document.getElementById('v2-bases-filter');
    if (!input) return;
    var needle = input.value.trim().toLowerCase();
    var rows = document.querySelectorAll('#v2-bases-body .v2-bases-row');
    var shown = 0;
    rows.forEach(function (row) {
      var hay = row.getAttribute('data-search') || '';
      var match = !needle || hay.indexOf(needle) !== -1;
      row.hidden = !match;
      if (match) shown += 1;
    });
    var noMatch = document.getElementById('v2-bases-no-match');
    if (noMatch) noMatch.hidden = (rows.length === 0 || shown > 0);
  }

  function openDrawer() {
    var drawer = document.getElementById('v2-bases-drawer');
    if (!drawer) return;
    drawer.classList.add('v2-bases-drawer--open');
    drawer.setAttribute('aria-hidden', 'false');
  }

  function closeDrawer() {
    var drawer = document.getElementById('v2-bases-drawer');
    if (!drawer) return;
    drawer.classList.remove('v2-bases-drawer--open');
    drawer.setAttribute('aria-hidden', 'true');
  }

  document.addEventListener('input', function (e) {
    if (e.target && e.target.id === 'v2-bases-filter') applyFilter();
  });

  document.addEventListener('click', function (e) {
    var opener = e.target.closest ? e.target.closest('[data-bases-open]') : null;
    if (opener) { openDrawer(); return; }
    if (e.target && e.target.id === 'v2-bases-drawer-close') { closeDrawer(); return; }
    // Click on the overlay backdrop (the aside itself, not the panel) closes it.
    if (e.target && e.target.id === 'v2-bases-drawer') closeDrawer();
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeDrawer();
  });

  // Re-apply the active filter after the list fragment is swapped back in.
  document.body.addEventListener('htmx:afterSwap', function (e) {
    if (e.target && e.target.id === 'v2-bases-body') applyFilter();
  });
})();
