/* portal-solido.js — blueprint market gallery + detail viewer + My Bases.
 *
 * Loaded on /portal/solido, /portal/solido/{id}, and /portal/my-bases. The heavy
 * three.js viewer lives in portal-solido-viewer.js and is dynamic-import()ed only
 * when a 3D view is actually opened (Preview on My Bases, or "View in 3D" on a
 * detail page) — never on the gallery list view. Keep that gate (QA: network tab).
 *
 * State-changing POSTs (publish/unpublish) go through window.PortalAPI.fetch,
 * which injects the ls_portal_csrf cookie as X-Portal-CSRF-Token.
 *
 * Real-mesh toggle: after the viewer mounts, a Boxes / Real Meshes toggle appears
 * when the glb-manifest reports any pieces available for this blueprint. The toggle
 * calls viewer.switchMode() which loads GLBs lazily and falls back to boxes per
 * unmatched piece. Mode is disabled (and a note shown) above REAL_MESH_WARN_CAP
 * pieces on mobile or when the manifest has no matching entries.
 */
(function () {
  'use strict';

  var STATIC = '/admin/static/js/';
  // Version for dynamically import()ed modules. The edge serves /admin/static/
  // with no Cache-Control, so browsers heuristically pin un-queried module URLs
  // ~forever (the 2026-06-12 "fixes don't show up" bug). Bump alongside the
  // template ?v= of this file whenever the viewer module changes.
  var ASSET_V = '20260612q';

  var IS_TOUCH = ('ontouchstart' in window) ||
    (window.matchMedia && window.matchMedia('(pointer: coarse)').matches);

  // Blueprint display-name cap; must match dune-blueprint-rename.py NAME_MAX /
  // solido.RENAME_NAME_MAX.
  var RENAME_MAX = 40;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function fmt(n) {
    n = Number(n) || 0;
    return n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k' : String(n);
  }

  // Shared catalog fetch (memoized) for any viewer mount on the page.
  var _catalogPromise = null;
  function catalog() {
    if (!_catalogPromise) {
      _catalogPromise = fetch('/admin/static/js/data/solido-piece-catalog.json',
        { credentials: 'same-origin' }).then(function (r) { return r.json(); });
    }
    return _catalogPromise;
  }

  // ---- gallery (/portal/solido) -----------------------------------------
  function initGallery(root) {
    var grid = root.querySelector('[data-solido-grid]');
    var status = root.querySelector('[data-solido-status]');
    var loadMore = root.querySelector('[data-solido-loadmore]');
    var tagInput = root.querySelector('[data-solido-tagfilter]');
    if (!grid) return;

    var limit = parseInt(root.getAttribute('data-limit'), 10) || 24;
    var linked = root.getAttribute('data-linked') === '1';
    var state = {
      sort: root.getAttribute('data-sort') || 'new',
      tag: root.getAttribute('data-tag') || '',
      offset: parseInt(root.getAttribute('data-loaded'), 10) || 0,
      busy: false,
    };

    function setStatus(msg) {
      if (!status) return;
      status.textContent = msg || '';
      status.hidden = !msg;
    }

    function card(bp, linked) {
      var article = el('article', 'solido-card');
      article.setAttribute('data-publish-id', bp.publish_id);

      var a = el('a', 'solido-card__link');
      a.href = '/portal/solido/' + bp.publish_id;

      var art = el('div', 'solido-card__art' + (bp.thumb_url ? '' : ' is-empty'));
      art.setAttribute('data-faction', bp.faction || 'neutral');
      if (bp.thumb_url) {
        var shot = el('img', 'solido-card__shot');
        shot.src = bp.thumb_url;
        shot.alt = '';
        shot.loading = 'lazy';
        art.appendChild(shot);
      }
      if (bp.has_paid_pieces) {
        art.appendChild(el('span', 'solido-card__badge solido-card__badge--mtx', 'MTX'));
      }
      a.appendChild(art);

      var body = el('div', 'solido-card__body');
      body.appendChild(el('h3', 'solido-card__title', bp.title));
      var meta = el('div', 'solido-card__meta');
      meta.appendChild(el('span', 'solido-card__author', 'by ' + (bp.author_name || '')));
      var stats = el('span', 'solido-card__stats');
      stats.appendChild(el('span', null, (bp.piece_count || 0) + ' pcs'));
      stats.appendChild(el('span', null, '↓ ' + (bp.download_count || 0)));
      meta.appendChild(stats);
      body.appendChild(meta);
      if (bp.tags && bp.tags.length) {
        var tags = el('div', 'solido-card__tags');
        bp.tags.forEach(function (t) { tags.appendChild(el('span', 'solido-chip', t)); });
        body.appendChild(tags);
      }
      a.appendChild(body);
      article.appendChild(a);

      if (linked) {
        var save = el('a', 'solido-card__save portal-btn portal-btn--sm', 'Save');
        save.href = '/portal/solido/import?from=' + bp.publish_id +
          '&title=' + encodeURIComponent(bp.title || '');
        save.title = 'Save this blueprint to your character';
        article.appendChild(save);
      }
      return article;
    }

    function load(reset) {
      if (state.busy) return;
      state.busy = true;
      if (loadMore) loadMore.disabled = true;
      if (reset) { state.offset = 0; setStatus('Loading…'); }
      var url = '/portal/api/solido/market?sort=' + encodeURIComponent(state.sort) +
        '&tag=' + encodeURIComponent(state.tag) +
        '&limit=' + limit + '&offset=' + state.offset;
      fetch(url, { credentials: 'same-origin' })
        .then(function (r) { if (!r.ok) throw new Error('market ' + r.status); return r.json(); })
        .then(function (data) {
          var rows = (data && data.listings) || [];
          if (reset) grid.textContent = '';
          rows.forEach(function (bp) { grid.appendChild(card(bp, linked)); });
          state.offset += rows.length;
          if (loadMore) loadMore.hidden = !(data && data.has_more);
          if (reset && !rows.length) {
            setStatus('No blueprints match that filter yet.');
          } else {
            setStatus('');
          }
        })
        .catch(function (err) {
          if (window.console) console.warn('gallery load failed', err);
          setStatus('Could not load the market — try again shortly.');
        })
        .then(function () {
          state.busy = false;
          if (loadMore) loadMore.disabled = false;
        });
    }

    root.querySelectorAll('[data-solido-sort]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var next = btn.getAttribute('data-solido-sort');
        if (next === state.sort) return;
        state.sort = next;
        root.querySelectorAll('[data-solido-sort]').forEach(function (b) {
          var on = b === btn;
          b.classList.toggle('is-active', on);
          b.setAttribute('aria-selected', on ? 'true' : 'false');
        });
        load(true);
      });
    });

    if (tagInput) {
      var tagTimer = null;
      tagInput.addEventListener('input', function () {
        if (tagTimer) clearTimeout(tagTimer);
        tagTimer = setTimeout(function () {
          state.tag = tagInput.value.trim();
          load(true);
        }, 300);
      });
    }

    if (loadMore) {
      loadMore.addEventListener('click', function () { load(false); });
    }
  }

  // ---- in-canvas viewer chrome (selection, breakdown, controls hint) -------
  // Builds overlays anchored to the stage element (the canvas's parent, made
  // position:relative in CSS). Returns an API the mount + toggle wire into.
  function buildChrome(stage) {
    if (!stage || stage.querySelector('.solido-overlay')) {
      return stage && stage.__chrome;   // reuse if already built
    }

    // Selected-piece readout. Lives INSIDE the Clay/Textured toggle bar (right
    // side) rather than floating over the canvas — out of the model's way, and
    // the bar shares the selection's visibility condition (real-mesh mode).
    // Attached via attachSelBar() once wireRealMeshToggle builds the bar.
    var sel = el('div', 'solido-selbar');
    sel.hidden = true;
    var selCat = el('span', 'solido-selbar__cat');
    var selName = el('span', 'solido-selbar__name');
    var selMeta = el('span', 'solido-selbar__meta');
    var walkBtn = el('button', 'solido-overlay__walkbtn solido-selbar__walk', 'Walk from here');
    walkBtn.type = 'button';
    walkBtn.hidden = true;
    sel.appendChild(selCat); sel.appendChild(selName); sel.appendChild(selMeta);
    sel.appendChild(walkBtn);

    // Piece breakdown (top-right), collapsible.
    var bd = el('div', 'solido-overlay solido-overlay--breakdown');
    bd.hidden = true;
    var bdHead = el('div', 'solido-overlay__head', 'Piece breakdown');
    var bdList = el('ul', 'solido-overlay__list');
    bd.appendChild(bdHead); bd.appendChild(bdList);

    // Controls hint (bottom-right).
    var hint = el('div', 'solido-overlay solido-overlay--hint');
    hint.innerHTML = 'Drag — rotate · Right-drag — pan · Scroll — zoom · Click — select piece';
    hint.hidden = true;

    // Walkthrough HUD (crosshair + hint + exit/fullscreen + mobile joystick).
    // Hidden until a walk starts; buttons matter most on touch (desktop runs
    // pointer-locked, so its exits are the Esc key and F for fullscreen).
    var hud = el('div', 'solido-walkhud');
    hud.hidden = true;
    var exitBtn = el('button', 'solido-walkhud__btn solido-walkhud__btn--exit', '✕ Exit tour');
    exitBtn.type = 'button';
    var fsBtn = el('button', 'solido-walkhud__btn solido-walkhud__btn--fs', '⛶ Fullscreen');
    fsBtn.type = 'button';
    var cross = el('div', 'solido-walkhud__cross');
    var whint = el('div', 'solido-walkhud__hint', IS_TOUCH
      ? 'Left pad — move · drag — look'
      : 'WASD — move · mouse — look · Shift — run · F — fullscreen · Esc — exit');
    var joy = el('div', 'solido-walkhud__joy');
    var nub = el('div', 'solido-walkhud__nub');
    joy.appendChild(nub);
    joy.hidden = !IS_TOUCH;
    // Paused state (pointer lock lost: Esc, click on another window, alt-tab).
    // The pointer is free here, so these buttons are clickable — the explicit
    // way back to the orbit view without losing your spot by accident.
    var pause = el('div', 'solido-walkhud__pause');
    pause.hidden = true;
    pause.appendChild(el('div', 'solido-walkhud__pause-title', 'Tour paused'));
    var resumeBtn = el('button', 'solido-walkhud__btn solido-walkhud__btn--resume', '▶ Resume walking');
    resumeBtn.type = 'button';
    var orbitBtn = el('button', 'solido-walkhud__btn solido-walkhud__btn--orbit', 'Back to orbit view');
    orbitBtn.type = 'button';
    var pauseRow = el('div', 'solido-walkhud__pause-row');
    pauseRow.appendChild(resumeBtn); pauseRow.appendChild(orbitBtn);
    pause.appendChild(pauseRow);
    hud.appendChild(exitBtn); hud.appendChild(fsBtn); hud.appendChild(cross);
    hud.appendChild(whint); hud.appendChild(joy); hud.appendChild(pause);

    // Drop the breakdown panel below the toolbar stack (render-mode + material
    // bars sit at the stage top and grew a selection readout, so a fixed CSS
    // top overlaps them; measure the last bar instead).
    function bdFit() {
      var bars = stage.querySelector('.solido-mesh-toolbars');
      if (bars) bd.style.top = (bars.offsetTop + bars.offsetHeight + 10) + 'px';
    }

    // Anchor the HUD to the canvas box (the stage also contains the toggle bars
    // above the canvas, so inset:0 would float the buttons off the 3D view).
    function hudFit() {
      var cv = stage.querySelector('canvas');
      if (!cv) return;
      hud.style.top = cv.offsetTop + 'px';
      hud.style.left = cv.offsetLeft + 'px';
      hud.style.width = cv.offsetWidth + 'px';
      hud.style.height = cv.offsetHeight + 'px';
    }
    window.addEventListener('resize', function () { if (!hud.hidden) hudFit(); });

    stage.appendChild(bd); stage.appendChild(hint);
    stage.appendChild(hud);

    // Fullscreen: native on the stage element, CSS takeover where the API is
    // missing (iPhone Safari only fullscreens <video>).
    function fsToggle() {
      if (document.fullscreenElement === stage) {
        document.exitFullscreen();
      } else if (stage.classList.contains('solido-stage--fs')) {
        stage.classList.remove('solido-stage--fs');
        document.documentElement.classList.remove('solido-fs-lock');
      } else if (stage.requestFullscreen) {
        stage.requestFullscreen().catch(function () { fsCssOn(); });
      } else {
        fsCssOn();
      }
    }
    function fsCssOn() {
      stage.classList.add('solido-stage--fs');
      document.documentElement.classList.add('solido-fs-lock');
    }
    document.addEventListener('fullscreenchange', function () {
      fsBtn.textContent = document.fullscreenElement === stage ? '⛶ Exit fullscreen' : '⛶ Fullscreen';
      if (!hud.hidden) setTimeout(hudFit, 50);   // canvas box moves on fs toggle
    });
    fsBtn.addEventListener('click', fsToggle);

    var api = {
      _realOn: false,
      attachSelBar: function (bar) {
        if (sel.parentNode !== bar) bar.appendChild(sel);
      },
      showSelection: function (info) {
        if (!info) { sel.hidden = true; return; }
        selCat.textContent = (info.category || 'Piece').toUpperCase();
        selName.textContent = info.building_type || '';
        var coords = [info.x, info.y, info.z].map(function (n) {
          return (Math.round(Number(n) * 10) / 10).toLocaleString();
        }).join(', ');
        var rot = '↻ ' + (info.rotation || 0) + '°';
        if (info.pitch) rot += ' ⤢ ' + info.pitch + '°';
        if (info.roll) rot += ' ⟳ ' + info.roll + '°';
        selMeta.textContent = rot + '  ·  ' + coords;
        walkBtn.hidden = !(api._realOn && api._walkHandle);
        sel.hidden = false;
      },
      renderBreakdown: function (list) {
        bdList.textContent = '';
        (list || []).forEach(function (row) {
          var li = el('li', 'solido-overlay__row');
          li.appendChild(el('span', 'solido-overlay__row-label', row.label));
          li.appendChild(el('span', 'solido-overlay__row-count', '×' + row.count));
          bdList.appendChild(li);
        });
        bd.hidden = !(list && list.length);
        bdFit();
      },
      setRealActive: function (on) {
        api._realOn = on;
        hint.hidden = !on;          // hint + selection only meaningful in real mode
        if (!on) sel.hidden = true;
        bdFit();
      },
      bindWalk: function (handle) {
        if (!handle || !handle.startWalk) { api._walkHandle = null; return; }
        api._walkHandle = handle;
        walkBtn.onclick = function () { handle.startWalk(); };
        exitBtn.onclick = function () { handle.stopWalk(); };
        orbitBtn.onclick = function () { handle.stopWalk(); };
        resumeBtn.onclick = function () { if (handle.resumeWalk) handle.resumeWalk(); };
        wireJoystick(joy, nub, function (x, y) { handle.setWalkJoystick(x, y); });
      },
      setWalkActive: function (on) {
        hud.hidden = !on;
        if (on) hudFit(); else pause.hidden = true;
        stage.classList.toggle('is-walking', on);
      },
      setWalkPaused: function (on) {
        pause.hidden = !on;
        cross.hidden = on;
      },
      requestFs: fsToggle,
    };
    stage.__chrome = api;
    return api;
  }

  // Virtual joystick: pointer-captured drag on the pad → normalized (x, y)
  // with y+ = forward. Returns the vector to (0, 0) on release.
  function wireJoystick(pad, nub, onVec) {
    pad.__onVec = onVec;          // refreshed on every bindWalk (new viewer handle)
    if (pad.__wired) return;
    pad.__wired = true;
    onVec = null;                 // listeners below must use pad.__onVec
    var pid = null, cx = 0, cy = 0, R = 40;
    pad.addEventListener('pointerdown', function (ev) {
      pid = ev.pointerId;
      try { pad.setPointerCapture(pid); } catch (e) {}
      var r = pad.getBoundingClientRect();
      cx = r.left + r.width / 2; cy = r.top + r.height / 2;
      move(ev);
      ev.preventDefault();
    });
    function move(ev) {
      if (ev.pointerId !== pid) return;
      var dx = ev.clientX - cx, dy = ev.clientY - cy;
      var d = Math.hypot(dx, dy) || 1, c = Math.min(1, d / R);
      var nx = (dx / d) * c, ny = (dy / d) * c;
      nub.style.transform = 'translate(' + (nx * R) + 'px,' + (ny * R) + 'px)';
      pad.__onVec(nx, -ny);
    }
    pad.addEventListener('pointermove', move);
    function end(ev) {
      if (ev.pointerId !== pid) return;
      pid = null;
      nub.style.transform = '';
      pad.__onVec(0, 0);
    }
    pad.addEventListener('pointerup', end);
    pad.addEventListener('pointercancel', end);
  }

  // ---- shared 3D mount --------------------------------------------------
  // Lazily imports the viewer + fetches a blueprint JSON, mounts it on `canvas`,
  // and reports a one-line stats note. `url` returns either the blueprint dict
  // or { available:false }.
  // stageEl: optional element (canvas parent) to inject toggle + overlays into.
  function mountViewer(canvas, url, setNote, stageEl) {
    var chrome = stageEl ? buildChrome(stageEl) : null;
    if (chrome) { chrome.setRealActive(false); chrome.showSelection(null); chrome.renderBreakdown([]); }
    return Promise.all([
      fetch(url, { credentials: 'same-origin' })
        .then(function (r) { if (!r.ok) throw new Error('blueprint ' + r.status); return r.json(); }),
      catalog(),
      import(STATIC + 'portal-solido-viewer.js?v=' + ASSET_V),
    ]).then(function (res) {
      var bp = res[0], cat = res[1], mod = res[2];
      if (bp && bp.available === false) {
        throw new Error('blob unavailable');
      }
      var handle = mod.mount(canvas, bp, cat, {
        onSelect: function (info) { if (chrome) chrome.showSelection(info); },
        onWalkState: function (on) { if (chrome) chrome.setWalkActive(on); },
        onWalkPause: function (on) { if (chrome) chrome.setWalkPaused(on); },
        onWalkRequestFs: function () { if (chrome) chrome.requestFs(); },
      });
      if (chrome) chrome.bindWalk(handle);
      var st = handle.stats || {};
      var bits = [fmt(st.rendered_pieces) + ' pieces'];
      if (st.capped) bits.push('large — simplified');
      if (st.pentashields_skipped) bits.push(st.pentashields_skipped + ' Pentashields not shown');
      setNote(bits.join(' · '));

      if (chrome) chrome.renderBreakdown(handle.getBreakdown && handle.getBreakdown());

      // Wire the real-mesh toggle after manifest loads (non-blocking).
      if (stageEl && handle.getManifest) {
        handle.getManifest().then(function (manifest) {
          wireRealMeshToggle(stageEl, handle, manifest, bp, bits, chrome);
        }).catch(function () { /* no manifest — boxes only */ });
      }

      return handle;
    });
  }

  // ---- real-mesh mode toggle ------------------------------------------------
  // Checks if the manifest has any pieces that appear in the blueprint, then
  // renders a toggle button. Hidden when no GLBs are available for this blueprint.
  function wireRealMeshToggle(container, handle, manifest, bp, baseBits, chrome) {
    if (!manifest || !manifest.pieces) return;

    // Collect all piece_ids in the blueprint.
    var used = {};
    var all = (bp.instances || []).concat(bp.placeables || []);
    for (var i = 0; i < all.length; i++) {
      var bt = all[i].building_type;
      if (bt) used[bt] = true;
    }

    // Check overlap with manifest (exact keys; the viewer also resolves
    // near-misses, so this is a conservative lower bound for the label).
    var matched = 0;
    for (var pid in manifest.pieces) {
      if (used[pid]) matched++;
    }
    if (matched === 0) return; // No real meshes for this blueprint — keep quiet.

    // Build the toggle bar.
    var bar = el('div', 'solido-mesh-toggle');
    bar.setAttribute('aria-label', 'Render mode');

    var boxBtn = el('button', 'solido-mesh-toggle__btn is-active', 'Boxes');
    boxBtn.type = 'button';
    boxBtn.setAttribute('aria-pressed', 'true');

    var realBtn = el('button', 'solido-mesh-toggle__btn', 'Real Meshes');
    realBtn.type = 'button';
    realBtn.setAttribute('aria-pressed', 'false');

    // First-person tour entry that doesn't require selecting a piece first
    // (spawns at the base centre). Real-mesh mode only — hidden under Boxes.
    var tourBtn = el('button', 'solido-mesh-toggle__btn solido-mesh-toggle__btn--tour', '🚶 Walk inside');
    tourBtn.type = 'button';
    tourBtn.hidden = true;
    tourBtn.addEventListener('click', function () {
      if (handle.startWalk) handle.startWalk();
    });

    bar.appendChild(boxBtn);
    bar.appendChild(realBtn);
    bar.appendChild(tourBtn);

    // Clay / Textured material sub-toggle (shown only while Real is active).
    var matWrap = el('div', 'solido-mesh-toggle solido-mesh-toggle--material');
    matWrap.hidden = true;
    var clayBtn = el('button', 'solido-mesh-toggle__btn is-active', 'Clay');
    clayBtn.type = 'button';
    var texBtn = el('button', 'solido-mesh-toggle__btn', 'Textured');
    texBtn.type = 'button';
    matWrap.appendChild(clayBtn);
    matWrap.appendChild(texBtn);

    if (handle.realMeshWarning) {
      var warn = el('span', 'solido-mesh-toggle__warn',
        'Large blueprint — real meshes may be slow on mobile');
      bar.appendChild(warn);
    }

    var switching = false;
    function setActive(mode) {
      var onBox = mode === 'boxes';
      boxBtn.classList.toggle('is-active', onBox);
      realBtn.classList.toggle('is-active', !onBox);
      boxBtn.setAttribute('aria-pressed', onBox ? 'true' : 'false');
      realBtn.setAttribute('aria-pressed', onBox ? 'false' : 'true');
      matWrap.hidden = onBox;
      tourBtn.hidden = onBox || !handle.startWalk;
      if (chrome) chrome.setRealActive(!onBox);
    }
    function setMat(toTextured) {
      clayBtn.classList.toggle('is-active', !toTextured);
      texBtn.classList.toggle('is-active', toTextured);
    }

    boxBtn.addEventListener('click', function () {
      if (switching) return;
      switching = true;
      boxBtn.disabled = realBtn.disabled = true;
      handle.switchMode('boxes', manifest).then(function () {
        setActive('boxes');
      }).catch(function () {}).then(function () {
        switching = false;
        boxBtn.disabled = realBtn.disabled = false;
      });
    });

    realBtn.addEventListener('click', function () {
      if (switching) return;
      switching = true;
      boxBtn.disabled = realBtn.disabled = true;
      realBtn.textContent = 'Loading…';
      handle.switchMode('real', manifest).then(function () {
        setActive('real');
      }).catch(function () {
        setActive('boxes');
      }).then(function () {
        realBtn.textContent = 'Real Meshes';
        switching = false;
        boxBtn.disabled = realBtn.disabled = false;
      });
    });

    function pickMaterial(toTextured) {
      if (switching || !handle.setTextured) return;
      switching = true;
      clayBtn.disabled = texBtn.disabled = true;
      var prev = toTextured ? texBtn.textContent : clayBtn.textContent;
      (toTextured ? texBtn : clayBtn).textContent = 'Loading…';
      handle.setTextured(toTextured).then(function () {
        setMat(toTextured);
      }).catch(function () {}).then(function () {
        (toTextured ? texBtn : clayBtn).textContent = prev;
        switching = false;
        clayBtn.disabled = texBtn.disabled = false;
      });
    }
    clayBtn.addEventListener('click', function () { pickMaterial(false); });
    texBtn.addEventListener('click', function () { pickMaterial(true); });

    // Insert before the canvas so it's keyboard-reachable before the 3D stage.
    // Both bars live in one wrapper (flex column) so they stack without
    // overlapping — the mode bar's warning line and the growable material/
    // selection bar no longer collide the way two fixed-top absolutes did.
    if (chrome && chrome.attachSelBar) chrome.attachSelBar(matWrap);
    var toolbars = el('div', 'solido-mesh-toolbars');
    toolbars.appendChild(bar);
    toolbars.appendChild(matWrap);
    container.insertBefore(toolbars, container.firstChild);

    // Real meshes are the default view (boxes stay one click away). The
    // instanced renderer handles 2500+ piece bases, so no piece-count gate.
    realBtn.click();
  }

  // ---- publish-time market thumbnail ------------------------------------
  // Renders the blueprint offscreen (real meshes, textured), captures a PNG,
  // and uploads it for the gallery card. Best-effort: any failure just leaves
  // the listing without a thumb. Resolves when done either way.
  function generateThumb(bpId, publishId) {
    var cv = document.createElement('canvas');
    cv.style.cssText = 'position:fixed;left:-10000px;top:0;width:800px;height:500px;';
    document.body.appendChild(cv);
    var handle = null;
    function cleanup() {
      if (handle) { try { handle.dispose(); } catch (e) {} handle = null; }
      cv.remove();
    }
    var work = mountViewer(cv,
      '/portal/my-bases/' + encodeURIComponent(bpId) + '/export',
      function () {}, null)
      .then(function (h) {
        handle = h;
        return h.getManifest();
      })
      .then(function (manifest) {
        if (!manifest || !manifest.pieces) throw new Error('no manifest');
        return handle.setTextured(true).then(function () {
          return handle.switchMode('real', manifest);
        });
      })
      .then(function () {
        // One settle tick so freshly-uploaded textures land on the GPU.
        return new Promise(function (r) { setTimeout(r, 300); });
      })
      .then(function () {
        var dataUrl = handle.snapshot(800, 500);
        var b64 = (dataUrl || '').split(',')[1];
        if (!b64) throw new Error('empty snapshot');
        return window.PortalAPI.fetch('POST',
          '/portal/solido/' + encodeURIComponent(publishId) + '/thumbnail',
          { png_base64: b64 });
      });
    // Hard cap so a slow GLB load never blocks the publish redirect.
    var timeout = new Promise(function (r) { setTimeout(r, 25000); });
    return Promise.race([work, timeout])
      .catch(function (err) {
        if (window.console) console.warn('thumbnail generation failed', err);
      })
      .then(cleanup);
  }

  // ---- detail (/portal/solido/{id}) -------------------------------------
  function initDetail(root) {
    var publishId = root.getAttribute('data-publish-id');
    var canvas = root.querySelector('[data-solido-canvas]');
    var note = root.querySelector('[data-solido-viewnote]');
    var viewBtn = root.querySelector('[data-solido-view]');
    var unpubBtn = root.querySelector('[data-solido-unpublish]');
    var handle = null;
    var busy = false;

    function setNote(msg) { if (note) { note.textContent = msg || ''; note.hidden = !msg; } }

    // Container for the real-mesh toggle (injected above the canvas after mount).
    var snapEl = root.querySelector('.solido-detail__snap');

    if (viewBtn && canvas) {
      viewBtn.addEventListener('click', function () {
        if (busy) return;
        busy = true;
        viewBtn.disabled = true;
        var label = viewBtn.textContent;
        viewBtn.textContent = 'Loading…';
        canvas.hidden = false;
        // Expand the snap to a full-width, taller stage while the 3D view is
        // open (the 2-col thumbnail box is too small to explore a base in).
        var head = root.querySelector('.solido-detail__head');
        if (head) head.classList.add('is-3d');
        setNote('');
        // Clear any previous toggle bar.
        if (snapEl) snapEl.querySelectorAll('.solido-mesh-toolbars, .solido-mesh-toggle').forEach(function (b) { b.remove(); });
        if (handle) { handle.dispose(); handle = null; }
        mountViewer(canvas,
          '/portal/api/solido/market/' + encodeURIComponent(publishId) + '/blueprint',
          setNote, snapEl)
          .then(function (h) {
            handle = h;
            canvas.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
          })
          .catch(function (err) {
            if (window.console) console.warn('detail 3D failed', err);
            canvas.hidden = true;
            if (head) head.classList.remove('is-3d');
            setNote('3D preview unavailable for this blueprint.');
          })
          .then(function () {
            busy = false;
            viewBtn.disabled = false;
            viewBtn.textContent = label;
          });
      });
    }

    if (unpubBtn) {
      unpubBtn.addEventListener('click', function () {
        if (busy) return;
        // data-portal-confirm already prompted via portal.js wireConfirmButtons.
        busy = true;
        unpubBtn.disabled = true;
        var id = unpubBtn.getAttribute('data-publish-id');
        window.PortalAPI.fetch('POST', '/portal/solido/' + encodeURIComponent(id) + '/unpublish', null)
          .then(function (r) {
            if (r.ok) { window.location = '/portal/solido'; return; }
            throw new Error('unpublish ' + r.status);
          })
          .catch(function (err) {
            if (window.console) console.warn('unpublish failed', err);
            busy = false;
            unpubBtn.disabled = false;
            setNote('Could not unpublish — try again.');
          });
      });
    }

    window.addEventListener('pagehide', function () {
      if (handle) { handle.dispose(); handle = null; }
    });
  }

  // ---- My Bases preview + publish/unpublish -----------------------------
  function initMyBases(root) {
    var stage = root.querySelector('[data-mybases-stage]');
    var canvas = root.querySelector('[data-mybases-canvas]');
    var note = root.querySelector('[data-mybases-viewnote]');
    var handle = null;
    var busy = false;

    function setNote(msg) { if (note) { note.textContent = msg || ''; note.hidden = !msg; } }

    root.addEventListener('click', function (ev) {
      var t = ev.target;
      if (!t || !t.closest) return;

      // Preview 3D (own blueprint, same-origin export JSON)
      var prev = t.closest('[data-mybases-preview]');
      if (prev && stage && canvas) {
        if (busy) return;
        var bpId = prev.getAttribute('data-bp-id');
        busy = true;
        prev.disabled = true;
        var label = prev.textContent;
        prev.textContent = 'Loading…';
        stage.hidden = false;
        setNote('');
        // Highlight the base row being viewed (clear any previous highlight).
        root.querySelectorAll('.is-viewing').forEach(function (n) {
          n.classList.remove('is-viewing');
        });
        var row = prev.closest('li') || prev.closest('section');
        if (row) row.classList.add('is-viewing');
        // Clear any previous toggle bar.
        stage.querySelectorAll('.solido-mesh-toolbars, .solido-mesh-toggle').forEach(function (b) { b.remove(); });
        if (handle) { handle.dispose(); handle = null; }
        mountViewer(canvas,
          '/portal/my-bases/' + encodeURIComponent(bpId) + '/export', setNote, stage)
          .then(function (h) {
            handle = h;
            stage.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
          })
          .catch(function (err) {
            if (window.console) console.warn('my-bases preview failed', err);
            setNote('Preview failed — this base may not be on your linked characters.');
          })
          .then(function () {
            busy = false;
            prev.disabled = false;
            prev.textContent = label;
          });
        return;
      }

      // Rename base — writes the in-game blueprint name (offline-only; the
      // writer hard-gates). Propagates to this list, exports, and the publish
      // default title. No modal infra on this page, so a prompt() is the
      // lightest fit (matches the alert()-based error style here).
      var ren = t.closest('[data-mybases-rename]');
      if (ren) {
        if (ren.disabled) return;
        var bpIdR = ren.getAttribute('data-bp-id');
        var rowR = ren.closest('li');
        var nameEl = rowR && rowR.querySelector('[data-mybases-name]');
        var current = nameEl ? nameEl.textContent.trim() : '';
        var next = window.prompt(
          'Rename this base (up to ' + RENAME_MAX + ' characters).\n' +
          'The new name applies while you are logged out of the game.', current);
        if (next == null) return;                       // cancelled
        next = next.replace(/\s+/g, ' ').trim();
        if (!next) { alert('Enter a name.'); return; }
        if (next.length > RENAME_MAX) {
          alert('Keep the name to ' + RENAME_MAX + ' characters or fewer.');
          return;
        }
        if (next === current) return;                   // no-op
        ren.disabled = true;
        var renLabel = ren.textContent;
        ren.textContent = 'Renaming…';
        window.PortalAPI.fetch('POST',
          '/portal/my-bases/' + encodeURIComponent(bpIdR) + '/rename', { name: next })
          .then(function (r) {
            return r.json().then(function (d) { return { status: r.status, data: d }; });
          })
          .then(function (res) {
            ren.textContent = renLabel;
            ren.disabled = false;
            if (res.status === 200 && res.data && res.data.ok) {
              var nm = res.data.name || next;
              if (nameEl) nameEl.textContent = nm;
              // Keep the Download filename hint in sync with the new name.
              if (rowR) {
                var dl = rowR.querySelector('a[download]');
                if (dl) dl.setAttribute('download', nm + '.json');
              }
              return;
            }
            alert((res.data && res.data.error) || 'Rename failed — try again.');
          })
          .catch(function (err) {
            if (window.console) console.warn('rename failed', err);
            ren.textContent = renLabel;
            ren.disabled = false;
            alert('Rename failed — check your connection and retry.');
          });
        return;
      }

      // Publish to market
      var pub = t.closest('[data-mybases-publish]');
      if (pub) {
        if (pub.disabled) return;
        pub.disabled = true;
        var label2 = pub.textContent;
        pub.textContent = 'Publishing…';
        window.PortalAPI.fetch('POST', '/portal/solido/publish',
          { game_bp_id: pub.getAttribute('data-bp-id') })
          .then(function (r) {
            return r.json().then(function (data) { return { status: r.status, data: data }; });
          })
          .then(function (res) {
            if (res.status === 200 && res.data && res.data.ok) {
              // Render + upload the gallery thumbnail before leaving the page
              // (best-effort, hard-capped; failure still redirects).
              pub.textContent = 'Rendering preview…';
              var pid = res.data.publish_id;
              generateThumb(pub.getAttribute('data-bp-id'), pid).then(function () {
                window.location = '/portal/solido/' + pid + '?published=1';
              });
              return;
            }
            var msg = (res.data && res.data.error) ||
              (res.status === 429 ? 'Daily publish limit reached.' :
               res.status === 413 ? 'Base too large to publish.' :
               res.status === 404 ? 'Blueprint not found for your characters.' :
               'Publish failed — try again.');
            pub.textContent = label2;
            pub.disabled = false;
            pub.setAttribute('title', msg);
            alert(msg);
          })
          .catch(function (err) {
            if (window.console) console.warn('publish failed', err);
            pub.textContent = label2;
            pub.disabled = false;
            alert('Publish failed — check your connection and retry.');
          });
        return;
      }

      // Unpublish from market (inline on My Bases)
      var unp = t.closest('[data-mybases-unpublish]');
      if (unp) {
        if (unp.disabled) return;
        // data-portal-confirm prompts via portal.js.
        unp.disabled = true;
        window.PortalAPI.fetch('POST',
          '/portal/solido/' + encodeURIComponent(unp.getAttribute('data-publish-id')) + '/unpublish', null)
          .then(function (r) {
            if (r.ok) { window.location.reload(); return; }
            throw new Error('unpublish ' + r.status);
          })
          .catch(function (err) {
            if (window.console) console.warn('unpublish failed', err);
            unp.disabled = false;
            alert('Could not unpublish — try again.');
          });
        return;
      }
    });

    window.addEventListener('pagehide', function () {
      if (handle) { handle.dispose(); handle = null; }
    });
  }

  // ----------------------------------------------------- Import to character ---
  // /portal/solido/import — paste a public link/UUID, upload a .json, or arrive
  // from a market listing via ?from=<publish_id>&title=<t> (private one-click).
  // Self-scoped POST; backpack delivery is gated on the selected char's online
  // state (the server enforces it too).
  function initImport(root) {
    var form = root.querySelector('[data-import-form]');
    if (!form) return;
    var resultEl = root.querySelector('[data-import-result]');
    var submitBtn = root.querySelector('[data-import-submit]');
    var csrfEl = root.querySelector('[data-csrf]');
    var privateBlock = root.querySelector('[data-private]');
    var publicUpload = root.querySelector('[data-public-upload]');

    function setResult(msg, kind) {
      if (!resultEl) return;
      resultEl.textContent = msg;
      resultEl.className = 'solido-import__result'
        + (kind === 'ok' ? ' is-ok' : (kind === 'err' ? ' is-err' : ''));
    }

    // Private deep-link from a market listing.
    var params = new URLSearchParams(window.location.search);
    var fromId = params.get('from');
    var privateMode = false;
    if (fromId && /^[0-9]+$/.test(fromId)) {
      privateMode = true;
      if (publicUpload) publicUpload.hidden = true;
      if (privateBlock) {
        privateBlock.hidden = false;
        var pid = privateBlock.querySelector('input[name="publish_id"]');
        if (pid) pid.value = fromId;
        var tnode = privateBlock.querySelector('[data-private-title]');
        if (tnode) tnode.textContent = params.get('title') || ('listing #' + fromId);
      }
    } else if (params.get('imported') === '1') {
      setResult('Imported. Check your CHOAM bank or backpack in-game.', 'ok');
    }

    function selectedAccount() {
      return form.querySelector('input[name="account_id"]:checked')
          || form.querySelector('input[name="account_id"]');
    }

    function syncDelivery() {
      var sel = selectedAccount();
      var online = sel && sel.getAttribute('data-online') === '1';
      var bpRadio = form.querySelector('input[name="delivery"][value="backpack"]');
      var bpLabel = form.querySelector('[data-backpack]');
      var note = form.querySelector('[data-backpack-note]');
      if (!bpRadio) return;
      bpRadio.disabled = !!online;
      if (bpLabel) bpLabel.classList.toggle('is-disabled', !!online);
      if (note) note.textContent = online
        ? '(log out of the game to use the backpack)'
        : '(works while you are offline)';
      if (online && bpRadio.checked) {
        var bank = form.querySelector('input[name="delivery"][value="bank"]');
        if (bank) bank.checked = true;
      }
    }

    function syncSourceInputs() {
      if (privateMode) return;
      var picked = form.querySelector('input[name="source_type"]:checked');
      var st = picked ? picked.value : 'public';
      var linkInput = form.querySelector('input[data-src="public"]');
      var fileInput = form.querySelector('input[data-src="upload"]');
      if (linkInput) linkInput.hidden = (st !== 'public');
      if (fileInput) fileInput.hidden = (st !== 'upload');
    }

    form.addEventListener('change', function (e) {
      if (e.target.name === 'account_id') syncDelivery();
      if (e.target.name === 'source_type') syncSourceInputs();
    });
    syncDelivery();
    syncSourceInputs();

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      if (submitBtn.disabled) return;
      var accSel = selectedAccount();
      var accountId = accSel ? accSel.value : '';
      var deliverySel = form.querySelector('input[name="delivery"]:checked');
      var delivery = deliverySel ? deliverySel.value : 'bank';
      var stSel = form.querySelector('input[name="source_type"]:checked');
      var sourceType = privateMode ? 'private' : (stSel ? stSel.value : 'public');

      var done = function (data) {
        submitBtn.disabled = false;
        if (data && data.ok) setResult(data.message || 'Imported.', 'ok');
        else setResult((data && (data.error || data.message)) || 'Import failed.', 'err');
      };
      var fail = function () {
        submitBtn.disabled = false;
        setResult('Import failed — check your connection and retry.', 'err');
      };

      submitBtn.disabled = true;
      setResult('Importing…', '');

      if (sourceType === 'upload') {
        var fileInput = form.querySelector('input[data-src="upload"]');
        if (!fileInput || !fileInput.files || !fileInput.files.length) {
          submitBtn.disabled = false;
          setResult('Choose a .json file to upload.', 'err');
          return;
        }
        var fd = new FormData();
        fd.append('csrf_token', csrfEl ? csrfEl.value : '');
        fd.append('source_type', 'upload');
        fd.append('account_id', accountId);
        fd.append('delivery', delivery);
        fd.append('blueprint_file', fileInput.files[0]);
        fetch('/portal/solido/import', {
          method: 'POST', credentials: 'same-origin',
          headers: { 'Accept': 'application/json' }, body: fd
        }).then(function (r) { return r.json().then(done); }).catch(fail);
        return;
      }

      var body = { source_type: sourceType, account_id: accountId, delivery: delivery };
      if (sourceType === 'private') {
        var pidEl = form.querySelector('input[name="publish_id"]');
        body.publish_id = pidEl ? pidEl.value : '';
      } else {
        var linkEl = form.querySelector('input[data-src="public"]');
        body.link = linkEl ? linkEl.value : '';
      }
      window.PortalAPI.fetch('POST', '/portal/solido/import', body)
        .then(function (r) { return r.json().then(done); }).catch(fail);
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    var gallery = document.querySelector('[data-solido-gallery]');
    if (gallery) initGallery(gallery);
    var detail = document.querySelector('[data-solido-detail]');
    if (detail) initDetail(detail);
    var mybases = document.querySelector('[data-mybases]');
    if (mybases) initMyBases(mybases);
    var imp = document.querySelector('[data-solido-import]');
    if (imp) initImport(imp);
  });
})();
