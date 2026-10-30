/* reading-font.js: the Easier reading font switch. The choice lives in this
 * browser only (ls-reading-font). Each admin page's head applies it before first
 * paint with a one-line inline script, so keep the key and the attribute in step
 * with that. Any button with data-reading-font-toggle becomes a switch.
 * Any button with data-reading-size-toggle cycles the body text size (Small,
 * Medium, Large; ls-reading-size) and only shows while the font is on. */
(function () {
  var root = document.documentElement;
  var ATTR = 'data-reading-font';
  var SIZE_ATTR = 'data-reading-size';
  var SIZES = ['s', 'm', 'l'];
  var SIZE_LABELS = { s: 'Small', m: 'Medium', l: 'Large' };

  function isOn() { return root.getAttribute(ATTR) === 'dyslexic'; }

  // Medium is the absent attribute, matching the inline head script.
  function size() {
    var v = root.getAttribute(SIZE_ATTR);
    return v === 's' || v === 'l' ? v : 'm';
  }

  function render() {
    var on = isOn();
    document.querySelectorAll('[data-reading-font-toggle]').forEach(function (btn) {
      btn.setAttribute('aria-checked', on ? 'true' : 'false');
      var state = btn.querySelector('.reading-toggle__state');
      if (state) state.textContent = on ? 'On' : 'Off';
    });
    var label = SIZE_LABELS[size()];
    document.querySelectorAll('[data-reading-size-toggle]').forEach(function (btn) {
      btn.hidden = !on;
      var state = btn.querySelector('.reading-toggle__state');
      if (state) state.textContent = label;
    });
  }

  document.addEventListener('click', function (e) {
    var sizeBtn = e.target.closest && e.target.closest('[data-reading-size-toggle]');
    if (sizeBtn) {
      var next = SIZES[(SIZES.indexOf(size()) + 1) % SIZES.length];
      if (next === 'm') root.removeAttribute(SIZE_ATTR);
      else root.setAttribute(SIZE_ATTR, next);
      try { localStorage.setItem('ls-reading-size', next); } catch (err) {}
      render();
      return;
    }
    var btn = e.target.closest && e.target.closest('[data-reading-font-toggle]');
    if (!btn) return;
    var on = !isOn();
    if (on) root.setAttribute(ATTR, 'dyslexic');
    else root.removeAttribute(ATTR);
    try {
      if (on) localStorage.setItem('ls-reading-font', '1');
      else localStorage.removeItem('ls-reading-font');
    } catch (err) {}
    render();
  });

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', render);
  else render();
})();
