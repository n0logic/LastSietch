/* Events Manager (ADMIN-02). One list, one editor, one preview.
 *
 * The server is the source of truth for every time: the form sends Eastern wall
 * time (starts_et / ends_et) and the list shows what the server stored, in UTC
 * and Eastern, plus the viewer's own zone. The status line only ever reports
 * what came back from a write, never what we asked for.
 */
(function () {
  "use strict";

  var root = document.getElementById("v2-events");
  if (!root) return;

  var API = "/admin/api/dune/v2/events";
  var el = function (id) { return document.getElementById(id); };
  var events = [];
  var filter = "upcoming";
  var current = null;

  var ET = new Intl.DateTimeFormat(undefined, { timeZone: "America/New_York", dateStyle: "medium", timeStyle: "short" });
  // dateStyle/timeStyle cannot be combined with timeZoneName (the constructor throws).
  var LOCAL = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric",
    minute: "2-digit", timeZoneName: "short" });

  function asDate(stored) { return stored ? new Date(stored.replace(" ", "T") + "Z") : null; }

  function when(stored) {
    var d = asDate(stored);
    if (!d || isNaN(d)) return "";
    return ET.format(d) + " ET · " + stored.slice(0, 16) + " UTC";
  }

  function etToUtcLabel(value) {
    if (!value) return "";
    // Hint only: the server does the real conversion. Find the UTC instant whose
    // Eastern wall time equals the typed value (two passes settle DST edges).
    var guess = new Date(value + ":00Z");
    for (var i = 0; i < 2; i++) {
      var parts = new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York", year: "numeric", month: "2-digit",
        day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(guess);
      var p = {}; parts.forEach(function (x) { p[x.type] = x.value; });
      var shown = new Date(p.year + "-" + p.month + "-" + p.day + "T" + p.hour + ":" + p.minute + ":00Z");
      guess = new Date(guess.getTime() + (new Date(value + ":00Z") - shown));
    }
    return "= " + guess.toISOString().slice(0, 16).replace("T", " ") + " UTC · " + LOCAL.format(guess) + " your time";
  }

  function say(text, bad) {
    var m = el("ev-msg");
    m.textContent = text || "";
    m.className = "ev__msg" + (bad ? " ev__msg--bad" : "");
  }

  function ensureCsrf() { return csrfToken ? Promise.resolve() : fetchCsrf(); }

  /* --- list --------------------------------------------------------------- */

  // Same split as the portal board: an event that has started and not ended
  // (a weekend effect mid-window) is still upcoming, not past. No end = past once started.
  function over(ev, now) {
    var start = asDate(ev.starts_utc);
    var end = asDate(ev.ends_utc);
    return start < now && (!end || end <= now);
  }

  function matches(ev) {
    var now = new Date();
    if (filter === "cancelled") return ev.status === "cancelled";
    if (filter === "draft") return ev.status === "draft";
    if (filter === "past") return ev.status === "published" && over(ev, now);
    return ev.status === "published" && !over(ev, now);
  }

  function renderList() {
    var ul = el("ev-rows");
    ul.textContent = "";
    var rows = events.filter(matches);
    if (filter === "upcoming" || filter === "draft") rows.reverse();
    if (!rows.length) {
      var li = document.createElement("li");
      li.className = "ev__empty";
      li.textContent = "Nothing here.";
      ul.appendChild(li);
      return;
    }
    rows.forEach(function (ev) {
      var li = document.createElement("li");
      var b = document.createElement("button");
      b.type = "button";
      b.className = "ev__row" + (current && current.id === ev.id ? " ev__row--on" : "");
      var t = document.createElement("span"); t.className = "ev__row-title"; t.textContent = ev.title;
      var s = document.createElement("span"); s.className = "ev__row-when"; s.textContent = when(ev.starts_utc);
      var tag = document.createElement("span"); tag.className = "ev__tag ev__tag--" + ev.status;
      tag.textContent = ev.status + (ev.reminders ? " · " + ev.reminders + " reminders" : "")
        + (ev.discord_sync ? " · Discord " + (ev.discord_state || "pending") : "")
        + (ev.effect && ev.effect !== "none" ? " · " + ev.effect + " " + (ev.effect_state || "not armed") : "");
      b.appendChild(t); b.appendChild(s); b.appendChild(tag);
      b.addEventListener("click", function () { open(ev); });
      li.appendChild(b);
      ul.appendChild(li);
    });
  }

  function load() {
    return apiCall("GET", API).then(function (res) {
      events = res.events || [];
      if (current) {
        var fresh = events.filter(function (e) { return e.id === current.id; })[0];
        if (fresh) current = fresh;
      }
      renderList();
    }).catch(function (err) {
      el("ev-rows").innerHTML = "";
      var li = document.createElement("li"); li.className = "ev__empty";
      li.textContent = "Events could not be read: " + (err.message || "error");
      el("ev-rows").appendChild(li);
    });
  }

  /* --- editor ------------------------------------------------------------- */

  var form = el("ev-form");
  var fields = {
    title: el("ev-title-input"), kind: el("ev-kind"), banner: el("ev-banner"),
    starts_et: el("ev-starts"), ends_et: el("ev-ends"), map: el("ev-map"),
    host: el("ev-host"), description: el("ev-description"),
    effect: el("ev-effect"), effect_scope: el("ev-effect-scope"), effect_hours: el("ev-effect-hours")
  };

  var EFFECT_STATES = {
    armed: "Armed: the runner applies it at the start.",
    running: "Running: applied and read back on every target map.",
    done: "Done: applied and removed.",
    failed: "Failed",
    missed: "Missed"
  };

  function effectState(ev) {
    if (!ev || !ev.effect || ev.effect === "none") return "";
    var line = ev.effect_state ? EFFECT_STATES[ev.effect_state] || ev.effect_state
      : "Not armed: publish to arm it.";
    if (ev.effect_job) line += " A " + ev.effect_job + " job is in flight since " + ev.effect_job_at + " UTC.";
    if (ev.effect_error) line += (ev.effect_state === "failed" || ev.effect_state === "missed" ? ": " : " Note: ") + ev.effect_error;
    return line;
  }

  function effectInputs() {
    var on = fields.effect.value !== "none";
    el("ev-effect-opts").hidden = !on;
    el("ev-ends-box").hidden = on;
  }

  // Publishing an effect event syncs it to Discord by default (plan Stage 3). The
  // form always sends the box, so the store's default never applies here: tick it
  // when an unpublished event gains an effect, and leave an operator's untick alone.
  var lastEffect = "none";
  fields.effect.addEventListener("change", function () {
    var published = current && current.status === "published";
    if (fields.effect.value !== "none" && lastEffect === "none" && !published) el("ev-discord-sync").checked = true;
    lastEffect = fields.effect.value;
    effectInputs();
  });

  function discordState(ev) {
    if (!ev || (!ev.discord_sync && !ev.discord_status)) return "";
    if (ev.discord_state === "failed") return "Discord sync failed: " + (ev.discord_error || "unknown error") + ". Retrying.";
    if (ev.discord_state === "synced") return "On Discord (" + (ev.discord_status || "scheduled") + ").";
    return "Discord sync pending.";
  }

  function open(ev) {
    current = ev || null;
    el("ev-editor").hidden = false;
    el("ev-editor-title").textContent = ev ? "Edit event" : "New event";
    fields.title.value = ev ? ev.title : "";
    fields.kind.value = ev ? ev.kind : "community";
    fields.banner.value = ev && ev.banner ? ev.banner : "";
    fields.starts_et.value = ev ? (ev.starts_et || "") : "";
    fields.ends_et.value = ev ? (ev.ends_et || "") : "";
    fields.map.value = ev && ev.map ? ev.map : "";
    fields.host.value = ev && ev.host ? ev.host : "";
    el("ev-discord-sync").checked = !!(ev && ev.discord_sync);
    el("ev-discord-sync").disabled = !!(ev && ev.status === "cancelled");
    el("ev-discord-state").textContent = discordState(ev);
    fields.description.value = ev ? (ev.description || "") : "";
    fields.effect.value = ev && ev.effect ? ev.effect : "none";
    lastEffect = fields.effect.value;
    fields.effect_scope.value = ev && ev.effect_scope ? ev.effect_scope : "fleet";
    fields.effect_hours.value = String(ev && ev.effect_hours ? ev.effect_hours : 72);
    effectInputs();
    el("ev-effect-state").textContent = effectState(ev);
    el("ev-effect-rearm").hidden = !(ev && ev.status === "published" && !ev.effect_job
      && (ev.effect_state === "failed" || ev.effect_state === "missed"));
    var cancelled = ev && ev.status === "cancelled";
    var inFlight = ev && (ev.effect_state === "running" || ev.effect_job);
    Object.keys(fields).forEach(function (k) { fields[k].disabled = !!cancelled; });
    ["effect", "effect_scope", "effect_hours", "starts_et", "ends_et"].forEach(function (k) {
      if (inFlight) fields[k].disabled = true;
    });
    el("ev-save-draft").hidden = !!cancelled;
    el("ev-publish").hidden = !!cancelled;
    el("ev-save-draft").textContent = ev && ev.status === "published" ? "Unpublish (back to draft)" : "Save draft";
    el("ev-publish").textContent = ev && ev.status === "published" ? "Save changes" : "Publish";
    el("ev-state").textContent = !ev ? "" : cancelled
      ? "Cancelled " + (ev.cancelled_at || "") + " UTC. Reason: " + (ev.cancel_reason || "none given") + ". Read-only."
      : (ev.status === "published" ? "Published, visible to players." : "Draft, visible to admins only.")
        + (ev.reminders ? " " + ev.reminders + " players asked for a reminder." : "");
    el("ev-cancel-box").hidden = !ev || cancelled;
    el("ev-cancel-reason").value = "";
    el("ev-cancel-confirm").value = "";
    el("ev-cancel").disabled = true;
    el("ev-history-box").hidden = !ev;
    say("");
    hints();
    preview();
    renderList();
    if (ev) history(ev.id);
    fields.title.focus();
  }

  function payload(status) {
    var body = {
      title: fields.title.value, kind: fields.kind.value, banner: fields.banner.value,
      starts_et: fields.starts_et.value, ends_et: fields.ends_et.value,
      map: fields.map.value, host: fields.host.value, description: fields.description.value,
      status: status, discord_sync: el("ev-discord-sync").checked, effect: fields.effect.value
    };
    if (body.effect !== "none") {
      body.effect_scope = fields.effect_scope.value;
      // A running effect's window is frozen server-side; send nothing that moves it.
      // An empty select is a legacy window (not one of the lengths): leave the end as stored.
      if (!fields.effect_hours.disabled && fields.effect_hours.value) body.effect_hours = fields.effect_hours.value;
    }
    return body;
  }

  el("ev-effect-rearm").addEventListener("click", function () {
    if (!current) return;
    var body = payload("published");
    body.effect_rearm = true;
    delete body.effect_hours;
    ensureCsrf().then(function () { return apiCall("POST", API + "/" + current.id, body); })
      .then(function (res) { current = res.event; return load().then(function () { open(current); say("Effect re-armed."); }); })
      .catch(function (err) { say(err.message || "The re-arm was refused.", true); });
  });

  el("ev-generate").addEventListener("click", function () {
    var m = el("ev-generate-msg");
    m.textContent = "Generating";
    ensureCsrf().then(function () { return apiCall("POST", API + "/generate", {}); })
      .then(function (res) {
        m.textContent = res.month + ": " + res.created.length + " drafts created"
          + (res.skipped.length ? ", " + res.skipped.length + " weekends already had one" : "") + ".";
        filter = "draft";
        document.querySelectorAll(".ev__filter").forEach(function (x) { x.classList.toggle("ev__filter--on", x.dataset.filter === "draft"); });
        return load();
      })
      .catch(function (err) { m.textContent = err.message || "Generate was refused."; });
  });

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var status = (e.submitter && e.submitter.dataset.status) || "draft";
    if (!fields.title.value.trim() || !fields.starts_et.value) { say("A title and a start time are required.", true); return; }
    var path = current ? API + "/" + current.id : API;
    say("Saving");
    ensureCsrf().then(function () { return apiCall("POST", path, payload(status)); })
      .then(function (res) {
        var ev = res.event;
        var msg = (ev.status === "published" ? "Published. Players see it now." : "Saved as a draft. Players cannot see it.")
          + (res.rearmed ? " The start moved, so reminders will go out again." : "");
        current = ev;
        return load().then(function () { open(current); say(msg); });
      })
      .catch(function (err) { say(err.message || "The save was refused.", true); });
  });

  el("ev-new").addEventListener("click", function () { open(null); });
  el("ev-close").addEventListener("click", function () { current = null; el("ev-editor").hidden = true; renderList(); });

  function armCancel() {
    el("ev-cancel").disabled = !(el("ev-cancel-reason").value.trim() && el("ev-cancel-confirm").value.trim() === "CANCEL");
  }
  el("ev-cancel-reason").addEventListener("input", armCancel);
  el("ev-cancel-confirm").addEventListener("input", armCancel);
  el("ev-cancel").addEventListener("click", function () {
    if (!current) return;
    el("ev-cancel").disabled = true;
    ensureCsrf().then(function () {
      return apiCall("POST", API + "/" + current.id + "/cancel",
        { confirm: el("ev-cancel-confirm").value.trim(), reason: el("ev-cancel-reason").value.trim() });
    }).then(function (res) {
      current = res.event;
      return load().then(function () { open(current); say("Cancelled. " + res.notified + " reminder notices sent."); });
    }).catch(function (err) { say(err.message || "The cancel was refused.", true); armCancel(); });
  });

  /* --- hints, preview, history -------------------------------------------- */

  function hints() {
    el("ev-starts-utc").textContent = etToUtcLabel(fields.starts_et.value);
    el("ev-ends-utc").textContent = etToUtcLabel(fields.ends_et.value);
  }

  function preview() {
    var banner = fields.banner.value;
    var img = el("ev-preview-banner");
    img.hidden = !banner;
    if (banner) img.src = API + "/banner/" + encodeURIComponent(banner);
    el("ev-preview-kind").textContent = fields.kind.value.replace("_", " ");
    el("ev-preview-title").textContent = fields.title.value || "Untitled event";
    el("ev-preview-when").textContent = fields.starts_et.value
      ? fields.starts_et.value.replace("T", " ") + " ET " + etToUtcLabel(fields.starts_et.value) : "No start time yet";
    var meta = [fields.map.value && "Map: " + fields.map.value, fields.host.value && "Host: " + fields.host.value].filter(Boolean);
    el("ev-preview-meta").textContent = meta.join(" · ");
    el("ev-preview-desc").textContent = fields.description.value;
  }

  Object.keys(fields).forEach(function (k) {
    fields[k].addEventListener("input", function () { hints(); preview(); });
    fields[k].addEventListener("change", function () { hints(); preview(); });
  });

  var LABELS = { portal_event_create: "created", portal_event_update: "edited", portal_event_cancel: "cancelled" };

  function history(id) {
    var ol = el("ev-history");
    ol.textContent = "";
    apiCall("GET", API + "/" + id + "/history").then(function (res) {
      (res.history || []).forEach(function (h) {
        var li = document.createElement("li");
        var head = document.createElement("strong");
        head.textContent = h.at + " UTC · " + (LABELS[h.action] || h.action) + " by " + h.by;
        li.appendChild(head);
        var changed = (h.details && h.details.changed) || {};
        Object.keys(changed).forEach(function (k) {
          var d = document.createElement("div");
          d.className = "ev__history-change";
          d.textContent = k + ": " + (changed[k][0] || "(empty)") + " → " + (changed[k][1] || "(empty)");
          li.appendChild(d);
        });
        if (!Object.keys(changed).length && h.details && h.details.status) {
          var s = document.createElement("div"); s.className = "ev__history-change";
          s.textContent = "status " + h.details.status + " (before field history was recorded)";
          li.appendChild(s);
        }
        ol.appendChild(li);
      });
      if (!ol.children.length) { var li = document.createElement("li"); li.textContent = "No history recorded."; ol.appendChild(li); }
    }).catch(function () { var li = document.createElement("li"); li.textContent = "History could not be read."; ol.appendChild(li); });
  }

  document.querySelectorAll(".ev__filter").forEach(function (b) {
    b.addEventListener("click", function () {
      filter = b.dataset.filter;
      document.querySelectorAll(".ev__filter").forEach(function (x) { x.classList.toggle("ev__filter--on", x === b); });
      renderList();
    });
  });

  load();
})();
