/* portal-cinematic.js — Phase 1 cinematic layer
 * (a) Sand drift canvas: lightweight particle system on primary hero banners.
 * (b) Hero parallax: scroll-aware background-position shift.
 *
 * CSP-safe: no eval, no inline scripts, ships as a static file.
 * prefers-reduced-motion: kills every effect immediately.
 * Tab visibility: animation loop pauses when tab is hidden.
 * Mobile cap: half the particle count on narrow viewports.
 */
(function () {
  'use strict';

  // Honour user preference first — if reduced motion, do nothing.
  var mql = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)');
  if (mql && mql.matches) return;

  var isMobile = window.matchMedia
    ? window.matchMedia('(max-width: 767px)').matches
    : false;

  // ── Particle configuration ────────────────────────────────────────────────
  var N = isMobile ? 28 : 56;          // grain count
  var R = 245, G = 162, B = 58;        // amber channel values (--accent-bright)

  function rand(lo, hi) { return lo + Math.random() * (hi - lo); }

  function mkGrain(W, H, seedAcross) {
    return {
      x:      seedAcross ? rand(0, W) : rand(-4, 0),
      y:      rand(0, H),
      vx:     rand(0.16, 0.38),   // drift rightward (desert breeze)
      vy:     rand(-0.06, 0.10),  // slight vertical waver
      r:      rand(0.35, 1.25),   // radius in canvas-pixels
      a:      rand(0.03, 0.09),   // base opacity
      glint:  Math.random() < 0.14  // 14 % of grains catch the light
    };
  }

  // ── Sand drift canvas ─────────────────────────────────────────────────────
  function initCanvas(hero) {
    var canvas = document.createElement('canvas');
    // Absolutely covers the hero, below the hero__inner text layer.
    canvas.style.cssText =
      'position:absolute;inset:0;width:100%;height:100%;' +
      'pointer-events:none;z-index:0;';
    canvas.setAttribute('aria-hidden', 'true');
    hero.insertBefore(canvas, hero.firstChild);   // behind hero__inner

    var ctx = canvas.getContext('2d');
    var W = 0, H = 0, grains = [];
    var raf = 0, frame = 0, paused = false;

    function measure() {
      W = canvas.width  = hero.offsetWidth;
      H = canvas.height = hero.offsetHeight;
    }

    function seed(across) {
      grains = [];
      for (var i = 0; i < N; i++) grains.push(mkGrain(W, H, across));
    }

    function tick() {
      if (paused) return;
      frame++;
      ctx.clearRect(0, 0, W, H);

      for (var i = 0; i < grains.length; i++) {
        var g = grains[i];
        g.x += g.vx;
        g.y += g.vy;
        // Wrap: exit right → re-enter left at new random Y.
        if (g.x > W + 2) { g.x = -2; g.y = rand(0, H); }
        if (g.y >  H + 2) g.y = -2;
        if (g.y < -2)     g.y = H + 2;

        var alpha = g.a;
        if (g.glint) {
          // Sine-wave flicker — phase offset per grain to avoid lockstep.
          alpha += 0.07 * Math.abs(Math.sin((frame * 0.035) + i * 0.73));
        }

        ctx.beginPath();
        ctx.arc(g.x, g.y, g.r, 0, 6.2832);
        ctx.fillStyle = 'rgba(' + R + ',' + G + ',' + B + ',' +
                        Math.min(alpha, 0.18).toFixed(3) + ')';
        ctx.fill();
      }
      raf = requestAnimationFrame(tick);
    }

    function pause()  { paused = true;  cancelAnimationFrame(raf); }
    function resume() { if (paused) { paused = false; tick(); } }

    document.addEventListener('visibilitychange', function () {
      document.hidden ? pause() : resume();
    });

    // Re-measure + re-seed when the hero resizes (orientation change, etc.)
    if (window.ResizeObserver) {
      new window.ResizeObserver(function () {
        measure();
        seed(true);  // distribute across full width after resize
      }).observe(hero);
    }

    measure();
    seed(true);
    tick();
  }

  // ── Hero parallax ─────────────────────────────────────────────────────────
  // Shifts background-position-y gently as the hero scrolls through the
  // viewport. The base Y value (42%) is from portal.css background-position.
  var BASE_Y  = 42;    // % — must match background-position: center 42%
  var FACTOR  =  0.10; // background moves at 10 % of scroll rate (subtle)
  var CLAMP_LO = 22;
  var CLAMP_HI = 62;

  function initParallax(hero) {
    var lastY = null;

    function update() {
      var rect = hero.getBoundingClientRect();
      // How far past the viewport top has the hero scrolled?
      var past = -rect.top;
      var y = BASE_Y - past * FACTOR;
      y = Math.max(CLAMP_LO, Math.min(CLAMP_HI, y));
      // Round to 0.5 % steps — avoids sub-pixel updates on every pixel scrolled.
      var snapped = Math.round(y * 2) / 2;
      if (snapped !== lastY) {
        hero.style.backgroundPositionY = snapped + '%';
        lastY = snapped;
      }
    }

    update();
    window.addEventListener('scroll', function () {
      requestAnimationFrame(update);
    }, { passive: true });
  }

  // ── Boot ──────────────────────────────────────────────────────────────────
  function boot() {
    // Primary heroes: full-height banners. Skip slim variants (map renderer,
    // spice page) — parallax on a 96 px strip looks wrong.
    var heroes = document.querySelectorAll(
      '.portal-hero:not(.portal-hero--slim)'
    );
    for (var i = 0; i < heroes.length; i++) {
      initCanvas(heroes[i]);
      initParallax(heroes[i]);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
}());
