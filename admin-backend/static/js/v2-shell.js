/* V2 admin shell helper.
 * (a) showToast(type, message, duration) -> injects a dismissible toast into .toast-container.
 * (b) Document-level htmx:responseError handler -> redirect to /admin/login on 401.
 * (c) v2Poll(fn, ms, opts) -> the one poll scheduler every admin page uses.
 * (d) v2ConfirmTyped(opts) -> the one typed-token confirmation (markup in base.html).
 */
(function () {
  'use strict';

  var DEFAULT_DURATION = { success: 5000, info: 5000, warning: 10000, error: 10000 };

  function ensureContainer() {
    var c = document.querySelector('.toast-container');
    if (!c) {
      c = document.createElement('div');
      c.className = 'toast-container';
      c.setAttribute('aria-live', 'polite');
      document.body.appendChild(c);
    }
    return c;
  }

  function showToast(type, message, duration) {
    var container = ensureContainer();
    var kind = type || 'info';
    var ms = typeof duration === 'number' ? duration : (DEFAULT_DURATION[kind] || 5000);

    var toast = document.createElement('div');
    toast.className = 'toast ' + kind;
    toast.setAttribute('role', kind === 'error' ? 'alert' : 'status');

    var body = document.createElement('div');
    body.className = 'toast__body';
    body.textContent = message;
    toast.appendChild(body);

    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'toast__close';
    close.setAttribute('aria-label', 'Dismiss notification');
    close.innerHTML = '&times;';
    toast.appendChild(close);

    container.appendChild(toast);

    var timer = null;
    function dismiss() {
      if (timer) { clearTimeout(timer); timer = null; }
      if (toast.parentNode) toast.parentNode.removeChild(toast);
    }
    close.addEventListener('click', dismiss);
    toast.addEventListener('mouseenter', function () { if (timer) { clearTimeout(timer); timer = null; } });
    toast.addEventListener('mouseleave', function () { if (!timer) timer = setTimeout(dismiss, ms); });

    if (ms > 0) timer = setTimeout(dismiss, ms);
    return toast;
  }

  document.addEventListener('htmx:responseError', function (event) {
    if (event && event.detail && event.detail.xhr && event.detail.xhr.status === 401) {
      window.location = '/admin/login';
    }
  });

  /* --- v2Poll ------------------------------------------------------------
   * Every admin poll went through a bare setInterval, so a pocketed phone on
   * Systems kept nine cards refreshing every 15 seconds, one of them over an
   * SSH round trip, and a source that was down was re-asked at full rate
   * forever. This is the one scheduler:
   *
   *   - fn runs immediately (opts.immediate === false skips that first run for
   *     a caller that has just loaded by itself),
   *   - it runs again only while the tab is visible; a hidden tab schedules
   *     nothing at all,
   *   - becoming visible fires one run straight away, so the operator never
   *     reads a stale board,
   *   - a failure (a rejected promise, a thrown error, or fn returning false)
   *     doubles the gap up to 4x, and any success puts it straight back.
   *
   * setTimeout chaining rather than setInterval: an interval cannot back off,
   * and it stacks runs when one is slower than the gap.
   */
  function v2Poll(fn, ms, opts) {
    var o = opts || {};
    var base = ms;
    var maxDelay = base * (o.maxBackoff || 4);
    var delay = base;
    var timer = null;
    var stopped = false;
    var running = false;

    function clear() {
      if (timer) { clearTimeout(timer); timer = null; }
    }

    function schedule() {
      clear();
      if (stopped || document.hidden) return;
      timer = setTimeout(tick, delay);
    }

    function settle(ok) {
      running = false;
      delay = ok ? base : Math.min(delay * 2, maxDelay);
      schedule();
    }

    function tick() {
      timer = null;
      if (stopped || document.hidden || running) return;
      running = true;
      var out;
      try {
        out = fn();
      } catch (e) {
        settle(false);
        return;
      }
      if (out && typeof out.then === 'function') {
        out.then(function (v) { settle(v !== false); }, function () { settle(false); });
      } else {
        settle(out !== false);
      }
    }

    function onVisibility() {
      if (stopped) return;
      if (document.hidden) { clear(); return; }
      tick();
    }

    document.addEventListener('visibilitychange', onVisibility);
    if (o.immediate === false) { schedule(); } else { tick(); }

    return {
      stop: function () {
        stopped = true;
        clear();
        document.removeEventListener('visibilitychange', onVisibility);
      },
      /* The current gap in ms. Exposed so the suite can watch the backoff. */
      delay: function () { return delay; }
    };
  }

  /* --- v2ConfirmTyped ----------------------------------------------------
   * The panel had four confirmation idioms, one of them window.prompt on a
   * live server-wide broadcast. This is the destructive one: a styled modal
   * that resolves true ONLY when the operator types the token exactly.
   *
   * Resolves false when the markup is absent (a page that does not extend
   * base.html): a missing dialog must refuse, never approve.
   */
  function v2ConfirmTyped(opts) {
    var o = opts || {};
    var token = String(o.token || 'CONFIRM');

    return new Promise(function (resolve) {
      var overlay = document.getElementById('typed-confirm-overlay');
      var input = document.getElementById('typed-confirm-input');
      var yes = document.getElementById('typed-confirm-yes');
      var no = document.getElementById('typed-confirm-no');
      if (!overlay || !input || !yes || !no) { resolve(false); return; }

      var dialog = overlay.querySelector('.confirm-dialog');
      var returnFocus = document.activeElement;
      var done = false;

      document.getElementById('typed-confirm-title').textContent = o.title || 'Confirm';
      document.getElementById('typed-confirm-body').textContent = o.body || '';
      document.getElementById('typed-confirm-token').textContent = token;
      if (dialog) dialog.classList.toggle('confirm-dialog--danger', !!o.danger);
      yes.textContent = o.confirmLabel || 'Confirm';
      input.value = '';
      yes.disabled = true;

      function close(result) {
        if (done) return;
        done = true;
        overlay.classList.remove('active');
        input.removeEventListener('input', onInput);
        input.removeEventListener('keydown', onKey);
        yes.removeEventListener('click', onYes);
        no.removeEventListener('click', onNo);
        document.removeEventListener('keydown', onEscape, true);
        if (returnFocus && returnFocus.focus) returnFocus.focus();
        resolve(result);
      }

      function matches() { return input.value === token; }
      function onInput() { yes.disabled = !matches(); }
      function onYes() { if (matches()) close(true); }
      function onNo() { close(false); }
      function onKey(e) {
        if (e.key === 'Enter') { e.preventDefault(); onYes(); }
      }
      function onEscape(e) {
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(false); }
      }

      input.addEventListener('input', onInput);
      input.addEventListener('keydown', onKey);
      yes.addEventListener('click', onYes);
      no.addEventListener('click', onNo);
      /* Capture phase: base.html has a bubbling Escape handler that closes the
         other two overlays, and this one must resolve rather than just hide. */
      document.addEventListener('keydown', onEscape, true);

      overlay.classList.add('active');
      input.focus();
    });
  }

  window.showToast = showToast;
  window.v2Poll = v2Poll;
  window.v2ConfirmTyped = v2ConfirmTyped;
})();
