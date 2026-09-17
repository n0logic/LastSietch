/* portal-character.js — character stage + 3D gear viewer for /portal/account.
 *
 * The vitals card renders a two-column layout: stats list (left) and the
 * character stage (right) — an equipped-items rail, a turntable canvas, and an
 * item-details strip. Server supplies the items via a JSON <script
 * type="application/json" data-gear-items> embed (CSP-safe, non-executable):
 *   [{slot, name, template_id, category}]
 * Live equipped data lands with the gated collector update; until then the
 * server sends demo items under PORTAL_SHOW_GEAR_DEMO, or [] (placeholder).
 *
 * three.js loads lazily (dynamic import of portal-gear-viewer.js) only when an
 * item is shown: clicking a rail slot, or auto-selecting the first item on
 * desktop widths. Mobile never auto-loads 3D.
 *
 * Legacy [data-gear-3d-trigger]/[data-gear-card] buttons (other pages) keep
 * working via the same delegated handler as before.
 */
(function () {
  'use strict';

  var STATIC = '/admin/static/js/';
  var STATIC_DATA = '/admin/static/data/';
  // Rich item-stat sidecar (description / rarity / per-type stat block), keyed by
  // template_id under `.gear`. Extracted from the client DataTables; absent items
  // (e.g. ranged weapons, uncovered utilities) fall back to the basic meta.
  var _gearStatsPromise = null;
  var _gearStats = null;        // resolved map cache (null until first load / on fail)
  function fetchGearStats() {
    if (!_gearStatsPromise) {
      _gearStatsPromise = fetch(STATIC_DATA + 'gear-stats.json?v=' + ASSET_V,
        { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { _gearStats = (d && d.gear) || null; return _gearStats; })
        .catch(function () { return null; });
    }
    return _gearStatsPromise;
  }
  // Cache-bust the dynamic import — /admin/static/ ships no Cache-Control, so
  // an un-queried module URL gets heuristically pinned by the browser.
  var ASSET_V = '20260622d';
  var _viewerModule = null;

  function getViewerModule() {
    if (!_viewerModule) {
      _viewerModule = import(STATIC + 'portal-gear-viewer.js?v=' + ASSET_V);
    }
    return _viewerModule;
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  // ---- character stage (account page) -----------------------------------
  // Redesign: ONE persistent turntable figure (the body/anchor garment) stays
  // mounted; clicking a rail slot opens a stats popup over it instead of
  // swapping the model. The popup is pure DOM (rail data-attrs), so it works
  // even when no GLB resolved for the figure.
  function initCharacterStage() {
    var stage = document.querySelector('[data-character-stage]');
    if (!stage) return;

    var canvas = stage.querySelector('[data-gear-canvas]');
    var rail = stage.querySelector('[data-gear-rail]');
    if (!canvas || !rail) return;   // placeholder mode (no items) — nothing to wire
    var emptyMsg = stage.querySelector('[data-gear-empty]');
    var popup = stage.querySelector('[data-gear-popup]');
    var pSlot = stage.querySelector('[data-p-slot]');
    var pName = stage.querySelector('[data-p-name]');
    var pChips = stage.querySelector('[data-p-chips]');
    var pDesc = stage.querySelector('[data-p-desc]');
    var pStats = stage.querySelector('[data-p-stats]');
    var pMeta = stage.querySelector('[data-p-meta]');
    var popupClose = stage.querySelector('[data-gear-popup-close]');
    var activeBtn = null;   // guards the async gear-stats render against fast slot switches

    var handle = null;       // persistent viewer handle (created once)
    var figurePending = false;

    // Anchor = the full-body garment the turntable shows. Prefer a torso slot
    // (live feed "Chest"; demo "Body"), else the first equipped item.
    function pickAnchor() {
      var items = rail.querySelectorAll('[data-gear-item]');
      for (var i = 0; i < items.length; i++) {
        var s = (items[i].getAttribute('data-slot') || '').toLowerCase();
        if (s === 'chest' || s === 'body' || s === 'torso') return items[i];
      }
      return items[0] || null;
    }

    // Mount the persistent figure once. Idempotent: repeat calls (e.g. the
    // first mobile tap) are no-ops once a handle exists or a mount is in flight.
    function ensureFigure() {
      if (handle || figurePending) return;
      var anchor = pickAnchor();
      if (!anchor) return;
      var templateId = anchor.getAttribute('data-template-id');
      // data-dye carries the equipped piece's SwatchId — tints the figure by
      // the player's chosen dye when the swatch LUT has a matching entry.
      var swatchId = anchor.getAttribute('data-dye');
      figurePending = true;
      getViewerModule().then(function (mod) {
        return mod.mountGear(canvas, templateId, swatchId);
      }).then(function (h) {
        handle = h;
        canvas.classList.remove('is-empty');
        if (emptyMsg) emptyMsg.hidden = true;
      }).catch(function (err) {
        if (window.console) console.warn('gear figure failed for', templateId, err);
        canvas.classList.add('is-empty');
        if (emptyMsg) emptyMsg.hidden = false;   // popup stats still work
      }).then(function () { figurePending = false; });
    }

    function dlRow(parent, label, value) {
      if (value == null || value === '') return;
      parent.appendChild(el('dt', null, label));
      parent.appendChild(el('dd', null, String(value)));
    }

    function chip(text, mod) {
      return el('span', 'portal-charstage__chip' + (mod ? ' is-' + mod : ''), text);
    }

    // Real description? WIP / null / empty placeholders are treated as none.
    function realDesc(d) {
      if (!d) return '';
      var s = String(d).trim();
      return (s && s.toUpperCase() !== 'WIP') ? s : '';
    }

    // value + unit, with a signed percent for mitigation/cost stats (negatives
    // already carry their minus): "+6%", "-12%", "64/s", "525".
    function fmtStat(val, unit) {
      if (unit === '%') return (val > 0 ? '+' : '') + val + '%';
      return String(val) + (unit || '');
    }

    // Always-available bits (slot/name/grade/meta from rail data-attrs) render
    // synchronously; the rich block (rarity/description/stat block) fills in
    // from gear-stats.json once it resolves (memoised, so instant after first).
    function renderPopup(btn, info) {
      var tid = btn.getAttribute('data-template-id');
      var quality = btn.getAttribute('data-quality');

      if (pSlot) pSlot.textContent = (btn.getAttribute('data-slot') || '').toUpperCase();
      if (pName) pName.textContent = btn.getAttribute('data-item-name') || tid || '';

      if (pChips) {
        pChips.textContent = '';
        if (info && info.rarity) {
          pChips.appendChild(chip(info.rarity, info.rarity.toLowerCase()));
        }
        if (quality) pChips.appendChild(chip('Grade ' + quality, 'grade'));
      }

      if (pDesc) {
        var desc = realDesc(info && info.description);
        pDesc.textContent = desc;
        pDesc.hidden = !desc;
      }

      if (pStats) {
        pStats.textContent = '';
        if (info && info.stats) {
          var units = info.units || {};
          Object.keys(info.stats).forEach(function (label) {
            dlRow(pStats, label, fmtStat(info.stats[label], units[label]));
          });
        }
      }

      if (pMeta) {
        pMeta.textContent = '';
        dlRow(pMeta, 'Category', btn.getAttribute('data-category'));
        dlRow(pMeta, 'Variant', btn.getAttribute('data-variant'));
        dlRow(pMeta, 'Dye', btn.getAttribute('data-dye'));
        dlRow(pMeta, 'Template', tid);
      }
    }

    function openPopup(btn) {
      if (!popup) return;
      activeBtn = btn;
      rail.querySelectorAll('.portal-charstage__slot').forEach(function (b) {
        b.classList.toggle('is-active', b === btn);
      });
      // First pass with whatever stats are already cached (maybe null), then
      // overlay once the sidecar loads — guarded so a fast slot switch wins.
      var tid = btn.getAttribute('data-template-id');
      renderPopup(btn, _gearStats ? _gearStats[tid] : null);
      popup.hidden = false;
      fetchGearStats().then(function (gear) {
        if (activeBtn === btn && !popup.hidden) {
          renderPopup(btn, gear ? gear[tid] : null);
        }
      });
    }

    function closePopup() {
      activeBtn = null;
      if (popup) popup.hidden = true;
      rail.querySelectorAll('.portal-charstage__slot.is-active')
        .forEach(function (b) { b.classList.remove('is-active'); });
    }

    rail.addEventListener('click', function (ev) {
      var btn = ev.target.closest('[data-gear-item]');
      if (!btn) return;
      openPopup(btn);
      ensureFigure();   // first mobile tap loads the figure (desktop already has it)
    });

    if (popupClose) popupClose.addEventListener('click', closePopup);
    if (popup) popup.addEventListener('click', function (ev) {
      if (ev.target === popup) closePopup();   // backdrop tap dismisses
    });
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape' && popup && !popup.hidden) closePopup();
    });

    // Desktop: mount the figure immediately so the stage isn't an empty black
    // box. Mobile defers the 3D download to the first rail tap (data cost).
    if (window.matchMedia && window.matchMedia('(min-width: 920px)').matches) {
      ensureFigure();
    }

    window.addEventListener('pagehide', function () {
      if (handle) { handle.dispose(); handle = null; }
    });
  }

  // ---- legacy per-card trigger buttons (kept for other pages) ------------
  function ensureViewerPanel(card) {
    var existing = card.querySelector('.gear-viewer');
    if (existing) {
      return {
        panel: existing,
        canvas: existing.querySelector('.gear-viewer__canvas'),
        label: existing.querySelector('.gear-viewer__label'),
        setError: function (msg) {
          var err = existing.querySelector('.gear-viewer__error');
          if (err) { err.textContent = msg || ''; err.hidden = !msg; }
        },
      };
    }
    var panel = el('div', 'gear-viewer');
    panel.setAttribute('aria-label', '3D model viewer');
    panel.setAttribute('role', 'region');
    var label = el('p', 'gear-viewer__label', '');
    var canvas = document.createElement('canvas');
    canvas.className = 'gear-viewer__canvas';
    canvas.setAttribute('aria-hidden', 'true');
    var hint = el('p', 'gear-viewer__hint', 'Drag to orbit · scroll to zoom');
    var errEl = el('p', 'gear-viewer__error', '');
    errEl.hidden = true;
    panel.appendChild(label);
    panel.appendChild(canvas);
    panel.appendChild(hint);
    panel.appendChild(errEl);
    card.appendChild(panel);
    return {
      panel: panel, canvas: canvas, label: label,
      setError: function (msg) { errEl.textContent = msg || ''; errEl.hidden = !msg; },
    };
  }

  function mountGearCard(btn, card) {
    var templateId = btn.getAttribute('data-template-id');
    var itemLabel = btn.getAttribute('data-item-label') || templateId;
    if (!templateId) return;
    btn.disabled = true;
    btn.textContent = 'Loading…';
    var panelCtx = ensureViewerPanel(card);
    panelCtx.panel.classList.add('is-open');
    panelCtx.label.textContent = itemLabel;
    panelCtx.setError('');
    if (panelCtx.panel._gearHandle) {
      panelCtx.panel._gearHandle.dispose();
      panelCtx.panel._gearHandle = null;
    }
    getViewerModule().then(function (mod) {
      return mod.mountGear(panelCtx.canvas, templateId);
    }).then(function (handle) {
      panelCtx.panel._gearHandle = handle;
      btn.textContent = 'Close 3D';
      btn.disabled = false;
      btn.setAttribute('data-gear-open', 'true');
      panelCtx.canvas.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }).catch(function (err) {
      if (window.console) console.warn('gear viewer failed for', templateId, err);
      panelCtx.setError('3D model not available for this item yet.');
      btn.textContent = 'View in 3D';
      btn.disabled = false;
      panelCtx.panel.classList.remove('is-open');
    });
  }

  function closeGearCard(btn, card) {
    var panel = card.querySelector('.gear-viewer');
    if (!panel) return;
    if (panel._gearHandle) {
      panel._gearHandle.dispose();
      panel._gearHandle = null;
    }
    panel.classList.remove('is-open');
    btn.textContent = 'View in 3D';
    btn.removeAttribute('data-gear-open');
    btn.disabled = false;
  }

  function initGearTriggers() {
    document.addEventListener('click', function (ev) {
      var btn = ev.target.closest('[data-gear-3d-trigger]');
      if (!btn) return;
      var card = btn.closest('[data-gear-card]');
      if (!card) return;
      if (btn.getAttribute('data-gear-open')) closeGearCard(btn, card);
      else mountGearCard(btn, card);
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    initGearTriggers();
    initCharacterStage();
  });

  window.addEventListener('pagehide', function () {
    document.querySelectorAll('.gear-viewer').forEach(function (panel) {
      if (panel._gearHandle) {
        panel._gearHandle.dispose();
        panel._gearHandle = null;
      }
    });
  });
})();
