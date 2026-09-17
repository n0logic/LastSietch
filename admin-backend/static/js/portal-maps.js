/* Last Sietch portal — coordinate-accurate map engine.
 *
 * Renders any map declared in map_model.MAPS from the cached /data endpoint:
 *   - backdrop:  sand texture (DD) or the real terrain image (Hagga)
 *   - markers:   17k+ POIs/resources on a <canvas> (fast), colored by category
 *   - grid:      9x9 survey grid + sector labels on an <svg> overlay (DD)
 *   - live:      active Large spice blow + candidate sites (DD), per instance
 *   - legend:    collapsible sidebar with category→type tree, search, presets
 *   - waypoints: private per-player pins (Alt+click), CRUD via /waypoints
 *   - sandstorm: per-dimension ETA countdown banner (T#5)
 *
 * One world transform (baseScale * zoom + pan) keeps canvas, svg overlay and the
 * CSS-transformed backdrop in lockstep. Static markers are shared across a map's
 * instances (dim=-1); only the live overlays swap when you toggle PvE/PvP.
 */
(function () {
  "use strict";
  var el = function (id) { return document.getElementById(id); };
  var viewport = el("map-viewport");
  if (!viewport) return;
  var MAP_KEY = viewport.dataset.mapKey;
  if (!MAP_KEY) return;

  var stage = el("map-stage"),
      backdrop = el("map-backdrop"), canvas = el("map-canvas"),
      svg = el("map-overlay"), tooltip = el("map-tooltip"),
      loading = el("map-loading"), legendBox = el("map-legend"),
      countEl = el("map-count"),
      popup = el("map-popup"), popupName = el("map-popup-name"),
      popupCat = el("map-popup-cat"), popupCoords = el("map-popup-coords"),
      popupCopy = el("map-popup-copy"), popupAlt = el("map-popup-alt"),
      popupClose = el("map-popup-close");
  var ctx = canvas.getContext("2d");
  var SVGNS = "http://www.w3.org/2000/svg";
  var META = { key: MAP_KEY, view: 1000 };
  var VIEW = 1000;
  var SS = Math.min(2, window.devicePixelRatio || 1) * 1.5;

  // Preset storage key (per map)
  var PRESET_KEY = "map_preset_" + MAP_KEY;
  // Current layer choice (per map), shared with the V2 companion via the same key.
  var LAYERS_KEY = "map_layers_" + MAP_KEY;

  var state = {
    data: null, markers: [], typeIndex: [], typeIcons: [],
    catIndex: [], catColors: {},
    // Per-TYPE hidden map: typeIdx (number) -> true. Legacy cat keys also work.
    hidden: {},
    zoom: 1, panX: 0, panY: 0, display: 0,
    instance: (META.instances && META.instances[0]) || null,
    spice: null, spiceHover: [],
    worms: null, wormShow: true, wormHover: [], wormEls: {}, wormAnim: null,
    me: null, meAuthed: false, meHover: [],
    meShow: { self: true, bases: true, vehicles: true },
    // Waypoints
    waypoints: [], wpAuthed: false, wpHover: [],
    // T#5: audio cue toggle
    audioOn: false,
    // search filter (lower-case string or "")
    filterSearch: "",
    // click popup state
    popupMarker: null,
  };

  var RADIUS = { spice: 0, poi: 3.6, enemy: 3.2, ore: 2.4, salvage: 2.2,
                 hazard: 3.0, flora: 2.0, other: 2.2 };

  // ---- data load ----------------------------------------------------------
  function load() {
    fetch("/portal/maps/" + META.key + "/data", { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        META = { key: d.map, view: d.view || 1000, cal: d.cal, grid: d.grid,
                 backdrop: d.backdrop, instances: d.instances || [],
                 has_spice: !!d.has_spice,
                 // Storms are their own axis, not a spice side-effect: Hagga has
                 // storms and no spice (three sietches, each storming on its own).
                 has_storms: !!d.has_storms,
                 spice_candidates: d.spice_candidates || [],
                 // Part A/B static-per-cycle spice data (from /data, not /live):
                 // medium fields [[nx,ny,sector],...] + exact candidate coords
                 // {sector:[nx,ny]}. Empty until the reader feeds them (degrades to
                 // sector-center plotting, never blanks).
                 spice_mediums: d.spice_mediums || [],
                 spice_candidate_coords: d.spice_candidate_coords || {},
                 // Deterministic Coriolis cycle window {cycle_start_utc,
                 // next_cycle_utc}; the client ticks a countdown off next_cycle_utc.
                 coriolis: d.coriolis || null };
        VIEW = META.view;
        state.instance = META.instances[0] || null;
        state.data = d;
        state.markers = d.markers || [];
        state.typeIndex = d.type_index || [];
        state.typeIcons = d.type_icons || [];
        state.catIndex = d.cat_index || [];
        state.catColors = d.cat_colors || {};
        state.legend = d.legend || [];
        state.hidden = restoreLayers(state.legend);
        buildLegend(state.legend);
        // Reflect the live medium-field count on the "Medium fields" legend toggle,
        // plus the erupted/active subset (md[3]) as a subtle tooltip figure.
        var medAll = META.spice_mediums || [];
        var medActiveN = medAll.filter(function (m) { return m && m[3] === true; }).length;
        setMediumLegendCount(medAll.length, medActiveN);
        countEl.textContent = (d.total || 0).toLocaleString() + " points · ";
        setupBackdrop();
        layout();
        loading.hidden = true;
        // /live carries spice + worms + sandstorm, so any map with EITHER
        // system needs the poll. Coriolis stays DD-only (spice cycle).
        if (META.has_spice || META.has_storms) {
          pollLive(); setInterval(pollLive, 10000);
        }
        if (META.has_spice) {
          updateCoriolisBanner(); setInterval(updateCoriolisBanner, 60000);
        }
        pollPlayers(); setInterval(pollPlayers, 45000);
        fetchMe();
        setInterval(fetchMe, 10000);
        fetchWaypoints();
      })
      .catch(function () { loading.textContent = "Map data unavailable."; });
  }

  // ---- backdrop -----------------------------------------------------------
  function setupBackdrop() {
    var b = META.backdrop || { type: "sand" };
    if (b.type === "image" && b.src) {
      backdrop.style.backgroundImage = "url('" + b.src + "')";
      backdrop.style.backgroundSize = "100% 100%";
      backdrop.classList.add("map-backdrop--image");
    } else {
      backdrop.classList.add("map-backdrop--sand");
    }
  }

  // ---- legend / layer toggles (category→type tree) ------------------------
  function isHiddenType(typeIdx) {
    return !!state.hidden[typeIdx];
  }
  function isHiddenCat(catKey) {
    // A category is "off" only when ALL its types are hidden.
    // If state.hidden[catKey] is explicitly set true (legacy compat), honour it.
    if (state.hidden[catKey] === true) return true;
    return false;
  }

  function buildLegend(legend) {
    legendBox.innerHTML = "";
    legend.forEach(function (cat) {
      // Category row (collapsible)
      var catRow = document.createElement("div");
      catRow.className = "map-legend__cat";
      catRow.dataset.catKey = cat.key;
      catRow.setAttribute("role", "listitem");

      var catBtn = document.createElement("button");
      catBtn.type = "button";
      catBtn.className = "map-legend__cat-btn";
      catBtn.innerHTML =
        '<span class="map-legend__expand" role="button" tabindex="0" ' +
          'aria-label="Collapse or expand category">&#x25BE;</span>' +
        '<span class="map-legend__swatch" style="background:' + cat.color + '"></span>' +
        '<span class="map-legend__cat-label">' + cat.label + '</span>' +
        '<span class="map-legend__count">' + (cat.count || 0).toLocaleString() + '</span>';

      // Children container
      var childWrap = document.createElement("div");
      childWrap.className = "map-legend__types";

      // The expand arrow collapses/expands the type list; clicking anywhere else
      // on the row toggles the whole category's visibility. (Previously the arrow
      // had no handler, so it fell through to the category toggle.)
      function toggleCollapse() {
        var collapsed = catRow.classList.toggle("is-collapsed");
        childWrap.style.display = collapsed ? "none" : "";
        var arrow = catBtn.querySelector(".map-legend__expand");
        if (arrow) arrow.innerHTML = collapsed ? "&#x25B8;" : "&#x25BE;";  // ▸ / ▾
      }
      catBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        if (e.target.classList && e.target.classList.contains("map-legend__expand")) {
          toggleCollapse();
          return;
        }
        var types = cat.types || [];
        var allHidden = types.every(function (t) { return state.hidden[t.idx]; });
        types.forEach(function (t) { state.hidden[t.idx] = !allHidden; });
        catRow.classList.toggle("is-off", !allHidden);
        updateTypeRows(catRow);
        persistLayers();
        drawCanvas();
        if (cat.overlay) { drawSpice(); drawStorm(); }   // overlay layers live in the SVG, not the canvas
      });
      catRow.appendChild(catBtn);

      (cat.types || []).forEach(function (typ) {
        var typeRow = document.createElement("button");
        typeRow.type = "button";
        typeRow.className = "map-legend__type";
        typeRow.dataset.typeIdx = typ.idx;
        var iconHtml = "";
        if (typ.icon) {
          iconHtml = '<img class="map-legend__type-icon" src="/admin/static/img/dune-icons/' +
            typ.icon + '.png" alt="" aria-hidden="true">';
        } else {
          iconHtml = '<span class="map-legend__swatch" style="background:' + cat.color + '"></span>';
        }
        typeRow.innerHTML = iconHtml +
          '<span class="map-legend__type-label">' + typ.label + '</span>' +
          '<span class="map-legend__count">' + (typ.count || 0).toLocaleString() + '</span>';
        if (state.hidden[typ.idx]) typeRow.classList.add("is-off");
        typeRow.addEventListener("click", function (e) {
          e.stopPropagation();
          state.hidden[typ.idx] = !state.hidden[typ.idx];
          typeRow.classList.toggle("is-off", !!state.hidden[typ.idx]);
          updateCatRow(catRow, cat);
          persistLayers();
          drawCanvas();
          if (typ.overlay) { drawSpice(); drawStorm(); }   // spice + sandstorm are SVG overlays
        });
        childWrap.appendChild(typeRow);
      });
      catRow.appendChild(childWrap);
      updateCatRow(catRow, cat);   // a restored all-hidden category opens "off"
      legendBox.appendChild(catRow);
    });
  }

  // Opening layer state: the player's last choice on this board (map_layers_*,
  // shared with the V2 companion), else the board's defaults. The Deep Desert
  // opens with the medium spice fields OFF (owner ask 2026-09-02); everything
  // else on. Seeded from the legend, never from the map key.
  function defaultLayers(legend) {
    var hasMedium = (legend || []).some(function (cat) {
      return (cat.types || []).some(function (t) { return t.idx === "spice_medium"; });
    });
    return hasMedium ? { spice_medium: true } : {};
  }
  function restoreLayers(legend) {
    try {
      var saved = JSON.parse(localStorage.getItem(LAYERS_KEY) || "null");
      if (saved && typeof saved === "object" && !Array.isArray(saved)) return saved;
    } catch (e) {}
    return defaultLayers(legend);
  }
  function persistLayers() {
    try { localStorage.setItem(LAYERS_KEY, JSON.stringify(state.hidden)); } catch (e) {}
  }
  // Re-derive every row's on/off class from state.hidden (after a bulk change).
  function syncLegendRows() {
    legendBox.querySelectorAll(".map-legend__cat").forEach(function (catRow) {
      updateTypeRows(catRow);
      var anyOn = false;
      catRow.querySelectorAll(".map-legend__type").forEach(function (b) {
        if (!state.hidden[legendKey(b.dataset.typeIdx)]) anyOn = true;
      });
      catRow.classList.toggle("is-off", !anyOn);
    });
  }

  // Marker types are keyed by a numeric index; spice overlay layers use a string
  // key (e.g. "spice_medium"). Preserve the string so overlay toggles round-trip.
  function legendKey(raw) {
    return /^\d+$/.test(raw) ? parseInt(raw, 10) : raw;
  }

  function updateTypeRows(catRow) {
    catRow.querySelectorAll(".map-legend__type").forEach(function (btn) {
      var ti = legendKey(btn.dataset.typeIdx);
      btn.classList.toggle("is-off", !!state.hidden[ti]);
    });
  }

  function updateCatRow(catRow, cat) {
    var types = cat.types || [];
    var allHidden = types.length > 0 && types.every(function (t) { return state.hidden[t.idx]; });
    catRow.classList.toggle("is-off", allHidden);
  }

  // Set the live medium-field count on its legend toggle + the Spice category row.
  // `active` (optional) is the erupted subset; surfaced as a row tooltip rather than
  // a second count chip — mediums are mostly-active, so a separate figure would just
  // crowd the row. Omitting `active` keeps the legacy total-only behavior.
  function setMediumLegendCount(n, active) {
    var row = legendBox.querySelector('.map-legend__type[data-type-idx="spice_medium"]');
    if (row) {
      var c = row.querySelector(".map-legend__count");
      if (c) c.textContent = (n || 0).toLocaleString();
      row.title = active
        ? active.toLocaleString() + " of " + (n || 0).toLocaleString() + " medium fields erupted now"
        : (n || 0).toLocaleString() + " medium spice fields";
    }
    var catRow = legendBox.querySelector('.map-legend__cat[data-cat-key="spice"]');
    if (catRow) {
      var cc = catRow.querySelector(".map-legend__count");
      if (cc) cc.textContent = (n || 0).toLocaleString();
    }
  }

  // Apply search filter: show/hide type rows matching the search string.
  function applySearch(needle) {
    state.filterSearch = needle;
    var low = needle.toLowerCase();
    legendBox.querySelectorAll(".map-legend__cat").forEach(function (catRow) {
      var anyVisible = false;
      catRow.querySelectorAll(".map-legend__type").forEach(function (btn) {
        var label = (btn.querySelector(".map-legend__type-label") || {}).textContent || "";
        var match = !low || label.toLowerCase().indexOf(low) >= 0;
        btn.style.display = match ? "" : "none";
        if (match) anyVisible = true;
      });
      catRow.style.display = anyVisible || !low ? "" : "none";
    });
  }

  // Quick-pick buttons
  el("map-filter-all") && el("map-filter-all").addEventListener("click", function () {
    legendBox.querySelectorAll(".map-legend__type").forEach(function (btn) {
      state.hidden[legendKey(btn.dataset.typeIdx)] = false;
      btn.classList.remove("is-off");
    });
    legendBox.querySelectorAll(".map-legend__cat").forEach(function (c) { c.classList.remove("is-off"); });
    persistLayers();
    drawCanvas(); drawSpice();
  });
  el("map-filter-none") && el("map-filter-none").addEventListener("click", function () {
    legendBox.querySelectorAll(".map-legend__type").forEach(function (btn) {
      state.hidden[legendKey(btn.dataset.typeIdx)] = true;
      btn.classList.add("is-off");
    });
    legendBox.querySelectorAll(".map-legend__cat").forEach(function (c) { c.classList.add("is-off"); });
    persistLayers();
    drawCanvas(); drawSpice();
  });
  el("map-filter-default") && el("map-filter-default").addEventListener("click", function () {
    state.hidden = defaultLayers(state.legend);   // as first opened, not "all on"
    syncLegendRows();
    persistLayers();
    drawCanvas(); drawSpice();
  });
  el("map-filter-save") && el("map-filter-save").addEventListener("click", function () {
    try {
      localStorage.setItem(PRESET_KEY, JSON.stringify(state.hidden));
      var loadBtn = el("map-filter-load");
      if (loadBtn) loadBtn.style.display = "";
    } catch (e) {}
  });
  el("map-filter-load") && el("map-filter-load").addEventListener("click", function () {
    try {
      var saved = localStorage.getItem(PRESET_KEY);
      if (!saved) return;
      state.hidden = JSON.parse(saved);
      syncLegendRows();
      persistLayers();
      drawCanvas(); drawSpice();
    } catch (e) {}
  });

  // Initialise load-preset button visibility
  (function () {
    try {
      if (localStorage.getItem(PRESET_KEY)) {
        var loadBtn = el("map-filter-load");
        if (loadBtn) loadBtn.style.display = "";
      }
    } catch (e) {}
  })();

  // Search input
  var searchEl = el("map-filter-search");
  if (searchEl) {
    searchEl.addEventListener("input", function () {
      applySearch(searchEl.value.trim());
    });
  }

  // ---- layout + transform -------------------------------------------------
  function layout() {
    var sidebar = el("map-sidebar");
    var sidebarW = (sidebar && window.innerWidth >= 900) ? sidebar.offsetWidth : 0;
    var avail = viewport.parentElement ? viewport.parentElement.clientWidth - sidebarW : viewport.clientWidth;
    var size = Math.min(avail, viewport.clientHeight || avail);
    state.display = size;
    stage.style.width = size + "px";
    stage.style.height = size + "px";
    canvas.style.width = size + "px";
    canvas.style.height = size + "px";
    canvas.width = Math.round(size * SS);
    canvas.height = Math.round(size * SS);
    drawGrid();
    drawCanvas();
    applyTransform();
  }

  function applyTransform() {
    var t = "translate(" + state.panX + "px," + state.panY + "px) scale(" + state.zoom + ")";
    stage.style.transform = t;
    drawSpice();
    drawWorms();
    drawStorm();
    drawMe();
    drawWaypoints();
  }

  function baseScale() { return state.display / VIEW; }
  function cx(nx) { return nx * baseScale() * SS; }
  function cy(ny) { return ny * baseScale() * SS; }

  // ---- canvas markers -----------------------------------------------------
  function drawCanvas() {
    if (!ctx) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    if (META.grid) {
      var bandTop = (7 / 9) * canvas.height;
      var grad = ctx.createLinearGradient(0, bandTop, 0, canvas.height);
      grad.addColorStop(0, "rgba(58,40,24,0)");
      grad.addColorStop(0.55, "rgba(52,35,20,0.55)");
      grad.addColorStop(1, "rgba(38,26,15,0.92)");
      ctx.fillStyle = grad;
      ctx.fillRect(0, bandTop, canvas.width, canvas.height - bandTop);
    }

    var byCat = {};
    state.markers.forEach(function (m) {
      var cat = state.catIndex[m[2]] || "other";
      // Per-type filtering: check typeIdx first, then category key.
      var typeIdx = m[3];
      if (typeof typeIdx === "number" && state.hidden[typeIdx]) return;
      if (isHiddenCat(cat)) return;
      (byCat[cat] || (byCat[cat] = [])).push(m);
    });
    // Hub categories (service/vendor/district) were added for Arrakeen/Harko and
    // must be drawn too — otherwise their markers bucket into byCat but never
    // render (only the hover tooltip finds them). CHOAM Exchange on Hagga is a
    // "service" marker, so it needs this. Appended last = drawn on top. "spice"
    // stays out: it has its own drawSpice() pass.
    var order = ["flora", "salvage", "ore", "hazard", "enemy", "poi", "other",
                 "district", "vendor", "service"];
    ctx.lineWidth = Math.max(0.6, 0.5 * SS);
    ctx.strokeStyle = "rgba(20,12,4,0.55)";
    order.forEach(function (cat) {
      var arr = byCat[cat]; if (!arr) return;
      ctx.fillStyle = state.catColors[cat] || "#888";
      var r = Math.max(1.4 * SS, (RADIUS[cat] || 1.5) * SS);
      for (var i = 0; i < arr.length; i++) {
        var m = arr[i];
        // Use icon if available (draw a small image), else colored dot
        var typeIdx = m[3];
        var iconName = (typeof typeIdx === "number") ? state.typeIcons[typeIdx] : null;
        if (iconName && _iconCache[iconName] && _iconCache[iconName].complete && _iconCache[iconName].naturalWidth) {
          var iconR = r * 1.4;
          ctx.drawImage(_iconCache[iconName], cx(m[0]) - iconR, cy(m[1]) - iconR, iconR * 2, iconR * 2);
        } else {
          ctx.beginPath();
          ctx.arc(cx(m[0]), cy(m[1]), r, 0, 6.2832);
          ctx.fill();
          ctx.stroke();
          // Kick off icon load if not yet cached
          if (iconName && !_iconCache[iconName]) {
            _loadIcon(iconName);
          }
        }
      }
    });
  }

  // Small icon cache (lazy-loaded). Each value is an HTMLImageElement.
  var _iconCache = {};
  var _iconLoading = {};
  function _loadIcon(name) {
    if (_iconLoading[name]) return;
    _iconLoading[name] = true;
    var img = new Image();
    img.onload = function () { _iconCache[name] = img; drawCanvas(); };
    img.onerror = function () { _iconCache[name] = false; };  // sentinel to skip retries
    img.src = "/admin/static/img/dune-icons/" + name + ".png";
  }

  // ---- svg grid + labels (DD) + PvP boundary line ------------------------
  function drawGrid() {
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    var g = META.grid;
    if (!g) return;
    var step = VIEW / g.cols;
    var gridG = document.createElementNS(SVGNS, "g");
    gridG.setAttribute("class", "map-grid");
    for (var i = 0; i <= g.cols; i++) {
      var p = i * step;
      line(gridG, p, 0, p, VIEW);
      line(gridG, 0, p, VIEW, p);
    }
    svg.appendChild(gridG);
    var letters = g.row_letters || "ABCDEFGHI";
    var labelG = document.createElementNS(SVGNS, "g");
    labelG.setAttribute("class", "map-grid-labels");
    for (var r = 0; r < g.rows; r++) {
      var letter = letters.charAt(g.rows - 1 - r);
      for (var c = 0; c < g.cols; c++) {
        var tx = c * step + step * 0.5, ty = r * step + step * 0.5;
        text(labelG, tx, ty, letter + (c + 1));
      }
    }
    svg.appendChild(labelG);

    // NOTE: no internal PvE/PvP split line. On our server PvE/PvP is per-INSTANCE
    // (the PvE|PvP tab), not a zone within one map: the PvE deep desert is PvE
    // across all rows A-I, and the PvP deep desert is PvP everywhere past the
    // shield wall (row A). The official-game mid-map boundary does not apply here.
  }

  function line(parent, x1, y1, x2, y2) {
    var l = document.createElementNS(SVGNS, "line");
    l.setAttribute("x1", x1); l.setAttribute("y1", y1);
    l.setAttribute("x2", x2); l.setAttribute("y2", y2);
    parent.appendChild(l);
    return l;
  }
  function text(parent, x, y, s) {
    var t = document.createElementNS(SVGNS, "text");
    t.setAttribute("x", x); t.setAttribute("y", y);
    t.setAttribute("class", "map-grid-label");
    t.textContent = s;
    parent.appendChild(t);
    return t;
  }

  // ---- live overlays (DD): ONE feed for spice + worms + sandstorm --------
  function pollLive() {
    fetch("/portal/maps/" + META.key + "/live", { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        state.spice = d.spice || { dimensions: {} };
        state.worms = d.worms || { dimensions: {} };
        state.sandstorm = d.sandstorm || { dimensions: {} };
        drawSpice(); updateSpiceBanner();
        drawWorms(); updateWormBanner();
        drawStorm();
        updateProximity();
        // T#5: sandstorm ETA banner
        updateSandstormBanner(d.sandstorm);
      })
      .catch(function () {});
  }

  // ---- player counter (all maps): this-map total + server-wide online -----
  function pollPlayers() {
    // Scope the count to the SELECTED instance. Without ?dim the backend returns
    // the whole-map total, which on a multi-world map is the same number on every
    // instance card (DD read "2 here" on both PvE and PvP while only dim 0 held
    // them; Hagga read dim0+dim1+dim2 on all three). Same pattern as the storm
    // banner above: state.instance.dim, null for single-world maps.
    var pdim = (state.instance && state.instance.dim != null) ? state.instance.dim : null;
    fetch("/portal/maps/" + META.key + "/players" + (pdim != null ? "?dim=" + pdim : ""),
          { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var box = el("map-players-counter");
        var txt = el("map-players-counter__text");
        if (!box || !txt) return;
        if (!d || !d.available) { box.hidden = true; return; }
        var here = d.map_players || 0;
        var total = d.server_players || 0;
        txt.textContent = here + " here · " + total + " online";
        box.hidden = false;
      })
      .catch(function () {});
  }

  function sectorCenter(sector) {
    var g = META.grid; if (!g || !sector) return null;
    var letters = g.row_letters || "ABCDEFGHI";
    var row = letters.indexOf(sector.charAt(0));
    var col = parseInt(sector.slice(1), 10);
    if (row < 0 || !col) return null;
    var step = VIEW / g.cols;
    var rowFromTop = (g.rows - 1) - row;
    return { x: (col - 0.5) * step, y: (rowFromTop + 0.5) * step };
  }

  function drawSpice() {
    var old = svg.querySelector(".map-spice-layer");
    if (old) svg.removeChild(old);
    if (!META.has_spice) return;
    var layer = document.createElementNS(SVGNS, "g");
    layer.setAttribute("class", "map-spice-layer");
    var step = VIEW / (META.grid ? META.grid.cols : 9);
    state.spiceHover = [];
    // Candidate Large sites. Plot at the exact projected coord when the reader has
    // supplied one for this sector (Part B); otherwise fall back to the sector
    // center so the layer never blanks between rotations / when RAM is unavailable.
    var coords = META.spice_candidate_coords || {};
    if (!state.hidden["spice_large"]) {
      (META.spice_candidates || []).forEach(function (sec) {
        var xy = coords[sec];
        var c = (xy && xy.length === 2) ? { x: xy[0], y: xy[1] } : sectorCenter(sec);
        if (!c) return;
        diamond(layer, c.x, c.y, step * 0.30, "map-spice-candidate");
        state.spiceHover.push({ x: c.x, y: c.y, r: step * 0.42,
          label: "Candidate Large spice field, " + sec });
      });
    }

    // Medium spice fields (Part A): the full per-cycle set at exact coords, static
    // from /data. Toggle-gated under the Spice legend category; smaller, lighter
    // melange-purple diamond to distinguish from the larger/brighter Large sites.
    // Degrades to nothing until the reader feeds ram_mediums.
    if (!state.hidden["spice_medium"]) {
      (META.spice_mediums || []).forEach(function (md) {
        if (!md || md.length < 2) return;
        // md[3] = active/erupted (rotates among the static medium sites). Same
        // size as the dormant variant; only the class (color/glow) differs.
        var medActive = md[3] === true;
        diamond(layer, md[0], md[1], step * 0.20,
                medActive ? "map-spice-medium-active" : "map-spice-medium");
        state.spiceHover.push({ x: md[0], y: md[1], r: step * 0.28,
          label: (medActive ? "Active medium spice field" : "Medium spice field")
            + (md[2] ? " — " + md[2] : "") });
      });
    }

    var dim = state.instance ? state.instance.dim : null;
    var dims = (state.spice && state.spice.dimensions) || {};
    var info = dims[String(dim)];
    if (info && info.large_active && !state.hidden["spice_large"]) {
      // Every surfaced Large (Spice Harvest = 2-3 at once). Fall back to the single
      // ram_sector pin when the reader didn't supply the multi-field list (legacy).
      var blows = (info.ram_active_fields && info.ram_active_fields.length)
        ? info.ram_active_fields.map(function (f) {
            var fsec = (f.sector || "").toUpperCase();
            var fc = (f.nx != null && f.ny != null)
              ? { x: f.nx, y: f.ny }
              : (fsec ? sectorCenter(fsec) : null);
            return { sec: fsec, c: fc };
          })
        : (function () {
            var sec = (info.ram_sector || "").toUpperCase();
            var c = (info.ram_nx != null && info.ram_ny != null)
              ? { x: info.ram_nx, y: info.ram_ny }
              : (sec ? sectorCenter(sec) : null);
            return [{ sec: sec, c: c }];
          })();
      blows.forEach(function (b) {
        if (!b.c) return;
        diamond(layer, b.c.x, b.c.y, step * 0.38, "map-spice-active");
        var pulse = document.createElementNS(SVGNS, "circle");
        pulse.setAttribute("cx", b.c.x); pulse.setAttribute("cy", b.c.y);
        pulse.setAttribute("r", step * 0.5);
        pulse.setAttribute("class", "map-spice-pulse");
        layer.insertBefore(pulse, layer.firstChild);
        state.spiceHover.push({ x: b.c.x, y: b.c.y, r: step * 0.5,
          label: "Active Large spice blow" + (b.sec ? ", " + b.sec : "") });
      });
    }
    svg.appendChild(layer);
  }

  function diamond(parent, x, y, r, cls) {
    var p = document.createElementNS(SVGNS, "path");
    p.setAttribute("d", "M" + x + " " + (y - r) + "L" + (x + r) + " " + y +
      "L" + x + " " + (y + r) + "L" + (x - r) + " " + y + "Z");
    p.setAttribute("class", cls);
    parent.appendChild(p);
  }

  function updateSpiceBanner() {
    var banner = el("map-spice-banner"); if (!banner) return;
    var dim = state.instance ? state.instance.dim : null;
    var info = ((state.spice && state.spice.dimensions) || {})[String(dim)];
    var txt = el("map-spice-banner__text");
    var medAll = META.spice_mediums || [];
    var medN = medAll.length;
    var medActiveN = medAll.filter(function (m) { return m && m[3] === true; }).length;
    var medTxt = medN
      ? " · " + medN.toLocaleString() + " medium field" + (medN === 1 ? "" : "s")
          + (medActiveN ? " (" + medActiveN.toLocaleString() + " active)" : "")
      : "";
    if (info && info.large_active) {
      banner.hidden = false;
      var fields = info.ram_active_fields || [];
      var secs = fields.map(function (f) { return (f.sector || "").toUpperCase(); })
                       .filter(function (s) { return s; });
      if (secs.length > 1) {
        var strong = secs.map(function (s) { return "<strong>" + s + "</strong>"; });
        txt.innerHTML = secs.length + " active spice blows erupting at sectors "
          + strong.join(", ") + medTxt;
      } else {
        var sec = secs[0] || (info.ram_sector || "").toUpperCase();
        txt.innerHTML = (sec
          ? "Active spice blow erupting at sector <strong>" + sec + "</strong>"
          : "A Large spice field is active now — at one of the candidate sites below.") + medTxt;
      }
    } else {
      banner.hidden = false;
      txt.textContent = "No active Large spice blow right now." + medTxt;
    }
  }

  // Coriolis reset countdown. The Deep Desert regenerates (nodes + spice) on the
  // engine's fixed 05:00 UTC / 14-day cadence; next_cycle_utc is an absolute
  // instant the client counts down from, so a stale data cache never skews it.
  function fmtCountdown(ms) {
    if (ms <= 0) return null;
    var s = Math.floor(ms / 1000);
    var d = Math.floor(s / 86400); s -= d * 86400;
    var h = Math.floor(s / 3600); s -= h * 3600;
    var m = Math.floor(s / 60);
    var parts = [];
    if (d) parts.push(d + "d");
    if (d || h) parts.push(h + "h");
    parts.push(m + "m");
    return parts.join(" ");
  }

  function updateCoriolisBanner() {
    var banner = el("map-coriolis-banner");
    var txt = el("map-coriolis-banner__text");
    if (!banner || !txt) return;
    var c = META.coriolis;
    if (!c || !c.next_cycle_utc) { banner.hidden = true; return; }
    var ms = new Date(c.next_cycle_utc).getTime() - Date.now();
    var cd = fmtCountdown(ms);
    banner.hidden = false;
    txt.innerHTML = cd
      ? "Deep Desert regenerates in <strong>" + cd + "</strong>"
      : "Deep Desert is regenerating now";
  }

  // T#5: sandstorm ETA banner -----------------------------------------------
  // Reads d.sandstorm: {available: bool, dimensions: {"0":{label,next_eta_utc,...},...}}
  // Renders a per-dimension countdown ("Next storm ~Xm, PvE"). No map pin.
  // Assumed sweep window for a recurring DD sandstorm. The pod logs emit only a
  // spawn timestamp (LogSandStorm BeginPlay) with no duration, so "active" is
  // derived: a storm is treated as sweeping for STORM_ACTIVE_SECONDS after its
  // last spawn. Calibration assumption — tune if it reads early/late in-game.
  var STORM_ACTIVE_SECONDS = 180;

  function updateSandstormBanner(sandstorm) {
    var banner = el("map-sandstorm-banner");
    var txt = el("map-sandstorm-banner__text");
    if (!banner || !txt) return;
    if (!sandstorm || !sandstorm.available) {
      banner.hidden = true;
      return;
    }
    var dims = sandstorm.dimensions || {};
    var parts = [];
    var anyActive = false;
    // Scope the banner to the SELECTED instance's dimension. Each DD instance
    // (PvE = dim 0, PvP = dim 1) runs its own storm cycle, so the PvE tab must
    // never show the PvP countdown. When no instance/dim is selected (non-DD maps)
    // fall back to showing every dimension.
    var selDim = (state.instance && state.instance.dim != null) ? state.instance.dim : null;
    var dimKeys = Object.keys(dims);
    dimKeys.sort();
    dimKeys.forEach(function (dk) {
      if (selDim !== null && String(selDim) !== dk) return;
      var info = dims[dk];
      if (!info) return;
      var label = info.label || (dk === "0" ? "PvE" : "PvP");
      // Live RAM storm read carries the moving center SECTOR + heading yaw. When
      // present + fresh, show "centered <sector>, heading <dir>" (the real active
      // storm) instead of the ETA. Freshness guards against a stale last read.
      var scanned = info.storm_scanned_utc;
      var locFresh = scanned &&
        (Date.now() - new Date(scanned).getTime()) < 20 * 60 * 1000;
      if (info.storm_sector && locFresh) {
        anyActive = true;
        var hd = headingToCompass(info.heading_yaw);
        parts.push("<span class='map-storm-dim'>" + label + ":</span> " +
                   "<span class='map-storm-active'>centered " + info.storm_sector +
                   (hd ? ", heading " + hd : "") + "</span>");
        return;
      }
      // Active = a storm spawned within the assumed sweep window.
      var spawn = info.last_spawn_utc;
      var active = false;
      if (spawn) {
        var sinceMs = Date.now() - new Date(spawn).getTime();
        active = sinceMs >= 0 && sinceMs < STORM_ACTIVE_SECONDS * 1000;
      }
      if (active) {
        anyActive = true;
        parts.push("<span class='map-storm-dim'>" + label + ":</span> " +
                   "<span class='map-storm-active'>sweeping now</span>");
        return;
      }
      var eta = info.next_eta_utc;
      var etaTxt;
      if (!eta) {
        etaTxt = "—";
      } else {
        var diff = Math.round((new Date(eta).getTime() - Date.now()) / 60000);
        etaTxt = diff > 0 ? "~" + diff + "m" : "imminent";
      }
      parts.push("<span class='map-storm-dim'>" + label + ":</span> " +
                 "<span class='map-storm-eta'>" + etaTxt + "</span>");
    });
    // The selected instance has no storm data in the feed (e.g. the PvE pod had no
    // spawn in the log window after a Coriolis restart): show a sealed placeholder
    // for the selected dimension rather than hiding it or borrowing the other
    // dimension's time, so PvE never silently displays the PvP countdown.
    if (!parts.length && selDim !== null) {
      var selLabel = (state.instance && state.instance.label) ||
                     (String(selDim) === "0" ? "PvE" : "PvP");
      parts.push("<span class='map-storm-dim'>" + selLabel + ":</span> " +
                 "<span class='map-storm-eta'>—</span>");
    }
    if (!parts.length) {
      banner.hidden = true;
      return;
    }
    banner.hidden = false;
    banner.classList.toggle("map-sandstorm-banner--active", anyActive);
    var prefix = anyActive ? "Sandstorm — " : "Next sandstorm — ";
    txt.innerHTML = prefix + parts.join(" &nbsp;·&nbsp; ");
  }

  // ---- Shai-Hulud (sandworm) danger overlay (DD) --------------------------
  function wormsForInstance() {
    if (!state.worms || !state.worms.dimensions) return [];
    var dim = state.instance ? state.instance.dim : null;
    var info = state.worms.dimensions[String(dim)];
    return (info && info.worms) || [];
  }

  function drawWorms() {
    var existing = svg.querySelector(".map-worm-layer");
    if (!META.has_spice || !state.wormShow) {
      if (existing) svg.removeChild(existing);
      state.wormEls = {};
      state.wormHover = [];
      if (state.wormAnim) { cancelAnimationFrame(state.wormAnim); state.wormAnim = null; }
      return;
    }
    var layer = existing;
    if (!layer) {
      layer = document.createElementNS(SVGNS, "g");
      layer.setAttribute("class", "map-worm-layer");
      state.wormEls = {};
    }
    // Keep worms above the per-poll spice redraw (storm draws after this).
    svg.appendChild(layer);
    state.wormHover = [];
    var step = VIEW / (META.grid ? META.grid.cols : 9);
    var unit = step * 0.014;
    var reduce = prefersReducedMotion();
    var els = state.wormEls || (state.wormEls = {});
    var now = nowMs();
    var seen = {};
    wormsForInstance().forEach(function (w) {
      if (w.nx == null || w.ny == null) return;
      var id = (w.id != null) ? ("id" + w.id) : ("p" + w.nx + "_" + w.ny);
      seen[id] = true;
      var threat = w.threat || (w.surfaced ? "surfaced" : "submerged");
      var age = w.age_s || 0;
      var op = age <= 60 ? 1 : age >= 360 ? 0.45 : 1 - (age - 60) / 545;
      var rec = els[id];
      if (!rec || rec.threat !== threat) {
        // New worm, or threat changed (its wave SMIL + class depend on threat, so
        // rebuild). Snap to the reported position; no glide on first sight.
        if (rec && rec.g && rec.g.parentNode) rec.g.parentNode.removeChild(rec.g);
        var g = worm(layer, w.nx, w.ny, unit, w);
        els[id] = { g: g, threat: threat, u: unit,
                    fromX: w.nx, fromY: w.ny, toX: w.nx, toY: w.ny, t0: now };
      } else {
        // Same worm, same threat: glide from its last reported position to the new
        // one over the poll interval (dead-reckoning between 10s live reads).
        rec.u = unit;
        rec.fromX = rec.toX; rec.fromY = rec.toY;
        rec.toX = w.nx; rec.toY = w.ny; rec.t0 = now;
        rec.g.setAttribute("opacity", op.toFixed(2));
        if (reduce) {
          rec.g.setAttribute("transform",
            "translate(" + w.nx + "," + w.ny + ") scale(" + unit + ")");
        }
      }
      state.wormHover.push({ x: w.nx, y: w.ny, r: step * 0.12, label: wormLabel(w) });
    });
    // Drop worms no longer in the feed.
    Object.keys(els).forEach(function (id) {
      if (!seen[id]) {
        if (els[id].g && els[id].g.parentNode) els[id].g.parentNode.removeChild(els[id].g);
        delete els[id];
      }
    });
    if (reduce) {
      if (state.wormAnim) { cancelAnimationFrame(state.wormAnim); state.wormAnim = null; }
    } else if (!state.wormAnim) {
      state.wormAnim = requestAnimationFrame(animateWorms);
    }
  }

  // Linear dead-reckoning between live worm reads: glide each worm from its last
  // reported position to the new one over one poll interval. Runs only while
  // something is still moving, then stops until the next poll dirties the targets.
  function animateWorms() {
    state.wormAnim = null;
    var els = state.wormEls; if (!els) return;
    var now = nowMs(), dur = 10000, moving = false;
    Object.keys(els).forEach(function (id) {
      var r = els[id]; if (!r || !r.g) return;
      var f = (now - r.t0) / dur; if (f < 0) f = 0; if (f > 1) f = 1;
      var x = r.fromX + (r.toX - r.fromX) * f;
      var y = r.fromY + (r.toY - r.fromY) * f;
      r.g.setAttribute("transform",
        "translate(" + x.toFixed(1) + "," + y.toFixed(1) + ") scale(" + r.u + ")");
      if (f < 1) moving = true;
    });
    if (moving) state.wormAnim = requestAnimationFrame(animateWorms);
  }

  function nowMs() {
    return (window.performance && performance.now) ? performance.now() : Date.now();
  }

  function prefersReducedMotion() {
    return !!(window.matchMedia &&
              window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  // ---- live sandstorm overlay (DD) ---------------------------------------
  // Consensus geometry (#5): a MOVING storm whose primary, rock-solid signal is
  // its projected CENTER + a heading arrow; a translucent radius CIRCLE is added
  // when the payload carries a radius. Every layer is INDEPENDENTLY guarded, so
  // whichever the RAM reader ends up supplying just works:
  //   center_nx/center_ny  -> moving storm-position marker (primary)
  //   heading_yaw          -> heading arrow (sweep direction)
  //   radius_nr            -> translucent circle (consensus: moving circle)
  //   start_/end_ nx/ny    -> band/front line (optional dead-code; drawn only if
  //                           a future reader ever emits a swept band instead)
  // Absent all geometry (today's time-only ETA feed) -> nothing drawn, banner
  // only, zero visual change. Coriolis stage/countdown stays in the banner.
  function drawStorm() {
    var old = svg.querySelector(".map-storm-layer");
    if (old) svg.removeChild(old);
    if (!META.has_storms) return;
    // The backend re-keys each map's storms onto the INSTANCE dim, so this lookup
    // already isolates one sietch: a Kulon storm cannot draw on the Habbanya tab.
    var dim = state.instance ? state.instance.dim : null;
    if (state.hidden["sandstorm"]) return;    // filter toggle: storm layer hidden
    var info = ((state.sandstorm && state.sandstorm.dimensions) || {})[String(dim)];
    if (!info) return;
    var cx = info.center_nx, cy = info.center_ny;
    var hasCenter = isFiniteNum(cx) && isFiniteNum(cy);
    var sx = info.start_nx, sy = info.start_ny, ex = info.end_nx, ey = info.end_ny;
    var hasBand = isFiniteNum(sx) && isFiniteNum(sy) && isFiniteNum(ex) && isFiniteNum(ey);
    if (!hasCenter && !hasBand) return;       // no geometry -> banner only
    var layer = document.createElementNS(SVGNS, "g");
    layer.setAttribute("class", "map-storm-layer");
    var step = VIEW / (META.grid ? META.grid.cols : 9);
    var r = info.radius_nr;

    // (a) radius circle -- only when a numeric radius is present.
    if (hasCenter && isFiniteNum(r) && r > 0) {
      var circle = document.createElementNS(SVGNS, "circle");
      circle.setAttribute("cx", cx); circle.setAttribute("cy", cy);
      circle.setAttribute("r", r);
      circle.setAttribute("class", "map-storm-circle");
      layer.appendChild(circle);
    }

    // (b) band/front -- optional dead-code; only if start+end ever arrive.
    if (hasBand) {
      var band = document.createElementNS(SVGNS, "line");
      band.setAttribute("x1", sx); band.setAttribute("y1", sy);
      band.setAttribute("x2", ex); band.setAttribute("y2", ey);
      band.setAttribute("class", "map-storm-band");
      layer.appendChild(band);
    }

    // (c) moving storm-position marker (primary) + (d) heading arrow.
    if (hasCenter) {
      var mark = document.createElementNS(SVGNS, "circle");
      mark.setAttribute("cx", cx); mark.setAttribute("cy", cy);
      mark.setAttribute("r", step * 0.10);
      mark.setAttribute("class", "map-storm-center");
      layer.appendChild(mark);
      // Heading arrow from the storm yaw. UE yaw degrees; world +X->screen right,
      // +Y->screen down (DD cal flipY=false), so dir = (cos,sin). PROVISIONAL --
      // confirm sign/axis against a live --verify storm sweep before trusting it.
      var h = info.heading_yaw;
      if (isFiniteNum(h)) {
        var alen = (isFiniteNum(r) && r > 0) ? r : step * 0.6;
        var rad = h * Math.PI / 180;
        var ax = cx + Math.cos(rad) * alen, ay = cy + Math.sin(rad) * alen;
        var arrow = document.createElementNS(SVGNS, "line");
        arrow.setAttribute("x1", cx); arrow.setAttribute("y1", cy);
        arrow.setAttribute("x2", ax); arrow.setAttribute("y2", ay);
        arrow.setAttribute("class", "map-storm-heading");
        layer.appendChild(arrow);
      }
    }
    svg.appendChild(layer);
  }

  function isFiniteNum(v) { return typeof v === "number" && isFinite(v); }

  // Storm heading yaw -> 8-point compass, matching drawStorm's arrow convention
  // (screen dir = (cos yaw, sin yaw); +x=E, +y=S). Sign PROVISIONAL, same caveat
  // as the on-map arrow -- confirm against a live --verify sweep before trusting.
  function headingToCompass(yaw) {
    if (!isFiniteNum(yaw)) return "";
    var d = ((yaw % 360) + 360) % 360;
    return ["E", "SE", "S", "SW", "W", "NW", "N", "NE"][Math.round(d / 45) % 8];
  }

  function worm(parent, x, y, u, w) {
    var threat = w.threat || (w.surfaced ? "surfaced" : "submerged");
    var g = document.createElementNS(SVGNS, "g");
    g.setAttribute("class", "map-worm is-" + threat);
    g.setAttribute("transform", "translate(" + x + "," + y + ") scale(" + u + ")");
    var age = w.age_s || 0;
    var op = age <= 60 ? 1 : age >= 360 ? 0.45 : 1 - (age - 60) / 545;
    g.setAttribute("opacity", op.toFixed(2));
    var arrow = document.createElementNS(SVGNS, "path");
    arrow.setAttribute("d", "M-13 0 L-7 -4 L-8.5 0 L-7 4 Z");
    arrow.setAttribute("class", "map-worm__arrow");
    g.appendChild(arrow);
    var wave = document.createElementNS(SVGNS, "path");
    wave.setAttribute("class", "map-worm__wave");
    var calm = threat === "submerged";
    wave.setAttribute("d", wavePath(calm ? 0 : 1, 0));
    if (!calm) {
      var amp = threat === "breaching" ? 1.35 : threat === "enraged" ? 1.1 : 0.85;
      var dur = threat === "breaching" ? "0.45s" : threat === "enraged" ? "0.6s" : "0.85s";
      var an = document.createElementNS(SVGNS, "animate");
      an.setAttribute("attributeName", "d");
      an.setAttribute("dur", dur);
      an.setAttribute("repeatCount", "indefinite");
      an.setAttribute("calcMode", "linear");
      an.setAttribute("values", [
        wavePath(amp, 0), wavePath(amp, 1), wavePath(amp, 2),
        wavePath(amp, 3), wavePath(amp, 0)
      ].join(";"));
      wave.appendChild(an);
    }
    g.appendChild(wave);
    parent.appendChild(g);
    return g;
  }

  function wavePath(amp, phase) {
    var heights = [0, -3, 2, -8, 9, -12, 10, -7, 4, -2, 0];
    var pts = [], n = heights.length, x0 = -5, dx = 1.7;
    for (var i = 0; i < n; i++) {
      var hv = heights[(i + (phase | 0)) % n];
      var yv = (i === 0 || i === n - 1) ? 0 : hv * amp;
      pts.push((x0 + i * dx).toFixed(1) + " " + yv.toFixed(1));
    }
    return "M" + pts.join(" L");
  }

  function wormLabel(w) {
    // Use "Sandworm" in the functional proximity readout (in-game term).
    var s = "Sandworm";
    if (w.sector) s += " — sector " + w.sector;
    var tag = { breaching: "BREACHING", enraged: "enraged",
                surfaced: "roaming", submerged: "submerged" }[w.threat];
    if (tag) s += " (" + tag + ")";
    if (w.age_s != null && w.age_s > 120) s += " · " + Math.round(w.age_s / 60) + "m ago";
    return s;
  }

  function updateWormBanner() {
    var banner = el("map-worm-banner"); if (!banner) return;
    var txt = el("map-worm-banner__text");
    var ws = wormsForInstance();
    var roaming = ws.filter(function (w) { return w.threat && w.threat !== "submerged"; });
    var enraged = ws.filter(function (w) { return w.threat === "enraged" || w.threat === "breaching"; });
    var breaching = ws.filter(function (w) { return w.threat === "breaching"; });
    banner.hidden = false;
    banner.classList.toggle("is-danger", enraged.length > 0);

    // T#5: audio cue on danger transition
    if (enraged.length > 0 && state.audioOn) {
      _pingAudio();
    }

    if (!ws.length) {
      // T#5: section title uses "Shai-Hulud"
      txt.textContent = "Shai-Hulud activity updating…";
    } else if (!roaming.length) {
      txt.textContent = "No Shai-Hulud roaming right now — deep sand can still wake them.";
    } else {
      var sectors = roaming.map(function (w) { return w.sector; })
        .filter(Boolean).filter(function (v, i, a) { return a.indexOf(v) === i; });
      txt.innerHTML = "<strong>" + roaming.length + "</strong> Shai-Hulud" +
        (roaming.length > 1 ? "" : "") +
        " roaming" +
        (enraged.length ? " · <strong>" + enraged.length + " enraged</strong>" : "") +
        (breaching.length ? " · <strong>" + breaching.length + " BREACHING</strong>" : "") +
        (sectors.length ? " · " + sectors.slice(0, 6).join(", ") : "");
    }
  }

  // T#5: small audio ping for danger tier
  var _audioCtx = null;
  function _pingAudio() {
    try {
      if (!_audioCtx) _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      var osc = _audioCtx.createOscillator();
      var gain = _audioCtx.createGain();
      osc.connect(gain);
      gain.connect(_audioCtx.destination);
      osc.type = "sine";
      osc.frequency.setValueAtTime(440, _audioCtx.currentTime);
      osc.frequency.exponentialRampToValueAtTime(220, _audioCtx.currentTime + 0.18);
      gain.gain.setValueAtTime(0.18, _audioCtx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, _audioCtx.currentTime + 0.22);
      osc.start(_audioCtx.currentTime);
      osc.stop(_audioCtx.currentTime + 0.22);
    } catch (e) {}
  }

  // T#5: audio toggle button
  (function () {
    var audioBtn = el("map-worm-audio-toggle");
    if (!audioBtn) return;
    audioBtn.classList.toggle("is-on", state.audioOn);
    audioBtn.addEventListener("click", function () {
      state.audioOn = !state.audioOn;
      audioBtn.classList.toggle("is-on", state.audioOn);
      audioBtn.title = state.audioOn ? "Audio alert on" : "Audio alert off";
    });
  })();

  function updateProximity() {
    var box = el("map-worm-proximity"); if (!box) return;
    var txt = el("map-worm-proximity__text");
    var self = state.me && state.me.self;
    if (!META.has_spice || !state.meAuthed || !self || self.nx == null) {
      box.hidden = true; return;
    }
    var dims = (state.worms && state.worms.dimensions) || {};
    var info = dims[String(self.dim)];
    var worms = (info && info.worms) || [];
    if (!worms.length) { box.hidden = true; return; }
    var step = VIEW / (META.grid ? META.grid.cols : 9);
    var nearest = null, bestD = Infinity;
    worms.forEach(function (w) {
      if (w.nx == null) return;
      var dx = w.nx - self.nx, dy = w.ny - self.ny, d = dx * dx + dy * dy;
      if (d < bestD) { bestD = d; nearest = w; }
    });
    if (!nearest) { box.hidden = true; return; }
    var distSec = Math.sqrt(bestD) / step;
    var dn = distSec.toFixed(1);
    var dir = compass(nearest.nx - self.nx, nearest.ny - self.ny);
    var tier, msg;
    if (distSec <= 0.65) {
      tier = "danger";
      // Keep "Sandworm" in the functional proximity readout (in-game term per spec)
      msg = "Sandworm in your sector" + (nearest.sector ? " (" + nearest.sector + ")" : "") +
            " — get to rock NOW.";
    } else if (distSec <= 1.5) {
      tier = "warn";
      msg = "Sandworm ~" + dn + " sectors " + dir +
            ((nearest.threat === "breaching" || nearest.threat === "enraged") ? " and closing" : "") +
            " — make for rock.";
    } else if (distSec <= 3) {
      tier = "caution";
      msg = "Sandworm ~" + dn + " sectors " + dir + ".";
    } else {
      tier = "clear";
      msg = "No sandworm near you — nearest ~" + dn + " sectors " + dir + ".";
    }
    box.hidden = false;
    box.className = "map-worm-proximity is-" + tier;
    txt.textContent = msg;
  }

  function compass(dx, dy) {
    var ang = Math.atan2(dx, -dy) * 180 / Math.PI;
    if (ang < 0) ang += 360;
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(ang / 45) % 8];
  }

  // ---- per-player overlay -------------------------------------------------
  function fetchMe() {
    fetch("/portal/maps/" + META.key + "/me", { credentials: "same-origin" })
      .then(function (r) {
        if (r.status === 401) { state.meAuthed = false; return null; }
        return r.json();
      })
      .then(function (d) {
        if (!d || !d.authenticated) { state.meAuthed = false; updateMeControls(); updateProximity(); return; }
        state.meAuthed = true;
        state.me = d.available ? d : { self: null, bases: [], vehicles: [] };
        updateMeControls();
        drawMe();
        updateProximity();
        // Show the waypoints section when authenticated
        if (!state.wpAuthed) {
          state.wpAuthed = true;
          var wpSec = el("map-wp-section");
          if (wpSec) wpSec.hidden = false;
        }
      })
      .catch(function () {});
  }

  function meDimVisible(dim) {
    var sel = state.instance ? state.instance.dim : null;
    if (sel === null || sel === undefined) return true;
    if (dim === null || dim === undefined) return true;
    return Number(dim) === Number(sel);
  }

  function drawMe() {
    var old = svg.querySelector(".map-me-layer");
    if (old) svg.removeChild(old);
    state.meHover = [];
    if (!state.meAuthed || !state.me) return;
    var layer = document.createElementNS(SVGNS, "g");
    layer.setAttribute("class", "map-me-layer");
    var unit = VIEW / 1000;
    if (state.meShow.bases) {
      (state.me.bases || []).forEach(function (b) {
        if (!meDimVisible(b.dim)) return;
        meHouse(layer, b.nx, b.ny, 13 * unit, b.kind === "outpost");
        state.meHover.push({ x: b.nx, y: b.ny, r: 14 * unit,
          label: (b.name ? b.name + " — " : "") +
                 (b.kind === "outpost" ? "your outpost" : "your base") });
      });
    }
    if (state.meShow.vehicles) {
      (state.me.vehicles || []).forEach(function (v) {
        if (!meDimVisible(v.dim)) return;
        meVehicle(layer, v.nx, v.ny, 20 * unit, v.icon);
        state.meHover.push({ x: v.nx, y: v.ny, r: 12 * unit,
          label: (v.name || "Vehicle") + " (yours)" });
      });
    }
    if (state.meShow.self && state.me.self && meDimVisible(state.me.self.dim)) {
      var s = state.me.self;
      mePin(layer, s.nx, s.ny, 15 * unit, !!s.online);
      state.meHover.push({ x: s.nx, y: s.ny - 13 * unit, r: 16 * unit,
        label: s.online ? "You are here" : "You — last known position (offline)" });
    }
    svg.appendChild(layer);
  }

  function mePin(parent, x, y, r, online) {
    var g = document.createElementNS(SVGNS, "g");
    g.setAttribute("class", "map-me-pin" + (online ? " is-online" : " is-offline"));
    if (online) {
      var pulse = document.createElementNS(SVGNS, "circle");
      pulse.setAttribute("cx", x); pulse.setAttribute("cy", y);
      pulse.setAttribute("r", r * 0.95);
      pulse.setAttribute("class", "map-me-pulse");
      g.appendChild(pulse);
    }
    var tip = y, top = y - r * 1.9;
    var p = document.createElementNS(SVGNS, "path");
    p.setAttribute("d", "M" + x + " " + tip +
      "C" + (x - r) + " " + (top + r * 0.6) + " " + (x - r) + " " + top + " " + x + " " + top +
      "C" + (x + r) + " " + top + " " + (x + r) + " " + (top + r * 0.6) + " " + x + " " + tip + "Z");
    p.setAttribute("class", "map-me-pin__body");
    g.appendChild(p);
    var dot = document.createElementNS(SVGNS, "circle");
    dot.setAttribute("cx", x); dot.setAttribute("cy", top + r * 0.55);
    dot.setAttribute("r", r * 0.34);
    dot.setAttribute("class", "map-me-pin__dot");
    g.appendChild(dot);
    parent.appendChild(g);
  }

  function meHouse(parent, x, y, r, small) {
    var g = document.createElementNS(SVGNS, "g");
    g.setAttribute("class", "map-me-base" + (small ? " is-outpost" : ""));
    var p = document.createElementNS(SVGNS, "path");
    var w = r * (small ? 0.7 : 0.9);
    p.setAttribute("d",
      "M" + (x - w) + " " + (y - w * 0.1) +
      "L" + x + " " + (y - w) +
      "L" + (x + w) + " " + (y - w * 0.1) +
      "L" + (x + w) + " " + (y + w) +
      "L" + (x - w) + " " + (y + w) + "Z");
    p.setAttribute("class", "map-me-base__body");
    g.appendChild(p);
    parent.appendChild(g);
  }

  function meVehicle(parent, x, y, r, icon) {
    if (icon) {
      var img = document.createElementNS(SVGNS, "image");
      img.setAttribute("href", "/admin/static/img/containers/" + icon + ".png");
      img.setAttribute("x", x - r / 2); img.setAttribute("y", y - r / 2);
      img.setAttribute("width", r); img.setAttribute("height", r);
      img.setAttribute("class", "map-me-vehicle__icon");
      parent.appendChild(img);
    } else {
      var c = document.createElementNS(SVGNS, "circle");
      c.setAttribute("cx", x); c.setAttribute("cy", y); c.setAttribute("r", r * 0.4);
      c.setAttribute("class", "map-me-vehicle__dot");
      parent.appendChild(c);
    }
  }

  function updateMeControls() {
    var bar = el("map-me-controls");
    if (!bar) return;
    bar.hidden = !state.meAuthed;
    if (!state.meAuthed) return;
    var me = state.me || {};
    var counts = {
      self: me.self ? 1 : 0,
      bases: (me.bases || []).length,
      vehicles: (me.vehicles || []).length,
    };
    bar.querySelectorAll(".map-me-chip").forEach(function (chip) {
      var layerKey = chip.dataset.meLayer;
      var n = counts[layerKey];
      var cnt = chip.querySelector(".map-me-chip__count");
      if (cnt) cnt.textContent = n;
      chip.classList.toggle("is-empty", !n);
      chip.classList.toggle("is-off", !state.meShow[layerKey]);
    });
  }

  // ---- Waypoints ---------------------------------------------------------
  function _csrfToken() {
    // Read the CSRF token from the cookie. Must match portal.js / the backend:
    // cookie ls_portal_csrf, header X-Portal-CSRF-Token.
    var match = document.cookie.match(/\bls_portal_csrf=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function fetchWaypoints() {
    fetch("/portal/maps/" + META.key + "/waypoints", { credentials: "same-origin" })
      .then(function (r) {
        if (r.status === 401) return null;
        return r.json();
      })
      .then(function (d) {
        if (!d || !d.authenticated) return;
        state.wpAuthed = true;
        state.waypoints = d.waypoints || [];
        renderWaypointList();
        drawWaypoints();
        var wpSec = el("map-wp-section");
        if (wpSec) wpSec.hidden = false;
      })
      .catch(function () {});
  }

  function addWaypoint(nx, ny) {
    var note = window.prompt("Waypoint note (optional):", "") || "";
    if (note === null) return;  // user cancelled
    fetch("/portal/maps/" + META.key + "/waypoints", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-Portal-CSRF-Token": _csrfToken(),
      },
      body: JSON.stringify({ nx: nx, ny: ny, note: note }),
    })
      .then(function (r) {
        return r.json().then(function (d) { d = d || {}; d.__status = r.status; return d; },
                             function () { return { __status: r.status }; });
      })
      .then(function (d) {
        if (d && d.ok && d.waypoint) {
          state.waypoints.push(d.waypoint);
          renderWaypointList();
          drawWaypoints();
        } else {
          // Surface the failure instead of swallowing it (a silent 403/CSRF or
          // 401 looked like "nothing happened" -> "No waypoints yet").
          alert((d && (d.error || d.detail)) ||
            "Could not save waypoint. Try reloading the page and signing in again.");
        }
      })
      .catch(function () {
        alert("Could not save waypoint (network error). Please try again.");
      });
  }

  function deleteWaypoint(id) {
    fetch("/portal/maps/" + META.key + "/waypoints/" + id, {
      method: "DELETE",
      credentials: "same-origin",
      headers: { "X-Portal-CSRF-Token": _csrfToken() },
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.ok) {
          state.waypoints = state.waypoints.filter(function (w) { return w.id !== id; });
          renderWaypointList();
          drawWaypoints();
        }
      })
      .catch(function () {});
  }

  function renderWaypointList() {
    var list = el("map-wp-list");
    var countEl2 = el("map-wp-count");
    if (!list) return;
    list.innerHTML = "";
    if (countEl2) countEl2.textContent = state.waypoints.length ? "(" + state.waypoints.length + ")" : "";
    if (!state.waypoints.length) {
      var empty = document.createElement("li");
      empty.className = "map-wp-empty";
      empty.textContent = "No waypoints yet.";
      list.appendChild(empty);
      return;
    }
    state.waypoints.forEach(function (wp) {
      var li = document.createElement("li");
      li.className = "map-wp-item";
      li.dataset.wpId = wp.id;
      var label = wp.note || ("Pin " + wp.id);
      li.innerHTML =
        '<span class="map-wp-item__icon" aria-hidden="true">&#x25C6;</span>' +
        '<span class="map-wp-item__note">' + _esc(label) + '</span>' +
        '<button type="button" class="map-wp-item__del" data-wp-id="' + wp.id +
        '" aria-label="Delete waypoint">&times;</button>';
      li.querySelector(".map-wp-item__del").addEventListener("click", function (e) {
        e.stopPropagation();
        deleteWaypoint(wp.id);
      });
      // Click the label to pan to the waypoint
      li.querySelector(".map-wp-item__note").addEventListener("click", function () {
        panToNormalized(wp.nx, wp.ny);
      });
      list.appendChild(li);
    });
  }

  function _esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function panToNormalized(nx, ny) {
    var bs = baseScale();
    state.panX = state.display / 2 - nx * bs * state.zoom;
    state.panY = state.display / 2 - ny * bs * state.zoom;
    clampPan();
    applyTransform();
  }

  function drawWaypoints() {
    var old = svg.querySelector(".map-wp-layer");
    if (old) svg.removeChild(old);
    state.wpHover = [];
    if (!state.wpAuthed || !state.waypoints.length) return;
    var layer = document.createElementNS(SVGNS, "g");
    layer.setAttribute("class", "map-wp-layer");
    var unit = VIEW / 1000;
    state.waypoints.forEach(function (wp) {
      var x = wp.nx, y = wp.ny;
      var r = 9 * unit;
      // Diamond pin
      var p = document.createElementNS(SVGNS, "path");
      p.setAttribute("d", "M" + x + " " + (y - r * 1.6) +
        "C" + (x - r) + " " + (y - r * 0.4) + " " + (x - r) + " " + (y + r * 0.1) + " " + x + " " + (y + r) +
        "C" + (x + r) + " " + (y + r * 0.1) + " " + (x + r) + " " + (y - r * 0.4) + " " + x + " " + (y - r * 1.6) + "Z");
      p.setAttribute("class", "map-wp-pin");
      layer.appendChild(p);
      // Dot center
      var dot = document.createElementNS(SVGNS, "circle");
      dot.setAttribute("cx", x); dot.setAttribute("cy", y - r * 0.6);
      dot.setAttribute("r", r * 0.32);
      dot.setAttribute("class", "map-wp-pin__dot");
      layer.appendChild(dot);
      state.wpHover.push({ x: x, y: y - r * 0.6, r: r * 1.4,
        label: (wp.note || "Waypoint") + " (" + Math.round(x) + ", " + Math.round(y) + ")" });
    });
    svg.appendChild(layer);
  }

  // ---- click popup (marker details) --------------------------------------
  function showPopup(m, mx, my) {
    if (!popup) return;
    var cat = state.catIndex[m[2]] || "other";
    var typeName = state.typeIndex[m[3]] || "";
    // Named POIs carry a 5th element (the friendly name, e.g. "Imperial Testing
    // Station 2"); fall back to the type label for unnamed markers.
    var displayName = m[4] || typeName;
    var catLabel = "";
    if (state.data && state.data.legend) {
      state.data.legend.forEach(function (leg) {
        if (leg.key === cat) catLabel = leg.label;
      });
    }
    var bs = baseScale();
    // Convert normalized -> world (approximate, from inverse of normalize)
    var worldX = null, worldY = null;
    if (META.cal) {
      worldX = Math.round(m[0] / VIEW * META.cal.spanX + META.cal.originX);
      worldY = Math.round(m[1] / VIEW * META.cal.spanY + META.cal.originY);
    }
    if (popupName) popupName.textContent = displayName;
    if (popupCat) {
      // For a named POI, surface the type as a secondary label so the category +
      // type are both legible (e.g. name "Imperial Testing Station 2" / "Testing Station").
      popupCat.textContent = (m[4] && typeName) ? (catLabel + " · " + typeName) : catLabel;
      popupCat.style.color = state.catColors[cat] || "";
    }
    var coordStr = worldX !== null ? worldX + ", " + worldY : m[0].toFixed(0) + ", " + m[1].toFixed(0);
    if (popupCoords) popupCoords.textContent = coordStr;
    if (popupAlt) popupAlt.hidden = true;  // z absent from snapshot — never fabricate
    if (popupCopy) {
      popupCopy.onclick = function () {
        try { navigator.clipboard.writeText(coordStr); } catch (e) {}
        popupCopy.textContent = "Copied!";
        setTimeout(function () { popupCopy.textContent = "Copy coords"; }, 1400);
      };
    }

    state.popupMarker = m;
    popup.hidden = false;
    // Position near the click. mx,my are viewport-relative (clientX - viewportRect),
    // but #map-popup lives OUTSIDE .map-viewport in the DOM, so its absolute
    // left/top are measured from its own offsetParent, not the viewport. Translate
    // the viewport-relative point into the offsetParent's coordinate space, else the
    // popup lands at the page's top-left (worse the further the viewport is offset /
    // the more you zoom + pan). Both rects are window-relative, so the delta is
    // scroll-correct.
    var vRect = viewport.getBoundingClientRect();
    var pw = popup.offsetWidth || 200, ph = popup.offsetHeight || 120;
    var px = mx + 16, py = my + 16;
    if (px + pw > vRect.width) px = mx - pw - 8;
    if (py + ph > vRect.height) py = my - ph - 8;
    px = Math.max(0, px); py = Math.max(0, py);
    var opRect = (popup.offsetParent || document.body).getBoundingClientRect();
    popup.style.left = (vRect.left - opRect.left + px) + "px";
    popup.style.top = (vRect.top - opRect.top + py) + "px";
  }

  function closePopup() {
    if (popup) popup.hidden = true;
    state.popupMarker = null;
  }

  if (popupClose) popupClose.addEventListener("click", closePopup);

  // ---- zoom / pan ---------------------------------------------------------
  function setZoom(z, ox, oy) {
    z = Math.max(1, Math.min(8, z));
    if (ox != null) {
      var k = z / state.zoom;
      state.panX = ox - (ox - state.panX) * k;
      state.panY = oy - (oy - state.panY) * k;
    }
    state.zoom = z;
    clampPan();
    applyTransform();
  }
  function clampPan() {
    var size = state.display, max = 0, min = size * (1 - state.zoom);
    state.panX = Math.max(min, Math.min(max, state.panX));
    state.panY = Math.max(min, Math.min(max, state.panY));
  }

  el("map-zoom-in").addEventListener("click", function () {
    setZoom(state.zoom * 1.5, state.display / 2, state.display / 2);
  });
  el("map-zoom-out").addEventListener("click", function () {
    setZoom(state.zoom / 1.5, state.display / 2, state.display / 2);
  });
  el("map-zoom-reset").addEventListener("click", function () {
    state.zoom = 1; state.panX = 0; state.panY = 0; applyTransform();
  });
  viewport.addEventListener("wheel", function (e) {
    e.preventDefault();
    var rect = viewport.getBoundingClientRect();
    setZoom(state.zoom * (e.deltaY < 0 ? 1.15 : 1 / 1.15),
            e.clientX - rect.left, e.clientY - rect.top);
  }, { passive: false });

  var pointers = new Map();
  var drag = null, pinch = null;
  var _pointerDownNx = null, _pointerDownNy = null;

  viewport.addEventListener("pointerdown", function (e) {
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    viewport.setPointerCapture(e.pointerId);
    viewport.classList.add("is-dragging");
    if (pointers.size === 2) {
      var pts = Array.from(pointers.values());
      var rect = viewport.getBoundingClientRect();
      drag = null;
      pinch = {
        dist: Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1,
        zoom: state.zoom,
        midX: (pts[0].x + pts[1].x) / 2 - rect.left,
        midY: (pts[0].y + pts[1].y) / 2 - rect.top,
        panX: state.panX, panY: state.panY
      };
    } else if (pointers.size === 1) {
      drag = { x: e.clientX, y: e.clientY, px: state.panX, py: state.panY };
      // Record the normalized position at pointer-down for Alt+click
      var rect2 = viewport.getBoundingClientRect();
      var mx0 = e.clientX - rect2.left, my0 = e.clientY - rect2.top;
      _pointerDownNx = ((mx0 - state.panX) / state.zoom) / baseScale();
      _pointerDownNy = ((my0 - state.panY) / state.zoom) / baseScale();
    }
  });
  viewport.addEventListener("pointermove", function (e) {
    if (pointers.has(e.pointerId)) {
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    }
    if (pinch && pointers.size >= 2) {
      var pts = Array.from(pointers.values());
      var rect = viewport.getBoundingClientRect();
      var dist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1;
      var midX = (pts[0].x + pts[1].x) / 2 - rect.left;
      var midY = (pts[0].y + pts[1].y) / 2 - rect.top;
      var z = Math.max(1, Math.min(8, pinch.zoom * dist / pinch.dist));
      var wx = (pinch.midX - pinch.panX) / pinch.zoom;
      var wy = (pinch.midY - pinch.panY) / pinch.zoom;
      state.zoom = z;
      state.panX = midX - wx * z;
      state.panY = midY - wy * z;
      clampPan(); applyTransform();
    } else if (drag) {
      state.panX = drag.px + (e.clientX - drag.x);
      state.panY = drag.py + (e.clientY - drag.y);
      clampPan(); applyTransform();
    } else {
      hover(e);
    }
  });
  function endPointer(e) {
    pointers.delete(e.pointerId);
    if (pointers.size < 2) pinch = null;
    if (pointers.size === 1) {
      var p = pointers.values().next().value;
      drag = { x: p.x, y: p.y, px: state.panX, py: state.panY };
    } else if (pointers.size === 0) {
      drag = null;
      viewport.classList.remove("is-dragging");
    }
  }
  viewport.addEventListener("pointerup", endPointer);
  viewport.addEventListener("pointercancel", endPointer);
  viewport.addEventListener("pointerleave", function () { tooltip.hidden = true; });

  // ---- click: marker popup + Alt+click waypoint --------------------------
  viewport.addEventListener("click", function (e) {
    var rect = viewport.getBoundingClientRect();
    var mx = e.clientX - rect.left, my = e.clientY - rect.top;
    var nx = ((mx - state.panX) / state.zoom) / baseScale();
    var ny = ((my - state.panY) / state.zoom) / baseScale();

    // Alt+click drops a waypoint (if authenticated)
    if (e.altKey && state.wpAuthed) {
      e.preventDefault();
      addWaypoint(Math.round(nx * 10) / 10, Math.round(ny * 10) / 10);
      return;
    }

    // Find nearest marker and show popup
    var best = null, bestD = 18 / state.zoom / baseScale(); bestD *= bestD;
    for (var i = 0; i < state.markers.length; i++) {
      var m = state.markers[i];
      var cat = state.catIndex[m[2]];
      var typeIdx = m[3];
      if (typeof typeIdx === "number" && state.hidden[typeIdx]) continue;
      if (isHiddenCat(cat)) continue;
      var dx = m[0] - nx, dy = m[1] - ny, d = dx * dx + dy * dy;
      if (d < bestD) { bestD = d; best = m; }
    }
    if (best) {
      showPopup(best, mx, my);
    } else {
      closePopup();
    }
  });

  // ---- hover tooltip (nearest marker) ------------------------------------
  function hover(e) {
    var rect = viewport.getBoundingClientRect();
    var mx = e.clientX - rect.left, my = e.clientY - rect.top;
    var nx = ((mx - state.panX) / state.zoom) / baseScale();
    var ny = ((my - state.panY) / state.zoom) / baseScale();

    // per-player markers first
    for (var k = 0; k < state.meHover.length; k++) {
      var mh = state.meHover[k];
      var mdx = mh.x - nx, mdy = mh.y - ny;
      if (mdx * mdx + mdy * mdy <= mh.r * mh.r) {
        tooltip.hidden = false;
        tooltip.style.left = (mx + 12) + "px";
        tooltip.style.top = (my + 12) + "px";
        tooltip.textContent = mh.label;
        return;
      }
    }
    // waypoints
    for (var wk = 0; wk < state.wpHover.length; wk++) {
      var wh = state.wpHover[wk];
      var wdx2 = wh.x - nx, wdy2 = wh.y - ny;
      if (wdx2 * wdx2 + wdy2 * wdy2 <= wh.r * wh.r) {
        tooltip.hidden = false;
        tooltip.style.left = (mx + 12) + "px";
        tooltip.style.top = (my + 12) + "px";
        tooltip.textContent = wh.label;
        return;
      }
    }
    // sandworms
    for (var w = 0; w < state.wormHover.length; w++) {
      var wh2 = state.wormHover[w];
      var wdx = wh2.x - nx, wdy = wh2.y - ny;
      if (wdx * wdx + wdy * wdy <= wh2.r * wh2.r) {
        tooltip.hidden = false;
        tooltip.style.left = (mx + 12) + "px";
        tooltip.style.top = (my + 12) + "px";
        tooltip.textContent = wh2.label;
        return;
      }
    }
    // spice diamonds
    for (var s = 0; s < state.spiceHover.length; s++) {
      var sp = state.spiceHover[s];
      var ddx = sp.x - nx, ddy = sp.y - ny;
      if (ddx * ddx + ddy * ddy <= sp.r * sp.r) {
        tooltip.hidden = false;
        tooltip.style.left = (mx + 12) + "px";
        tooltip.style.top = (my + 12) + "px";
        tooltip.textContent = sp.label;
        return;
      }
    }
    // static markers
    var best = null, bestD = 14 / state.zoom / baseScale(); bestD *= bestD;
    for (var i = 0; i < state.markers.length; i++) {
      var m = state.markers[i];
      var cat = state.catIndex[m[2]];
      var typeIdx = m[3];
      if (typeof typeIdx === "number" && state.hidden[typeIdx]) continue;
      if (isHiddenCat(cat)) continue;
      var dx = m[0] - nx, dy = m[1] - ny, d = dx * dx + dy * dy;
      if (d < bestD) { bestD = d; best = m; }
    }
    if (best) {
      tooltip.hidden = false;
      tooltip.style.left = (mx + 12) + "px";
      tooltip.style.top = (my + 12) + "px";
      // Prefer the friendly POI name (best[4]); fall back to the type label.
      tooltip.textContent = best[4] || state.typeIndex[best[3]] || "";
    } else {
      tooltip.hidden = true;
    }
  }

  // ---- instance switch ---------------------------------------------------
  document.querySelectorAll(".map-instance-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      document.querySelectorAll(".map-instance-btn").forEach(function (b) {
        b.classList.remove("is-active"); b.setAttribute("aria-selected", "false");
      });
      btn.classList.add("is-active"); btn.setAttribute("aria-selected", "true");
      var key = btn.dataset.instance;
      state.instance = (META.instances || []).filter(function (i) { return i.key === key; })[0] || state.instance;
      // pollPlayers too: the counter is now per-dimension, so switching instance
      // must refetch or the card keeps the previous instance's number.
      drawSpice(); updateSpiceBanner(); drawWorms(); updateWormBanner(); drawStorm(); drawMe();
      pollPlayers();
    });
  });

  // T#5: Shai-Hulud layer toggle
  (function () {
    var t = el("map-worm-banner__toggle");
    if (!t) return;
    t.classList.toggle("is-on", state.wormShow);
    t.addEventListener("click", function () {
      state.wormShow = !state.wormShow;
      t.classList.toggle("is-on", state.wormShow);
      t.textContent = state.wormShow ? "shown" : "hidden";
      drawWorms();
    });
  })();

  document.querySelectorAll(".map-me-chip").forEach(function (chip) {
    chip.addEventListener("click", function () {
      var layerKey = chip.dataset.meLayer;
      state.meShow[layerKey] = !state.meShow[layerKey];
      chip.classList.toggle("is-off", !state.meShow[layerKey]);
      drawMe();
    });
  });

  window.addEventListener("resize", function () { layout(); });
  load();
})();
