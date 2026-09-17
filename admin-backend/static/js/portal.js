/* Last Sietch Dune Portal — small vanilla JS layer.
 * (a) Read ls_portal_csrf cookie and inject as X-Portal-CSRF-Token header
 *     on data-portal-action POSTs (fetch path).
 * (b) Mirror the cookie value into hidden csrf_token form inputs on submit
 *     so the no-JS form-POST fallback also carries the token.
 * (c) Disable submit buttons + aria-busy during in-flight quiz POSTs.
 * (d) Drive [data-portal-countdown-seconds] / [data-portal-countdown-target]
 *     widgets — non-blocking, cosmetic.
 * No HTMX. No innerHTML= concat. No JS redirects (server-side 302s only).
 */
(function () {
  'use strict';

  var CSRF_COOKIE = 'ls_portal_csrf';
  var CSRF_HEADER = 'X-Portal-CSRF-Token';

  function getCookie(name) {
    var match = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return match ? decodeURIComponent(match[1]) : '';
  }

  /* Ensure every CSRF-bearing form has a hidden csrf_token populated at submit.
   * Templates render an empty hidden input server-side; we hydrate at submit. */
  function hydrateCsrfForms() {
    var forms = document.querySelectorAll('form[data-portal-csrf-form]');
    Array.prototype.forEach.call(forms, function (form) {
      form.addEventListener('submit', function () {
        var token = getCookie(CSRF_COOKIE);
        var input = form.querySelector('input[name="csrf_token"]');
        if (input && !input.value) {
          input.value = token;
        }
      });
    });
  }

  /* Disable submit buttons + add aria-busy while a form is in-flight. */
  function wireSubmitBusy() {
    var forms = document.querySelectorAll('form[data-portal-busy-on-submit]');
    Array.prototype.forEach.call(forms, function (form) {
      form.addEventListener('submit', function () {
        var btn = form.querySelector('button[type="submit"], input[type="submit"]');
        if (btn) {
          btn.setAttribute('aria-busy', 'true');
          btn.setAttribute('disabled', 'disabled');
        }
      });
    });
  }

  /* Countdown widget — seconds-from-now variant.
   * Element: <div data-portal-countdown-seconds="600">Try again in <span>10m 0s</span></div>
   * Cosmetic only; backend remains the source of truth for the lockout. */
  function wireCountdowns() {
    var nodes = document.querySelectorAll('[data-portal-countdown-seconds]');
    Array.prototype.forEach.call(nodes, function (el) {
      var remaining = parseInt(el.getAttribute('data-portal-countdown-seconds'), 10);
      if (isNaN(remaining) || remaining <= 0) return;
      var label = el.querySelector('[data-portal-countdown-label]') || null;
      function fmt(s) {
        var m = Math.floor(s / 60);
        var sec = s % 60;
        if (m > 0) return m + 'm ' + sec + 's';
        return sec + 's';
      }
      function tick() {
        if (remaining <= 0) {
          if (label) {
            label.textContent = 'now';
          }
          return;
        }
        if (label) {
          label.textContent = fmt(remaining);
        }
        remaining -= 1;
        window.setTimeout(tick, 1000);
      }
      tick();
    });
  }

  /* Fetch helper for any caller that wants AJAX with CSRF (no callers in v1
   * since flows are pure 302; exported for v1.1 use). */
  function portalFetch(method, path, body) {
    var headers = { 'Accept': 'application/json' };
    var init = { method: method, credentials: 'same-origin', headers: headers };
    var upper = (method || 'GET').toUpperCase();
    if (upper !== 'GET' && upper !== 'HEAD') {
      headers[CSRF_HEADER] = getCookie(CSRF_COOKIE);
      if (body !== undefined && body !== null) {
        headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(body);
      }
    }
    return fetch(path, init);
  }

  /* Lightweight confirm-before-submit for destructive buttons.
   * Markup: <button data-portal-confirm="Are you sure?">…</button>
   * Falls back to plain submit when JS is disabled (button works either way). */
  function wireConfirmButtons() {
    var btns = document.querySelectorAll('[data-portal-confirm]');
    Array.prototype.forEach.call(btns, function (btn) {
      btn.addEventListener('click', function (event) {
        var msg = btn.getAttribute('data-portal-confirm') || 'Are you sure?';
        if (!window.confirm(msg)) {
          event.preventDefault();
        }
      });
    });
  }

  /* Theme picker — 4 choices: night, day, atreides, harkonnen.
   * The no-FOUC script in base.html sets the initial data-theme before paint;
   * this wires the picker dropdowns and persists the choice. */
  var THEME_KEY = 'ls-portal-theme';
  var THEME_LABELS = { night: 'Night', day: 'Day', atreides: 'House Atreides', harkonnen: 'House Harkonnen' };
  var THEME_COLORS = { night: '#0c0a07', day: '#e9dcc0', atreides: '#090e0b', harkonnen: '#0c0304' };
  var VALID_THEMES = { night: 1, day: 1, atreides: 1, harkonnen: 1 };

  function applyTheme(next) {
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', THEME_COLORS[next] || '#0c0a07');
  }

  function wireThemePicker() {
    var pickers = document.querySelectorAll('[data-portal-theme-picker]');
    if (!pickers.length) return;

    function currentTheme() {
      var t = document.documentElement.getAttribute('data-theme');
      return VALID_THEMES[t] ? t : 'night';
    }

    function syncPickerUI(picker, theme) {
      var btn = picker.querySelector('[data-portal-theme-current]');
      if (!btn) return;
      var label = btn.querySelector('[data-portal-theme-label]');
      var swatch = btn.querySelector('.portal-themepicker__swatch');
      if (label) label.textContent = THEME_LABELS[theme] || theme;
      if (swatch) swatch.className = 'portal-themepicker__swatch portal-themepicker__swatch--' + theme;
      var opts = picker.querySelectorAll('[data-theme-value]');
      Array.prototype.forEach.call(opts, function (opt) {
        opt.setAttribute('aria-selected', opt.getAttribute('data-theme-value') === theme ? 'true' : 'false');
      });
    }

    function closeAll() {
      Array.prototype.forEach.call(pickers, function (picker) {
        var menu = picker.querySelector('.portal-themepicker__menu');
        var btn = picker.querySelector('[data-portal-theme-current]');
        if (menu) menu.hidden = true;
        if (btn) btn.setAttribute('aria-expanded', 'false');
      });
    }

    Array.prototype.forEach.call(pickers, function (picker) {
      var btn = picker.querySelector('[data-portal-theme-current]');
      var menu = picker.querySelector('.portal-themepicker__menu');
      var opts = picker.querySelectorAll('[data-theme-value]');

      syncPickerUI(picker, currentTheme());

      if (btn && menu) {
        btn.addEventListener('click', function (e) {
          e.stopPropagation();
          var opening = menu.hidden;
          closeAll();
          if (opening) {
            menu.hidden = false;
            btn.setAttribute('aria-expanded', 'true');
            var first = menu.querySelector('[data-theme-value]');
            if (first) first.focus();
          }
        });
      }

      Array.prototype.forEach.call(opts, function (opt) {
        opt.setAttribute('tabindex', '-1');
        opt.addEventListener('click', function () {
          var next = opt.getAttribute('data-theme-value');
          if (!next) return;
          applyTheme(next);
          Array.prototype.forEach.call(pickers, function (p) { syncPickerUI(p, next); });
          closeAll();
        });
        opt.addEventListener('keydown', function (e) {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            opt.click();
          } else if (e.key === 'ArrowDown') {
            e.preventDefault();
            var sib = opt.nextElementSibling;
            if (sib) sib.focus();
          } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            var sib = opt.previousElementSibling;
            if (sib) sib.focus();
          } else if (e.key === 'Escape') {
            closeAll();
            if (btn) btn.focus();
          }
        });
      });
    });

    document.addEventListener('click', closeAll);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeAll();
    });
  }

  /* Faction opt-in prompt: one-time "Wear your House colors?" banner for
   * Atreides / Harkonnen-aligned players. Never forced; dismissed indefinitely
   * with a localStorage flag. */
  function wireFactionPrompt() {
    var DISMISSED_KEY = 'ls-house-colors-dismissed';
    var FACTION_KEY   = 'ls-portal-faction';
    var HOUSE_THEMES  = { atreides: 1, harkonnen: 1 };

    // The account page stamps data-portal-faction on an element when the
    // character is aligned to a House. Cache it so the prompt works site-wide.
    var facEl = document.querySelector('[data-portal-faction]');
    if (facEl) {
      var fac = (facEl.getAttribute('data-portal-faction') || '').toLowerCase();
      if (HOUSE_THEMES[fac]) {
        try { localStorage.setItem(FACTION_KEY, fac); } catch (e) {}
      }
    }

    var band = document.getElementById('portal-houseband');
    if (!band) return;

    var storedFaction;
    try {
      if (localStorage.getItem(DISMISSED_KEY)) return;
      storedFaction = localStorage.getItem(FACTION_KEY);
      if (!storedFaction || !HOUSE_THEMES[storedFaction]) return;
      var cur = document.documentElement.getAttribute('data-theme');
      if (HOUSE_THEMES[cur]) return; // already on a house theme
    } catch (e) { return; }

    var crest    = document.getElementById('portal-houseband-crest');
    var nameEl   = document.getElementById('portal-houseband-name');
    var acceptBtn = document.getElementById('portal-houseband-accept');
    var dismissBtn = document.getElementById('portal-houseband-dismiss');
    var displayName = storedFaction === 'atreides' ? 'Atreides' : 'Harkonnen';

    if (crest)  crest.className = 'portal-houseband__crest portal-houseband__crest--' + storedFaction;
    if (nameEl) nameEl.textContent = 'House ' + displayName;

    function dismiss() {
      try { localStorage.setItem(DISMISSED_KEY, '1'); } catch (e) {}
      band.hidden = true;
    }

    if (acceptBtn) {
      acceptBtn.addEventListener('click', function () {
        applyTheme(storedFaction);
        var pickers = document.querySelectorAll('[data-portal-theme-picker]');
        Array.prototype.forEach.call(pickers, function (p) {
          var label = p.querySelector('[data-portal-theme-label]');
          var swatch = p.querySelector('[data-portal-theme-current] .portal-themepicker__swatch');
          var opts = p.querySelectorAll('[data-theme-value]');
          if (label)  label.textContent = THEME_LABELS[storedFaction];
          if (swatch) swatch.className = 'portal-themepicker__swatch portal-themepicker__swatch--' + storedFaction;
          Array.prototype.forEach.call(opts, function (opt) {
            opt.setAttribute('aria-selected', opt.getAttribute('data-theme-value') === storedFaction ? 'true' : 'false');
          });
        });
        dismiss();
      });
    }
    if (dismissBtn) dismissBtn.addEventListener('click', dismiss);

    band.hidden = false;
  }

  // Mobile tab bar "More" overflow sheet (Orders / Landsraad / Guilds).
  function wireMoreSheet() {
    var btn = document.querySelector('[data-portal-more]');
    var sheet = document.getElementById('portal-more-sheet');
    if (!btn || !sheet) return;
    function close() {
      sheet.hidden = true;
      btn.setAttribute('aria-expanded', 'false');
    }
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      var opening = sheet.hidden;
      sheet.hidden = !opening;
      btn.setAttribute('aria-expanded', opening ? 'true' : 'false');
    });
    document.addEventListener('click', function (e) {
      if (!sheet.hidden && !sheet.contains(e.target) && !btn.contains(e.target)) close();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') close();
    });
  }

  // PWA install hint. Chrome/Android exposes beforeinstallprompt; iOS Safari
  // NEVER prompts, so iOS gets a one-time dismissible "Add to Home Screen"
  // instruction instead. Hidden once installed (standalone) or dismissed.
  function wireInstallHint() {
    var KEY = 'ls-portal-install-dismissed';
    try {
      if (window.matchMedia('(display-mode: standalone)').matches ||
          window.navigator.standalone === true) return;
      if (localStorage.getItem(KEY)) return;
    } catch (e) { return; }
    if (!window.matchMedia('(max-width: 900px)').matches) return;

    function show(message, actionLabel, onAction) {
      var bar = document.createElement('div');
      bar.className = 'portal-installhint';
      bar.setAttribute('role', 'status');
      var icon = document.createElement('img');
      icon.src = '/assets/icon.png';
      icon.alt = '';
      icon.className = 'portal-installhint__mark';
      bar.appendChild(icon);
      var text = document.createElement('span');
      text.className = 'portal-installhint__text';
      text.textContent = message;
      bar.appendChild(text);
      function dismiss() {
        try { localStorage.setItem(KEY, '1'); } catch (e) {}
        bar.remove();
      }
      if (actionLabel) {
        var act = document.createElement('button');
        act.type = 'button';
        act.className = 'portal-installhint__action';
        act.textContent = actionLabel;
        act.addEventListener('click', function () { onAction(); dismiss(); });
        bar.appendChild(act);
      }
      var x = document.createElement('button');
      x.type = 'button';
      x.className = 'portal-installhint__close';
      x.setAttribute('aria-label', 'Dismiss');
      x.textContent = '×';
      x.addEventListener('click', dismiss);
      bar.appendChild(x);
      document.body.appendChild(bar);
    }

    var ua = navigator.userAgent || '';
    var isIOS = /iPad|iPhone|iPod/.test(ua) ||
        (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    if (isIOS) {
      show('Install this portal as an app: tap Share, then "Add to Home Screen".');
    } else {
      window.addEventListener('beforeinstallprompt', function (e) {
        e.preventDefault();
        show('Install the Last Sietch portal as an app.', 'Install', function () {
          e.prompt();
        });
      });
    }
  }

  /* Self-rescue "I'm stuck" button on the map page. POSTs /portal/rescue (CSRF
   * via portalFetch), swaps the returned HTML fragment into #map-rescue-result,
   * and disables the button during the 1/hour cooldown the server reports back.
   * The server is authoritative for the cooldown — this is cosmetic UX only.
   * The data-portal-confirm wiring already gates the click with a confirm(). */
  function wireRescue() {
    var btn = document.getElementById('map-rescue-btn');
    var out = document.getElementById('map-rescue-result');
    if (!btn || !out) return;

    function startCooldown(seconds) {
      var remaining = parseInt(seconds, 10);
      if (isNaN(remaining) || remaining <= 0) return;
      btn.setAttribute('disabled', 'disabled');
      var baseLabel = "Rescue on cooldown";
      function tick() {
        if (remaining <= 0) {
          btn.removeAttribute('disabled');
          btn.textContent = "Help! I'm stuck";
          return;
        }
        var m = Math.ceil(remaining / 60);
        btn.textContent = baseLabel + ' (' + m + 'm)';
        remaining -= 1;
        window.setTimeout(tick, 1000);
      }
      tick();
    }

    btn.addEventListener('click', function () {
      if (btn.hasAttribute('disabled')) return;
      if (!window.confirm("Teleport to your nearest base? You can only do this " +
                          "once per hour, and only while logged in to the game.")) {
        return;
      }
      btn.setAttribute('aria-busy', 'true');
      btn.setAttribute('disabled', 'disabled');
      portalFetch('POST', '/portal/rescue', null)
        .then(function (resp) { return resp.text(); })
        .then(function (html) {
          out.innerHTML = html;
          btn.removeAttribute('aria-busy');
          var node = out.querySelector('[data-rescue-cooldown]');
          if (node) {
            startCooldown(node.getAttribute('data-rescue-cooldown'));
          } else {
            btn.removeAttribute('disabled');
          }
        })
        .catch(function () {
          out.textContent = 'Rescue request failed — check your connection and retry.';
          btn.removeAttribute('aria-busy');
          btn.removeAttribute('disabled');
        });
    });
  }

  function init() {
    hydrateCsrfForms();
    wireSubmitBusy();
    wireCountdowns();
    wireConfirmButtons();
    wireThemePicker();
    wireFactionPrompt();
    wireMoreSheet();
    wireInstallHint();
    wireRescue();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.PortalAPI = { fetch: portalFetch };

  // PWA: register the service worker (scope /portal/) so the portal is
  // installable on phones and loads fast on a warm cache. Silent on failure
  // (older browsers / no SW support just get the normal site).
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('/portal/sw.js', { scope: '/portal/' })
        .catch(function () {});
    });
    // Auto-refresh installed/PWA users after a deploy. The new SW does
    // skipWaiting + clients.claim, which fires controllerchange on already-open
    // clients; reload once so they pick up the fresh shell instead of the stale
    // cached one. Guards: only for clients that already had a controller (so a
    // first install/visit does NOT reload), and reload at most once.
    if (navigator.serviceWorker.controller) {
      var _swRefreshing = false;
      navigator.serviceWorker.addEventListener('controllerchange', function () {
        if (_swRefreshing) return;
        _swRefreshing = true;
        window.location.reload();
      });
    }
  }
})();
