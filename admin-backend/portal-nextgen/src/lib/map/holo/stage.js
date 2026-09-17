// Renderer/scene/loop lifecycle (contract 4.4). One WebGLRenderer, one Scene,
// one rAF loop, pixel-ratio capped at 2, pause-on-hidden, WebGL context-loss
// handling, and a full idempotent dispose. THREE is injected. No per-frame
// allocation here: the loop only calls the injected frame callback and renders.

export function createStage(THREE, opts) {
  const canvas = opts.canvas;
  const viewport = opts.viewport || canvas;
  const onContextLost = opts.onContextLost || function () {};

  const renderer = new THREE.WebGLRenderer({
    canvas: canvas,
    antialias: true,
    alpha: true,                 // carved-sand backdrop / CSS gradient shows through
    powerPreference: 'high-performance',
  });
  renderer.setPixelRatio(Math.min(
    (typeof devicePixelRatio !== 'undefined' && devicePixelRatio) || 1, 2));

  const scene = new THREE.Scene();

  let cam = null;      // camera wrapper { camera, resize }
  let frameCb = null;
  let raf = null;
  let paused = false;
  let destroyed = false;
  let startT = 0;
  let firstDone = null;

  function sizeNow() {
    const w = viewport.clientWidth || canvas.clientWidth || 1;
    const h = viewport.clientHeight || canvas.clientHeight || 1;
    renderer.setSize(w, h, false);
  }

  function renderFrame(nowMs) {
    if (destroyed) return;
    if (startT === 0) startT = nowMs;
    const sec = (nowMs - startT) / 1000;
    if (frameCb) frameCb(sec);
    if (cam) renderer.render(scene, cam.camera);
    if (firstDone) { firstDone(); firstDone = null; }
  }

  function loop(nowMs) {
    raf = null;
    if (destroyed || paused) return;
    renderFrame(nowMs);
    raf = requestAnimationFrame(loop);
  }

  function schedule() {
    if (!destroyed && !paused && raf == null) raf = requestAnimationFrame(loop);
  }

  function onVisibility() {
    paused = !!document.hidden;
    if (!paused) { startT = 0; schedule(); }   // reset clock to avoid a jump
    else if (raf != null) { cancelAnimationFrame(raf); raf = null; }
  }

  function onLost(e) {
    // Do NOT preventDefault: we do not attempt restore (contract 3). Stop the
    // loop and let the arbiter fall back to the carved board.
    if (raf != null) { cancelAnimationFrame(raf); raf = null; }
    paused = true;
    onContextLost();
  }

  const ro = (typeof ResizeObserver !== 'undefined')
    ? new ResizeObserver(function () { sizeNow(); if (cam && cam.resize) cam.resize(); })
    : null;
  if (ro) ro.observe(viewport);
  document.addEventListener('visibilitychange', onVisibility);
  canvas.addEventListener('webglcontextlost', onLost, false);

  sizeNow();

  return {
    scene,
    renderer,
    canvas,

    setCamera(wrap) { cam = wrap; },

    // frameCb(sec) runs each frame before render; ready resolves after frame 1.
    start(cb) {
      frameCb = cb;
      sizeNow();
      const ready = new Promise(function (res) { firstDone = res; });
      schedule();
      return ready;
    },

    resize() { sizeNow(); if (cam && cam.resize) cam.resize(); },

    dispose() {
      if (destroyed) return;
      destroyed = true;
      if (raf != null) { cancelAnimationFrame(raf); raf = null; }
      document.removeEventListener('visibilitychange', onVisibility);
      canvas.removeEventListener('webglcontextlost', onLost, false);
      if (ro) ro.disconnect();
      renderer.dispose();
      if (renderer.forceContextLoss) renderer.forceContextLoss();
    },
  };
}
