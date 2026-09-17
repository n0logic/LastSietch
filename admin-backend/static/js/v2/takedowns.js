/* Content takedowns (admin wave 2, lever C).
 *
 * Owns the Takedowns section on Economy and Social and nothing else. The read
 * cards on both pages belong to systems.js, which only ever touches elements
 * carrying data-sys-card; this file only ever touches data-td-card. The two
 * never meet, which is what lets one page carry both.
 *
 * Three rules the whole file exists to enforce:
 *
 *   1. A button is dead until the operator has typed the token for the exact
 *      direction they are asking for. HIDE and SHOW arm different buttons, and
 *      REMOVE arms only the irreversible one. The server checks the same token
 *      again; this half is so nobody presses the wrong verb, not a gate.
 *   2. A reason is required before the button arms, because it is required by
 *      the server and finding that out after the press wastes the press.
 *   3. The status line reports what the SERVER read back after the write, never
 *      what we asked for. Every route re-reads its row and returns that.
 */
(function () {
  "use strict";

  if (!document.querySelector("[data-td-card]")) return;

  var API = "/admin/api/dune/v2";

  var el = function (id) { return document.getElementById(id); };

  function say(node, text, bad) {
    if (!node) return;
    node.textContent = text;
    node.className = "td-status" + (bad ? " td-status--bad" : " td-status--ok");
  }

  function disableAll(buttons) {
    for (var i = 0; i < buttons.length; i++) {
      if (buttons[i]) buttons[i].disabled = true;
    }
  }

  /* One takedown control: a set of fields, and one button per direction with
     the token that arms it. `body` builds the request from the live fields so a
     field edited between arming and pressing is the one that gets sent. */
  function wire(spec) {
    var reason = el(spec.reason), status = el(spec.status);
    var confirm = spec.confirm ? el(spec.confirm) : null;
    var subject = el(spec.subject);
    if (!reason || !subject || !status) return;
    var buttons = spec.actions.map(function (a) { return el(a.button); });

    function arm() {
      var typed = confirm ? (confirm.value || "").trim() : "";
      var ready = !!(reason.value || "").trim() && !!(subject.value || "").trim();
      spec.actions.forEach(function (a, i) {
        if (!buttons[i]) return;
        buttons[i].disabled = !(ready && (a.token === null || typed === a.token));
      });
    }

    function fire(action) {
      var payload = spec.body(subject.value, reason.value.trim(), action);
      if (payload === null) { say(status, "check the fields above", true); return; }
      disableAll(buttons);
      say(status, action.busy, false);
      apiCall("POST", API + spec.path(subject.value, action), payload)
        .then(function (res) {
          if (confirm) confirm.value = "";
          say(status, spec.report(res, action), false);
        })
        .catch(function (err) {
          say(status, err.message || "the action was refused", true);
        })
        .then(arm);
    }

    reason.addEventListener("input", arm);
    subject.addEventListener("input", arm);
    if (confirm) confirm.addEventListener("input", arm);
    spec.actions.forEach(function (a, i) {
      if (buttons[i]) buttons[i].addEventListener("click", function () { fire(a); });
    });
    arm();
  }

  function positiveInt(v) {
    var n = parseInt(v, 10);
    return (isFinite(n) && n > 0) ? n : null;
  }

  /* --- Economy: blueprint listing ---------------------------------------- */

  wire({
    subject: "td-bp-id",
    reason: "td-bp-reason",
    confirm: "td-bp-confirm",
    status: "td-bp-status",
    actions: [
      { button: "td-bp-hide", token: "HIDE", verb: "_unpublish", busy: "hiding the listing" },
      { button: "td-bp-republish", token: "REPUBLISH", verb: "_republish",
        busy: "putting the listing back on the gallery" },
      { button: "td-bp-remove", token: "REMOVE", verb: "_remove", busy: "removing the listing and its file" }
    ],
    path: function (id, action) {
      return "/moderation/blueprint/" + encodeURIComponent(positiveInt(id)) + "/" + action.verb;
    },
    body: function (id, reason, action) {
      if (positiveInt(id) === null) return null;
      return { reason: reason, confirm: action.token };
    },
    report: function (res, action) {
      var st = (res.listing && res.listing.status) || "unknown";
      var tail = action.token === "REMOVE" ? "; the blueprint file is gone" : "";
      return "listing now reads " + st + (res.changed ? "" : " (already there)") + tail;
    }
  });

  /* --- Economy: Karum force return ---------------------------------------
   *
   * The one control here that is NOT a new verb: /karum/force is the operator
   * action that already exists, with the reason field it already requires. It
   * takes no typed token, so nothing here pretends it does; the server's own
   * four-character reason minimum is the gate, and inventing a second one in
   * the browser would be theatre.
   */

  wire({
    subject: "td-karum-id",
    reason: "td-karum-reason",
    confirm: null,
    status: "td-karum-status",
    actions: [
      { button: "td-karum-return", token: null, verb: "force-return",
        busy: "handing the listing back to its seller" }
    ],
    path: function () { return "/karum/force"; },
    body: function (id, reason, action) {
      var listing = positiveInt(id);
      if (listing === null || reason.length < 4) return null;
      return { listing_id: listing, action: action.verb, reason: reason };
    },
    report: function (res) {
      if (res && res.ok === false) return res.message || res.error || "the writer refused";
      return "force return dispatched; the Karum page carries the event trail";
    }
  });

  /* --- Social: Signal Board, guild recruiting card ------------------------ */

  wire({
    subject: "td-guild-id",
    reason: "td-guild-reason",
    confirm: "td-guild-confirm",
    status: "td-guild-status",
    actions: [
      { button: "td-guild-hide", token: "HIDE", recruiting: false, busy: "closing the card" },
      { button: "td-guild-show", token: "SHOW", recruiting: true, busy: "reopening the card" }
    ],
    path: function (id) {
      return "/moderation/signal/guild/" + encodeURIComponent(positiveInt(id));
    },
    body: function (id, reason, action) {
      if (positiveInt(id) === null) return null;
      return { recruiting: action.recruiting, reason: reason, confirm: action.token };
    },
    report: function (res) {
      var card = res.card || {};
      return "guild " + card.guild_id + " is now " +
             (card.recruiting ? "listed" : "closed") + ", updated " + card.updated_at;
    }
  });

  /* --- Social: Signal Board, seeker card ---------------------------------- */

  wire({
    subject: "td-seeker-name",
    reason: "td-seeker-reason",
    confirm: "td-seeker-confirm",
    status: "td-seeker-status",
    actions: [
      { button: "td-seeker-hide", token: "HIDE", active: false, busy: "expiring the card" },
      { button: "td-seeker-show", token: "SHOW", active: true, busy: "restoring the card" }
    ],
    path: function () { return "/moderation/signal/seeker"; },
    body: function (name, reason, action) {
      return { char_name: (name || "").trim(), active: action.active,
               reason: reason, confirm: action.token };
    },
    report: function (res) {
      var card = res.card || {};
      return card.char_name + " is now " + (card.active ? "on the seeker wall" :
             "expired at " + card.expires_at);
    }
  });

  /* --- Social: player directory ------------------------------------------ */

  wire({
    subject: "td-dir-name",
    reason: "td-dir-reason",
    confirm: "td-dir-confirm",
    status: "td-dir-status",
    actions: [
      { button: "td-dir-hide", token: "HIDE", listed: false, busy: "unlisting the profile" },
      { button: "td-dir-show", token: "SHOW", listed: true, busy: "relisting the profile" }
    ],
    path: function () { return "/moderation/directory"; },
    body: function (name, reason, action) {
      return { char_name: (name || "").trim(), listed: action.listed,
               reason: reason, confirm: action.token };
    },
    report: function (res) {
      var p = res.profile || {};
      return p.char_name + " is now " + (p.listed ? "listed" : "unlisted") +
             "; the blurb is untouched (" + p.blurb_len + " chars)";
    }
  });

})();
