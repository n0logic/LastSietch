/* World & Maps > Live Map.
 *
 * One full-viewport map per INSTANCE. Everything it draws arrives in a single
 * /api/dune/v2/world/layers response, one envelope per layer, so a layer whose
 * source is down turns its own legend row into an explained "unavailable" and
 * the rest of the map keeps working. Nothing here fetches per layer.
 *
 * The legend mirrors the player portal's idiom (static/js/portal-maps.js):
 * state.hidden is a key -> true map, a saved choice wins over the seed, the seed
 * is derived from the layer model rather than hardcoded per map, and every
 * toggle persists. The storage key is admin_map_layers_<KEY> -- deliberately NOT
 * the portal's map_layers_<KEY>, because these are different layer sets and
 * sharing the key would have each side silently hide rows the other invented.
 *
 * Deep Desert opens with the MEDIUM spice fields off (Wave 1.1 ruling). The
 * default is seeded from the presence of a medium layer in the payload, so a map
 * that never had one seeds nothing.
 */
(function () {
  "use strict";

  var root = document.getElementById("wm");
  if (!root) return;

  var SVGNS = "http://www.w3.org/2000/svg";
  var VIEW = parseInt(root.dataset.view, 10) || 1000;
  var POLL_MS = 20000;
  var PIN_PX = 4.5;            // on-screen pin radius, held constant under zoom

  /* Raster map tokens (2026-09-04). One <symbol> per icon, built once into
     <defs>, so a three-frame ornithopter strip is ONE CSS animation however
     many thopters are in the air. Names match static/img/v2/map-icons/v1/
     (manifest.json). ICON_PX is the on-screen box per family, held constant
     under zoom exactly like PIN_PX. A row with no token keeps the circle, so
     an unknown vehicle class or an owner with no activity band never wears a
     picture that claims more than the feed knows. */
  var ICON_BASE = "/admin/static/img/v2/map-icons/v1/";
  var ICONS = {
    "orni-light": 3, "orni-medium": 3, "orni-transport": 3,
    "sandbike": 1, "buggy": 1, "sandcrawler": 1,
    "base-active": 1, "base-quiet": 1, "base-dormant": 1, "base-abandoned": 1,
    "base-orphaned": 1, "base-stored": 1,
    "player-anon": 1, "player-amber": 1
  };
  var ICON_PX = { orni: 26, ground: 20, base: 20, player: 16 };
  var FRAME = 96;
  var FLAP_DUR = "0.45s";
  var REDUCED_MOTION = !!(window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches);

  /* "0;-96;-192;0": one stop per frame, back to the first. */
  function flapValues(frames) {
    var v = [];
    for (var i = 0; i < frames; i++) v.push(String(-i * FRAME));
    v.push("0");
    return v.join(";");
  }
  var MIN_ZOOM = 1, MAX_ZOOM = 12;

  var MAPS = [];
  try {
    MAPS = JSON.parse((document.getElementById("wm-maps") || {}).textContent || "[]");
  } catch (e) {
    MAPS = [];
  }
  if (!MAPS.length) {
    console.error("world map: the map model did not parse; nothing to draw");
    return;
  }

  var el = function (id) { return document.getElementById(id); };
  var mapSel = el("wm-map"), instSel = el("wm-instance");
  var viewport = el("wm-viewport"), stage = el("wm-stage");
  var backdrop = el("wm-backdrop"), svg = el("wm-svg");
  var legendBox = el("wm-legend"), banner = el("wm-banner");
  var popover = el("wm-popover"), popTitle = el("wm-popover-title"),
      popBody = el("wm-popover-body"), popLink = el("wm-popover-link");
  var refreshedEl = el("wm-refreshed"), staleEl = el("wm-stale");

  var state = {
    mapKey: MAPS[0].key,
    instance: (MAPS[0].instances && MAPS[0].instances[0] && MAPS[0].instances[0].key) || null,
    payload: null,
    hidden: {},
    layersInit: false,
    zoom: 1, panX: 0, panY: 0, size: 0, offX: 0, offY: 0,
    pinId: null,
    timer: null
  };

  /* Layer catalogue. `live` marks the two layers the counters poll: those are
     the only things on this page allowed to wear Ibad blue. */
  var LAYERS = [
    { key: "players", label: "Players", swatch: "--wm-player", live: true },
    { key: "vehicles", label: "Vehicles", swatch: "--wm-vehicle", live: true },
    { key: "bases", label: "Claims by owner", swatch: "--wm-base" },
    { key: "spice_large", label: "Spice, large", swatch: "--wm-spice-large" },
    { key: "spice_medium", label: "Spice, medium", swatch: "--wm-spice-medium" },
    { key: "worms", label: "Sandworms", swatch: "--wm-worm" },
    { key: "storms", label: "Sandstorm", swatch: "--wm-storm" },
    { key: "waypoints", label: "Waypoints", swatch: "--wm-waypoint" },
    { key: "rescue", label: "Rescues", swatch: "--wm-rescue" }
  ];

  function mapFor(key) {
    for (var i = 0; i < MAPS.length; i++) if (MAPS[i].key === key) return MAPS[i];
    return MAPS[0];
  }

  function esc(s) {
    var d = document.createElement("div");
    d.textContent = (s === null || s === undefined) ? "" : String(s);
    return d.innerHTML;
  }

  function num(v) { return typeof v === "number" && isFinite(v); }

  // --- layer state -------------------------------------------------------

  function layersKey(k) { return "admin_map_layers_" + k; }

  /* Seeded from the layer model, never from the map key: a board that carries a
     medium spice layer opens with it hidden, a board without one seeds nothing. */
  function defaultLayers(model) {
    var hasMedium = model.some(function (row) { return row.key === "spice_medium"; });
    return hasMedium ? { spice_medium: true } : {};
  }

  function restoreLayers(model) {
    try {
      var saved = JSON.parse(localStorage.getItem(layersKey(state.mapKey)) || "null");
      if (saved && typeof saved === "object" && !Array.isArray(saved)) return saved;
    } catch (e) { /* storage blocked: fall through to the seed */ }
    return defaultLayers(model);
  }

  function persistLayers() {
    try {
      localStorage.setItem(layersKey(state.mapKey), JSON.stringify(state.hidden));
    } catch (e) { /* storage blocked: the choice just does not survive a reload */ }
  }

  // --- legend ------------------------------------------------------------

  /* One row per layer that this map could have, carrying its own availability.
     A layer the map does not have at all (worms off Hagga) is dropped rather
     than shown greyed out; a layer the map HAS but could not load stays, with
     its error, because "no data" and "source down" must not look the same. */
  function legendModel(layers) {
    var model = [];
    LAYERS.forEach(function (spec) {
      var env, count = null;
      if (spec.key === "spice_large" || spec.key === "spice_medium") {
        var spice = layers.spice || {};
        if (spice.applicable === false) return;
        if (spice.available === false) {
          model.push({ key: spec.key, label: spec.label, swatch: spec.swatch,
                       available: false, error: spice.error, count: null });
          return;
        }
        var sized = spice[spec.key === "spice_large" ? "large" : "medium"] || {};
        model.push({ key: spec.key, label: spec.label, swatch: spec.swatch,
                     available: true, count: sized.count || 0,
                     defaultHidden: !!sized.default_hidden });
        return;
      }
      env = layers[spec.key] || {};
      /* A layer this map structurally does not have is dropped; a layer it HAS
         but could not load stays, with its error, because "no data" and "source
         down" must not look the same. The server says which is which with the
         `applicable` flag, so the legend never keys on error prose. */
      if (env.applicable === false) return;
      if (env.available === false) {
        model.push({ key: spec.key, label: spec.label, swatch: spec.swatch,
                     available: false, error: env.error, count: null, live: spec.live });
        return;
      }
      count = num(env.count) ? env.count : (env.storm ? 1 : 0);
      model.push({ key: spec.key, label: spec.label, swatch: spec.swatch,
                   available: true, count: count, live: spec.live,
                   note: env.withheld || null });
    });
    return model;
  }

  function buildLegend(model) {
    legendBox.innerHTML = "";
    model.forEach(function (row) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "wm__layer" + (state.hidden[row.key] ? " is-off" : "") +
        (row.available ? "" : " is-out");
      btn.dataset.layer = row.key;
      btn.setAttribute("role", "listitem");
      btn.setAttribute("aria-pressed", state.hidden[row.key] ? "false" : "true");
      btn.innerHTML =
        '<span class="wm__swatch" style="background: var(' + row.swatch + ')"></span>' +
        '<span class="wm__layer-label">' + esc(row.label) + '</span>' +
        '<span class="wm__layer-count">' +
          (row.available ? esc(row.count === null ? "" : row.count) : "n/a") +
        '</span>';
      if (row.available) {
        btn.addEventListener("click", function () {
          state.hidden[row.key] = !state.hidden[row.key];
          btn.classList.toggle("is-off", !!state.hidden[row.key]);
          btn.setAttribute("aria-pressed", state.hidden[row.key] ? "false" : "true");
          persistLayers();
          draw();
        });
      } else {
        btn.disabled = true;
      }
      legendBox.appendChild(btn);

      var note = row.available ? row.note : row.error;
      if (note) {
        var p = document.createElement("p");
        p.className = "wm__layer-note" + (row.available ? "" : " wm__layer-note--error");
        p.textContent = note;
        legendBox.appendChild(p);
      }
    });
  }

  // --- geometry ----------------------------------------------------------

  /* The stage is a square inscribed in the viewport, so the backdrop image and
     the 0..1000 SVG frame share one box and a marker cannot drift off its
     terrain when the window is not square. */
  function fit() {
    var r = viewport.getBoundingClientRect();
    state.size = Math.max(1, Math.min(r.width, r.height));
    state.offX = (r.width - state.size) / 2;
    state.offY = (r.height - state.size) / 2;
    stage.style.width = state.size + "px";
    stage.style.height = state.size + "px";
  }

  function applyTransform() {
    stage.style.transform =
      "translate(" + (state.offX + state.panX) + "px," + (state.offY + state.panY) + "px)" +
      " scale(" + state.zoom + ")";
  }

  function pinRadius() {
    return PIN_PX * VIEW / (state.size * state.zoom);
  }

  function setZoom(z, cx, cy) {
    var next = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, z));
    if (next === state.zoom) return;
    /* Keep the point under the cursor put: solve for the pan that leaves the
       stage-local coordinate of (cx, cy) unchanged across the scale change. */
    var lx = (cx - state.offX - state.panX) / state.zoom;
    var ly = (cy - state.offY - state.panY) / state.zoom;
    state.panX = cx - state.offX - lx * next;
    state.panY = cy - state.offY - ly * next;
    state.zoom = next;
    applyTransform();
    draw();
  }

  // --- drawing -----------------------------------------------------------

  function ensureDefs() {
    if (svg.querySelector("defs.wm__defs")) return;
    var defs = document.createElementNS(SVGNS, "defs");
    defs.setAttribute("class", "wm__defs");
    Object.keys(ICONS).forEach(function (name) {
      var frames = ICONS[name];
      var sym = document.createElementNS(SVGNS, "symbol");
      sym.setAttribute("id", "wmi-" + name);
      sym.setAttribute("viewBox", "0 0 " + FRAME + " " + FRAME);
      var img = document.createElementNS(SVGNS, "image");
      img.setAttribute("href", ICON_BASE + name + ".webp");
      img.setAttribute("width", FRAME);
      img.setAttribute("height", FRAME * frames);
      if (frames > 1) {
        /* A nested svg clips to one frame; the strip scrolls behind it. SMIL,
           not a CSS transform: a <use> clone repaints a SMIL-driven attribute,
           while a CSS animation inside a <symbol> was not seen to move the
           clones (headless capture, 2026-09-04). Reduced motion gets a still
           strip: the <animate> is never appended, since SMIL ignores CSS. */
        var win = document.createElementNS(SVGNS, "svg");
        win.setAttribute("width", FRAME);
        win.setAttribute("height", FRAME);
        win.setAttribute("viewBox", "0 0 " + FRAME + " " + FRAME);
        img.setAttribute("class", "wm__flap");
        if (!REDUCED_MOTION) {
          var an = document.createElementNS(SVGNS, "animate");
          an.setAttribute("attributeName", "y");
          an.setAttribute("values", flapValues(frames));
          an.setAttribute("calcMode", "discrete");
          an.setAttribute("dur", FLAP_DUR);
          an.setAttribute("repeatCount", "indefinite");
          img.appendChild(an);
        }
        win.appendChild(img);
        sym.appendChild(win);
      } else {
        sym.appendChild(img);
      }
      defs.appendChild(sym);
    });
    svg.appendChild(defs);
  }

  function marker(nx, ny, px, name, cls, data) {
    if (!num(nx) || !num(ny)) return null;      // never emit a NaN marker
    if (!ICONS[name]) return null;
    var box = px * VIEW / (state.size * state.zoom);
    var u = document.createElementNS(SVGNS, "use");
    u.setAttribute("href", "#wmi-" + name);
    u.setAttribute("x", (nx - box / 2).toFixed(2));
    u.setAttribute("y", (ny - box / 2).toFixed(2));
    u.setAttribute("width", box.toFixed(2));
    u.setAttribute("height", box.toFixed(2));
    u.setAttribute("class", "wm__pin wm__icon " + cls);
    if (data) u.__wm = data;
    return u;
  }

  var ORNI_TIERS = { light: 1, medium: 1, transport: 1 };
  function vehicleIcon(v) {
    if (v.t === "ornithopter") return "orni-" + (ORNI_TIERS[v.st] ? v.st : "light");
    if (v.t === "sandbike" || v.t === "buggy" || v.t === "sandcrawler") return v.t;
    return null;                  // container, treadwheel, unknown: the circle
  }

  var BASE_STATES = { active: 1, quiet: 1, dormant: 1, abandoned: 1 };
  function baseIcon(b) {
    if (b.ownership === "orphaned") return "base-orphaned";
    if (b.ownership === "stored_backup") return "base-stored";
    if (BASE_STATES[b.owner_activity]) return "base-" + b.owner_activity;
    return null;                  // an owner with no activity band: the circle
  }

  function playerIcon() { return "player-anon"; }

  /* Six hundred parked vehicles at 1x would tile the board with full-size
     tokens; they shrink toward the old pin size when zoomed out and reach
     full size from 2x, where a cluster has room to spread. */
  function iconPx(name) {
    var px = ICON_PX.ground;
    if (name.indexOf("orni-") === 0) px = ICON_PX.orni;
    else if (name.indexOf("base-") === 0) px = ICON_PX.base;
    else if (name.indexOf("player-") === 0) px = ICON_PX.player;
    var k = Math.max(0.6, Math.min(1, state.zoom / 2));
    return px * k;
  }

  function circle(nx, ny, r, cls, data) {
    if (!num(nx) || !num(ny)) return null;      // never emit a NaN marker
    var c = document.createElementNS(SVGNS, "circle");
    c.setAttribute("cx", nx.toFixed(1));
    c.setAttribute("cy", ny.toFixed(1));
    c.setAttribute("r", r.toFixed(2));
    c.setAttribute("class", "wm__pin " + cls);
    if (data) c.__wm = data;
    return c;
  }

  function drawGrid(frag, meta) {
    var g = meta.grid;
    if (!g) return;
    var step = VIEW / g.cols, i, j;
    var letters = (g.row_letters || "").split("");
    if (letters.length && g.row_top_is && letters[letters.length - 1] === g.row_top_is) {
      letters.reverse();
    }
    for (i = 1; i < g.cols; i++) {
      var vl = document.createElementNS(SVGNS, "line");
      vl.setAttribute("x1", i * step); vl.setAttribute("x2", i * step);
      vl.setAttribute("y1", 0); vl.setAttribute("y2", VIEW);
      vl.setAttribute("class", "wm__gridline");
      frag.appendChild(vl);
    }
    for (i = 1; i < g.rows; i++) {
      var hl = document.createElementNS(SVGNS, "line");
      hl.setAttribute("y1", i * (VIEW / g.rows)); hl.setAttribute("y2", i * (VIEW / g.rows));
      hl.setAttribute("x1", 0); hl.setAttribute("x2", VIEW);
      hl.setAttribute("class", "wm__gridline");
      frag.appendChild(hl);
    }
    for (i = 0; i < g.rows && i < letters.length; i++) {
      for (j = 0; j < g.cols; j++) {
        var t = document.createElementNS(SVGNS, "text");
        t.setAttribute("x", j * step + 8);
        t.setAttribute("y", i * (VIEW / g.rows) + 26);
        t.setAttribute("class", "wm__gridlabel");
        t.textContent = letters[i] + (j + 1);
        frag.appendChild(t);
      }
    }
  }

  function on(key) { return !state.hidden[key]; }

  function draw() {
    var p = state.payload;
    if (!p) return;
    var layers = p.layers || {};
    var r = pinRadius();
    var frag = document.createDocumentFragment();

    drawGrid(frag, p);

    function plot(list, cls, key, mapper, iconFor) {
      if (!on(key) || !list) return;
      list.forEach(function (row) {
        var data = mapper ? mapper(row) : null;
        var name = iconFor ? iconFor(row) : null;
        var c = name ? marker(row.nx, row.ny, iconPx(name), name, cls, data)
                     : circle(row.nx, row.ny, r, cls, data);
        if (c && data && data.vaultTotem) {
          c.setAttribute('tabindex', '0');
          c.setAttribute('role', 'button');
          c.setAttribute('aria-label', 'Select claim ' + data.title);
        }
        if (c) frag.appendChild(c);
      });
    }

    var spice = layers.spice || {};
    if (spice.available !== false) {
      plot((spice.large || {}).sites, "wm__pin--spice-large", "spice_large", function (s) {
        return { title: "Spice, large", rows: [["Sector", s.sector]] };
      });
      plot((spice.medium || {}).sites, "wm__pin--spice-medium", "spice_medium", function (s) {
        return { title: "Spice, medium",
                 rows: [["Sector", s.sector], ["Erupted", s.active ? "yes" : "no"]] };
      });
    }

    if ((layers.bases || {}).available) {
      plot(layers.bases.points, "wm__pin--base", "bases", function (b) {
        return {
          title: b.label || ("Claim " + b.totem_id),
          rows: [["Owner", b.owner_name || "unknown"],
                 ["Activity", b.owner_activity || "n/a"],
                 ["Ownership", b.ownership || "n/a"],
                 ["Totem", b.totem_id]],
          href: b.href,
          vaultTotem: b.ownership === "stored_backup" ? null : b.totem_id
        };
      }, baseIcon);
    }

    if ((layers.waypoints || {}).available) {
      plot(layers.waypoints.points, "wm__pin--waypoint", "waypoints", function (w) {
        return {
          title: "Waypoint",
          rows: [["Player", w.character_name || w.account_id],
                 ["Note", w.note || "(none)"],
                 ["Added", w.created_at]],
          href: w.href
        };
      });
    }

    if ((layers.rescue || {}).available && on("rescue")) {
      (layers.rescue.points || []).forEach(function (x) {
        var c = circle(x.nx, x.ny, r * 1.3,
          "wm__pin--rescue" + (x.open ? " wm__pin--rescue-open" : ""), {
            title: x.open ? "Rescue, within cooldown" : "Rescue",
            rows: [["Player", x.character_name || x.account_id],
                   ["Used", x.used_at],
                   ["Map", x.from_map]],
            href: x.href
          });
        if (c) frag.appendChild(c);
      });
    }

    if ((layers.worms || {}).available) {
      plot(layers.worms.worms, "wm__pin--worm", "worms", function (w) {
        return { title: "Sandworm " + (w.id || ""),
                 rows: [["Sector", w.sector], ["Threat", w.threat],
                        ["Surfaced", w.surfaced ? "yes" : "no"]] };
      });
    }

    var storm = (layers.storms || {}).storm || {};
    if ((layers.storms || {}).available && on("storms") &&
        num(storm.center_nx) && num(storm.center_ny)) {
      var ring = document.createElementNS(SVGNS, "circle");
      ring.setAttribute("cx", storm.center_nx);
      ring.setAttribute("cy", storm.center_ny);
      ring.setAttribute("r", num(storm.radius_nr) ? storm.radius_nr : 40);
      ring.setAttribute("class", "wm__ring");
      frag.appendChild(ring);
    }

    /* Vehicles then players last: the live layers sit on top of the static ones,
       which is the reading order an operator wants when they overlap. */
    if ((layers.vehicles || {}).available) {
      plot(layers.vehicles.points, "wm__pin--vehicle", "vehicles", function (v) {
        return { title: "Vehicle",
                 rows: [["Type", v.t + (v.st ? " (" + v.st + ")" : "")]] };
      }, vehicleIcon);
    }
    /* Motion is a liveness claim: a stale vehicle feed holds every wing still.
       pauseAnimations() is the root svg's SMIL clock; the flap is the only
       SMIL on this page, so pausing it pauses exactly the wings. */
    var vehiclesStale = !!((layers.vehicles || {}).stale);
    svg.classList.toggle("is-stale", vehiclesStale);
    if (svg.pauseAnimations) {
      if (vehiclesStale) svg.pauseAnimations(); else svg.unpauseAnimations();
    }
    /* The live feed carries the character name and the account id (the public
       projection strips both; this page reads the raw side behind require_admin),
       so a player pin names the player. Instance and partition come off the
       envelope rather than the point: every dot drawn here already routed
       through _routes_here for the selected instance. */
    if ((layers.players || {}).available) {
      var inst = p.instance || {};
      plot(layers.players.points, "wm__pin--player", "players", function (pl) {
        var rows = [["Account", pl.account_id === null || pl.account_id === undefined
                                ? "unknown" : pl.account_id]];
        if (inst.label) {
          rows.push(["Instance", inst.label + (inst.mode ? " (" + inst.mode + ")" : "")]);
        }
        if (inst.part !== null && inst.part !== undefined) {
          rows.push(["Partition", inst.part]);
        }
        return { title: pl.name || "Player", rows: rows, href: pl.href };
      }, playerIcon);
    }

    /* Clear the markers but keep <defs>: the symbols (and the one flap
       animation) survive every poll instead of restarting with it. */
    Array.prototype.slice.call(svg.childNodes).forEach(function (n) {
      if (!(n.tagName && n.tagName.toLowerCase() === "defs")) svg.removeChild(n);
    });
    ensureDefs();
    svg.appendChild(frag);
  }

  // --- popover -----------------------------------------------------------

  function closePopover() { popover.hidden = true; }

  function openPopover(data, ev) {
    if (!data) return closePopover();
    popTitle.textContent = data.title || "";
    popBody.innerHTML = (data.rows || []).map(function (row) {
      return "<dt>" + esc(row[0]) + "</dt><dd>" + esc(row[1]) + "</dd>";
    }).join("");
    if (data.href) { popLink.href = data.href; popLink.hidden = false; }
    else { popLink.hidden = true; }
    var vaultButton = el("wm-popover-vault");
    vaultButton.hidden = !data.vaultTotem;
    if (data.vaultTotem) vaultButton.dataset.baseVault = data.vaultTotem;
    else delete vaultButton.dataset.baseVault;
    var r = viewport.getBoundingClientRect();
    popover.hidden = false;
    var w = popover.offsetWidth, h = popover.offsetHeight;
    var x = Math.min(Math.max(8, ev.clientX - r.left + 12), r.width - w - 8);
    var y = Math.min(Math.max(8, ev.clientY - r.top + 12), r.height - h - 8);
    popover.style.left = x + "px";
    popover.style.top = y + "px";
  }

  svg.addEventListener("click", function (ev) {
    var t = ev.target;
    if (t && t.__wm) openPopover(t.__wm, ev);
    else closePopover();
  });
  el("wm-popover-close").addEventListener("click", closePopover);
  svg.addEventListener("keydown", function (ev) {
    if (ev.key !== "Enter" && ev.key !== " ") return;
    var target = ev.target;
    if (!target || !target.__wm || !target.__wm.vaultTotem) return;
    ev.preventDefault();
    var rect = target.getBoundingClientRect();
    openPopover(target.__wm, { clientX: rect.x + rect.width / 2, clientY: rect.y + rect.height / 2 });
    el("wm-popover-vault").focus();
  });

  // --- pan / zoom --------------------------------------------------------

  var drag = null;
  viewport.addEventListener("mousedown", function (ev) {
    if (ev.button !== 0) return;
    drag = { x: ev.clientX, y: ev.clientY, panX: state.panX, panY: state.panY, moved: false };
    viewport.classList.add("is-panning");
  });
  window.addEventListener("mousemove", function (ev) {
    if (!drag) return;
    state.panX = drag.panX + (ev.clientX - drag.x);
    state.panY = drag.panY + (ev.clientY - drag.y);
    if (Math.abs(ev.clientX - drag.x) + Math.abs(ev.clientY - drag.y) > 3) drag.moved = true;
    applyTransform();
  });
  window.addEventListener("mouseup", function () {
    if (drag) viewport.classList.remove("is-panning");
    drag = null;
  });
  viewport.addEventListener("wheel", function (ev) {
    ev.preventDefault();
    var r = viewport.getBoundingClientRect();
    setZoom(state.zoom * (ev.deltaY < 0 ? 1.2 : 1 / 1.2),
            ev.clientX - r.left, ev.clientY - r.top);
  }, { passive: false });

  el("wm-zoom-in").addEventListener("click", function () {
    var r = viewport.getBoundingClientRect();
    setZoom(state.zoom * 1.4, r.width / 2, r.height / 2);
  });
  el("wm-zoom-out").addEventListener("click", function () {
    var r = viewport.getBoundingClientRect();
    setZoom(state.zoom / 1.4, r.width / 2, r.height / 2);
  });
  el("wm-zoom-reset").addEventListener("click", function () {
    state.zoom = 1; state.panX = 0; state.panY = 0;
    applyTransform(); draw();
  });
  window.addEventListener("resize", function () { fit(); applyTransform(); draw(); });

  // --- chrome ------------------------------------------------------------

  function renderBackdrop(meta) {
    var b = (meta && meta.backdrop) || {};
    backdrop.className = "wm__backdrop";
    backdrop.style.backgroundImage = "";
    if (b.type === "image" && b.src) {
      backdrop.style.backgroundImage = 'url("' + b.src + '")';
    } else {
      backdrop.classList.add("wm__backdrop--sand");
    }
  }

  function renderCounts(layers) {
    var players = layers.players || {}, vehicles = layers.vehicles || {}, bases = layers.bases || {};
    el("wm-count-players").textContent = players.available ? (players.count || 0) : "n/a";
    el("wm-count-vehicles").textContent = vehicles.available ? (vehicles.count || 0) : "n/a";
    el("wm-count-bases").textContent = bases.available ? (bases.count || 0) : "n/a";
    staleEl.hidden = !(players.stale || vehicles.stale || bases.stale);
  }

  function renderBanner(layers) {
    var bits = [];
    var storm = (layers.storms || {}).storm || {};
    if ((layers.storms || {}).available && storm.next_storm_eta_s) {
      bits.push("Next storm in about " +
        Math.max(0, Math.round(storm.next_storm_eta_s / 60)) + " min");
    }
    var rescue = layers.rescue || {};
    if (rescue.available && rescue.open_count) {
      bits.push(rescue.open_count + " rescue" + (rescue.open_count === 1 ? "" : "s") +
        " within the 1 hour cooldown");
    }
    if (!bits.length) { banner.hidden = true; banner.textContent = ""; return; }
    banner.hidden = false;
    banner.textContent = bits.join(" · ");
  }

  function verdict(v) {
    if (!v || v.id === null || v.id === undefined) return "none";
    var conf = num(v.confidence) ? v.confidence.toFixed(3) : "n/a";
    return v.id + " at " + conf + (v.source ? " (" + v.source + ")" : "");
  }

  function renderLayout(data) {
    var head = el("wm-layout-head"), card = el("wm-layout");
    if (state.mapKey !== "deep-desert" || !data || data.available === false) {
      head.hidden = true; card.hidden = true; renderPinPanel(null); return;
    }
    head.hidden = false; card.hidden = false;
    el("wm-layout-id").textContent = (data.id === null || data.id === undefined) ? "unknown" : data.id;
    el("wm-layout-cycle").textContent = data.cycle_key || "unknown";
    var src = el("wm-layout-source");
    src.textContent = data.source || "unknown";
    src.className = "wm__source wm__source--" + (data.source || "fallback");
    el("wm-layout-conf").textContent = num(data.confidence) ? data.confidence.toFixed(3) : "n/a";
    /* Effective and matcher are separate rows because with a pin in place they
       disagree, and that disagreement is the reason an operator opens this. */
    el("wm-layout-matcher").textContent = verdict(data.matcher);
    var ov = data.override;
    var ovEl = el("wm-layout-override");
    if (!ov) {
      ovEl.textContent = "none";
    } else if (ov.unreadable) {
      ovEl.textContent = "unreadable file";
    } else {
      ovEl.textContent = ov.id + (ov.active ? "" : " (stale cycle " + ov.cycle_key + ", ignored)");
    }
    var noteEl = el("wm-layout-note");
    if (ov && ov.note) {
      noteEl.textContent = '"' + ov.note + '" pinned ' + (ov.set_utc || "at an unknown time");
      noteEl.hidden = false;
    } else {
      noteEl.hidden = true;
    }
    /* Deep Desert's terrain is re-rolled each Coriolis cycle, so the flat sand
       is only a placeholder: once the layout is identified there is a baked
       rock-island plate for it, and that is the real backdrop. */
    if (data.backdrop) {
      backdrop.className = "wm__backdrop";
      backdrop.style.backgroundImage = 'url("' + data.backdrop + '")';
    }
    renderPinPanel(data);
  }

  /* --- owner-only layout pin ---------------------------------------------
   *
   * The panel is only in the DOM for the owner (the page route decides with the
   * same require_owner the POSTs enforce), so every function here no-ops when
   * it is absent. Both actions re-read the layout from the server afterwards
   * rather than trusting their own request: only the re-read proves what landed.
   */

  var pinBox = el("wm-pin"), pinHead = el("wm-pin-head");
  var pinGrid = el("wm-pin-grid"), pinNote = el("wm-pin-note");
  var pinConfirm = el("wm-pin-confirm"), pinStatus = el("wm-pin-status");
  var pinApply = el("wm-pin-apply"), pinClear = el("wm-pin-clear");

  function renderPinPanel(data) {
    if (!pinBox) return;
    var show = !!data && state.mapKey === "deep-desert";
    pinBox.hidden = !show;
    pinHead.hidden = !show;
    if (!show) return;
    var ov = (data && data.override) || null;
    if (state.pinId === null && ov && ov.id !== null && ov.id !== undefined) {
      state.pinId = ov.id;
    }
    markPinSelection();
  }

  function markPinSelection() {
    if (!pinGrid) return;
    var thumbs = pinGrid.querySelectorAll(".wm__pin-thumb");
    for (var i = 0; i < thumbs.length; i++) {
      var chosen = parseInt(thumbs[i].getAttribute("data-layout-id"), 10) === state.pinId;
      thumbs[i].classList.toggle("wm__pin-thumb--on", chosen);
      thumbs[i].setAttribute("aria-checked", chosen ? "true" : "false");
    }
    armPinButtons();
  }

  function armPinButtons() {
    if (!pinApply) return;
    var typed = (pinConfirm.value || "").trim();
    pinApply.disabled = !(typed === "PIN" && state.pinId !== null);
    pinClear.disabled = typed !== "CLEAR";
  }

  function pinSay(text, bad) {
    if (!pinStatus) return;
    pinStatus.textContent = text;
    pinStatus.className = "wm__pin-status" + (bad ? " wm__pin-status--bad" : "");
  }

  function pinPost(path, body, busyText) {
    pinApply.disabled = true; pinClear.disabled = true;
    pinSay(busyText, false);
    return apiCall("POST", "/admin/api/dune/v2/world/dd-layout" + path, body)
      .then(function (res) {
        pinConfirm.value = "";
        var ov = res && res.override;
        pinSay(ov ? ("layout " + ov.id + " is pinned for cycle " + ov.cycle_key +
                     "; it expires at the next Coriolis reset")
                  : "no pin is set; Deep Desert is back on the matcher", false);
        loadLayout();
      })
      .catch(function (err) {
        pinSay(err.message || "the pin was refused", true);
      })
      .then(armPinButtons);
  }

  if (pinGrid) {
    pinGrid.addEventListener("click", function (ev) {
      var btn = ev.target.closest(".wm__pin-thumb");
      if (!btn) return;
      state.pinId = parseInt(btn.getAttribute("data-layout-id"), 10);
      markPinSelection();
    });
    pinConfirm.addEventListener("input", armPinButtons);
    pinApply.addEventListener("click", function () {
      pinPost("/_pin", { id: state.pinId, note: pinNote.value || "", confirm: "PIN" },
              "pinning layout " + state.pinId);
    });
    pinClear.addEventListener("click", function () {
      pinPost("/_clear", { confirm: "CLEAR" }, "clearing the pin");
    });
  }

  function renderInstances() {
    var meta = mapFor(state.mapKey);
    instSel.innerHTML = "";
    (meta.instances || []).forEach(function (inst) {
      var o = document.createElement("option");
      o.value = inst.key;
      o.textContent = inst.label + (inst.mode ? " (" + inst.mode + ")" : "");
      instSel.appendChild(o);
    });
    if (!state.instance || !(meta.instances || []).some(function (i) { return i.key === state.instance; })) {
      state.instance = (meta.instances && meta.instances[0] && meta.instances[0].key) || null;
    }
    instSel.value = state.instance || "";
  }

  // --- load --------------------------------------------------------------

  function load() {
    var url = "/admin/api/dune/v2/world/layers?map=" + encodeURIComponent(state.mapKey) +
              "&instance=" + encodeURIComponent(state.instance || "");
    return apiCall("GET", url).then(function (data) {
      state.payload = data;
      var layers = data.layers || {};
      var model = legendModel(layers);
      /* The saved choice wins; the seed applies once per board, which is what
         makes "medium spice off" a default rather than an override. */
      if (!state.layersInit) {
        state.hidden = restoreLayers(model);
        state.layersInit = true;
      }
      renderBackdrop(data);
      buildLegend(model);
      renderCounts(layers);
      renderBanner(layers);
      draw();
      refreshedEl.textContent = new Date().toLocaleTimeString();
    }).catch(function () {
      refreshedEl.textContent = "refresh failed";
      return false;   // tells window.v2Poll to back off
    });
  }

  function loadLayout() {
    if (state.mapKey !== "deep-desert") { renderLayout(null); return; }
    apiCall("GET", "/admin/api/dune/v2/world/dd-layout")
      .then(renderLayout)
      .catch(function () { renderLayout({ available: false }); });
  }

  function selectBoard() {
    state.layersInit = false;
    state.hidden = {};
    state.zoom = 1; state.panX = 0; state.panY = 0;
    closePopover();
    renderInstances();
    fit();
    applyTransform();
    load();
    loadLayout();
  }

  mapSel.addEventListener("change", function () {
    state.mapKey = mapSel.value;
    state.instance = null;
    selectBoard();
  });
  instSel.addEventListener("change", function () {
    state.instance = instSel.value;
    closePopover();
    load();
  });

  mapSel.value = state.mapKey;
  selectBoard();
  /* immediate:false because selectBoard() has just loaded. window.v2Poll stops
     the 20s refresh while the tab is hidden and doubles the gap, up to 4x,
     while the layers call keeps failing. */
  state.poll = window.v2Poll(load, POLL_MS, { immediate: false });
})();
