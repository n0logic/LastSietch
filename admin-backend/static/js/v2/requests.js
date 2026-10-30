/* Player requests queue (F7). One filtered list, one drawer.
 *
 * The drawer only ever shows what the server returned: a save answers with the
 * stored row and its audit history, and those replace what was on screen. A save
 * carries the updated_at it was opened with, so two staff editing one request
 * get a 409 instead of a silent overwrite.
 */
(function () {
  "use strict";

  var root = document.getElementById("v2-requests");
  if (!root) return;

  var API = "/admin/api/dune/v2/requests";
  var el = function (id) { return document.getElementById(id); };
  var state = { status: "open", rows: [], staff: [], current: null };
  var FIELD_LABELS = { status: "status", staff_note: "staff note", player_note: "player note", assignee: "assignee" };
  var REF_LABELS = { totem: "Base (totem)", vehicle: "Vehicle" };

  function ensureCsrf() { return csrfToken ? Promise.resolve() : fetchCsrf(); }

  function node(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function short(stamp) { return stamp ? String(stamp).slice(0, 16) + " UTC" : ""; }

  function say(id, text, bad) {
    var m = el(id);
    m.textContent = text || "";
    m.className = "rq__msg" + (bad ? " rq__msg--bad" : "");
  }

  /* --- list --------------------------------------------------------------- */

  function query() {
    var p = new URLSearchParams();
    p.set("status", state.status);
    if (el("rq-type").value) p.set("type", el("rq-type").value);
    if (el("rq-assignee").value) p.set("assignee", el("rq-assignee").value);
    if (el("rq-q").value.trim()) p.set("q", el("rq-q").value.trim());
    return API + "?" + p.toString();
  }

  function fillStaff(select, keep) {
    var fixed = select.id === "rq-assignee" ? 3 : 1;
    while (select.options.length > fixed) select.remove(fixed);
    state.staff.forEach(function (name) {
      if (select.id === "rq-assignee" && select.options[2].value === name) return;
      select.appendChild(new Option(name, name));
    });
    if (keep === undefined) return;
    // An assignee whose account was since deactivated stays selectable, so a save
    // that does not touch it does not silently clear it.
    if (keep && !Array.prototype.some.call(select.options, function (o) { return o.value === keep; })) {
      select.appendChild(new Option(keep, keep));
    }
    select.value = keep;
  }

  function renderCounts(counts) {
    root.querySelectorAll("[data-count]").forEach(function (b) {
      var n = counts ? counts[b.getAttribute("data-count")] : 0;
      b.textContent = n ? String(n) : "";
    });
  }

  function renderRows() {
    var body = el("rq-rows");
    body.textContent = "";
    if (!state.rows.length) {
      var tr = node("tr"); var td = node("td", "rq__empty", "No requests match.");
      td.colSpan = 7; tr.appendChild(td); body.appendChild(tr);
      return;
    }
    state.rows.forEach(function (r) {
      var tr = node("tr");
      tr.setAttribute("data-id", r.id);
      tr.tabIndex = 0;
      if (state.current && state.current.id === r.id) tr.className = "rq__row--on";
      tr.appendChild(node("td", "rq__id", "#" + r.id));
      tr.appendChild(node("td", null, r.type_label));
      tr.appendChild(node("td", null, r.char_name || ""));
      tr.appendChild(node("td", "rq__muted", r.totem_ref || r.vehicle_ref || ""));
      var st = node("td"); st.appendChild(node("span", "rq__status rq__status--" + r.status, r.status)); tr.appendChild(st);
      tr.appendChild(node("td", r.assignee ? null : "rq__muted", r.assignee || "nobody"));
      tr.appendChild(node("td", "rq__muted", short(r.created_at)));
      tr.addEventListener("click", function () { openDetail(r.id); });
      tr.addEventListener("keydown", function (e) { if (e.key === "Enter") openDetail(r.id); });
      body.appendChild(tr);
    });
  }

  function load() {
    return apiCall("GET", query()).then(function (res) {
      state.rows = res.requests || [];
      state.staff = res.staff || [];
      fillStaff(el("rq-assignee"), el("rq-assignee").value);
      renderCounts(res.counts);
      renderRows();
    }).catch(function (err) {
      var body = el("rq-rows"); body.textContent = "";
      var tr = node("tr"); var td = node("td", "rq__empty", "Requests could not be read: " + (err.message || "error"));
      td.colSpan = 7; tr.appendChild(td); body.appendChild(tr);
    });
  }

  /* --- drawer ------------------------------------------------------------- */

  function openDrawer(mode) {
    el("rq-detail").hidden = mode !== "detail";
    el("rq-create").hidden = mode !== "create";
    el("rq-drawer").classList.add("rq-drawer--open");
    el("rq-drawer").setAttribute("aria-hidden", "false");
  }

  function closeDrawer() {
    el("rq-drawer").classList.remove("rq-drawer--open");
    el("rq-drawer").setAttribute("aria-hidden", "true");
    state.current = null;
    renderRows();
  }

  function metaRow(dl, label, value) {
    if (value === null || value === undefined || value === "") return;
    dl.appendChild(node("dt", null, label));
    var dd = node("dd");
    if (value instanceof Node) dd.appendChild(value); else dd.textContent = value;
    dl.appendChild(dd);
  }

  function describe(h) {
    var d = h.details;
    if (h.action === "player_request_create") return "filed on the portal";
    if (h.action === "player_request_log") return "logged by staff";
    if (!d || typeof d !== "object") return h.action;
    return Object.keys(d).map(function (k) {
      var pair = d[k] || [];
      var a = pair[0] === null || pair[0] === "" ? "none" : pair[0];
      var b = pair[1] === null || pair[1] === "" ? "none" : pair[1];
      if (k === "staff_note" || k === "player_note") return FIELD_LABELS[k] + " edited";
      return (FIELD_LABELS[k] || k) + ": " + a + " to " + b;
    }).join("; ");
  }

  function renderDetail(r, history) {
    state.current = r;
    el("rq-drawer-title").textContent = "#" + r.id + " · " + r.type_label;
    var dl = el("rq-meta"); dl.textContent = "";
    var who = r.char_name || "";
    if (r.account_id) {
      var a = node("a", null, who);
      a.href = "/admin/v2/players/" + encodeURIComponent(r.account_id);
      metaRow(dl, "Player", a);
    } else {
      metaRow(dl, "Player", who);
    }
    metaRow(dl, "Source", r.source === "staff" ? "Logged by staff (not visible to the player)" : "Portal");
    metaRow(dl, "Base (totem)", r.totem_ref);
    metaRow(dl, "Vehicle", r.vehicle_ref);
    metaRow(dl, "Details", r.details);
    metaRow(dl, "Filed", short(r.created_at));
    metaRow(dl, "Updated", short(r.updated_at));
    metaRow(dl, "Closed", short(r.closed_at));

    var route = el("rq-route"); route.textContent = "";
    route.appendChild(document.createTextNode("Fulfil with: "));
    if (r.tool_href) {
      var link = node("a", null, r.tool); link.href = r.tool_href; route.appendChild(link);
    } else {
      route.appendChild(node("strong", null, r.tool || "no tool"));
      route.appendChild(document.createTextNode(" (not built yet; do it by hand and note what you did)"));
    }

    el("rq-status").value = r.status;
    fillStaff(el("rq-assign"), r.assignee || "");
    el("rq-staff-note").value = r.staff_note || "";
    el("rq-player-note").value = r.player_note || "";
    say("rq-msg", "");

    var ol = el("rq-history"); ol.textContent = "";
    (history || []).forEach(function (h) {
      var li = node("li");
      li.appendChild(node("div", null, short(h.at) + " · " + (h.by || "?")));
      li.appendChild(node("div", "rq__history-change", describe(h)));
      ol.appendChild(li);
    });
    if (!ol.children.length) ol.appendChild(node("li", "rq__muted", "No history."));
    renderRows();
  }

  function openDetail(id) {
    openDrawer("detail");
    say("rq-msg", "Loading");
    apiCall("GET", API + "/" + encodeURIComponent(id)).then(function (res) {
      state.staff = res.staff || state.staff;
      renderDetail(res.request, res.history);
    }).catch(function (err) { say("rq-msg", err.message || "Could not read the request.", true); });
  }

  el("rq-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var r = state.current;
    if (!r) return;
    var body = {
      status: el("rq-status").value,
      assignee: el("rq-assign").value,
      staff_note: el("rq-staff-note").value,
      player_note: el("rq-player-note").value,
      updated_at: r.updated_at
    };
    say("rq-msg", "Saving");
    ensureCsrf().then(function () {
      return apiCall("POST", API + "/" + encodeURIComponent(r.id), body);
    }).then(function (res) {
      renderDetail(res.request, res.history);
      say("rq-msg", res.changed && res.changed.length ? "Saved: " + res.changed.map(function (k) { return FIELD_LABELS[k] || k; }).join(", ") + "." : "Nothing changed.");
      load();
    }).catch(function (err) { say("rq-msg", err.message || "Save failed.", true); });
  });

  /* --- log a request ------------------------------------------------------- */

  function syncRefLabel() {
    var opt = el("rq-c-type").selectedOptions[0];
    el("rq-c-ref-label").textContent = REF_LABELS[opt ? opt.getAttribute("data-ref") : "totem"] || "Reference";
  }

  el("rq-new").addEventListener("click", function () {
    state.current = null;
    el("rq-drawer-title").textContent = "Log a request";
    el("rq-create-form").reset();
    syncRefLabel();
    say("rq-c-msg", "");
    openDrawer("create");
    el("rq-c-name").focus();
  });
  el("rq-c-type").addEventListener("change", syncRefLabel);

  el("rq-create-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var ref = el("rq-c-type").selectedOptions[0].getAttribute("data-ref");
    var body = { type: el("rq-c-type").value, char_name: el("rq-c-name").value, details: el("rq-c-details").value };
    body[ref === "vehicle" ? "vehicle_ref" : "totem_ref"] = el("rq-c-ref").value;
    say("rq-c-msg", "Saving");
    ensureCsrf().then(function () { return apiCall("POST", API, body); }).then(function (res) {
      load();
      openDetail(res.request.id);
    }).catch(function (err) { say("rq-c-msg", err.message || "Could not log the request.", true); });
  });

  /* --- wiring ------------------------------------------------------------- */

  root.querySelectorAll(".rq__pill").forEach(function (b) {
    b.addEventListener("click", function () {
      state.status = b.getAttribute("data-status");
      root.querySelectorAll(".rq__pill").forEach(function (x) { x.classList.toggle("rq__pill--on", x === b); });
      load();
    });
  });
  el("rq-type").addEventListener("change", load);
  el("rq-assignee").addEventListener("change", load);
  var typing = null;
  el("rq-q").addEventListener("input", function () { clearTimeout(typing); typing = setTimeout(load, 250); });
  el("rq-drawer-close").addEventListener("click", closeDrawer);
  el("rq-drawer").addEventListener("click", function (e) { if (e.target === el("rq-drawer")) closeDrawer(); });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && el("rq-drawer").classList.contains("rq-drawer--open")) closeDrawer();
  });

  var deep = /[?&]id=(\d+)/.exec(window.location.search);
  load().then(function () { if (deep) openDetail(deep[1]); });
})();
