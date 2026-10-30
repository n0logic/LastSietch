// createHoloViewer: the StageViewer implementation (contract 4.2) and the ONLY
// module that imports 'three'. Because nothing else statically imports three,
// Rollup isolates three + all holo/*.js into one lazy chunk that dev-1 reaches
// via `await import('$lib/map/holo/index.js')`. This file constructs
// stage/terrain/material/overlays3d/camera, wires the render loop, and exposes
// the `projector` the engine uses for hit-testing.

import * as THREE from 'three';
import { base } from '$app/paths';
import { createReckoner, prefersReducedMotion } from '../reckon.js';
import { createStage } from './stage.js';
import { createHoloMaterial } from './material.js';
import { createTerrain, terrainSourceFor, surfaceFor } from './terrain.js';
import { createOverlays3d } from './overlays3d.js';
import { createCamera } from './camera.js';
import { createLabels } from './labels.js';
import { forcedLayout, forcedRelief } from './debug.js';
import { lightFor, holoLight, lerpWeights, hourOverride, readMode, RELIGHT_MS } from './light.js';

// Resolve the amber/Ibad tokens through the live CSS cascade (house + theme).
// A hidden probe lets the browser fully substitute var(--accent) etc. to an
// rgb() string that THREE.Color parses; reading the raw custom property would
// return an unresolved "var(...)".
function makeTokenReader() {
  const probe = document.createElement('span');
  probe.style.cssText = 'position:absolute;left:-9999px;width:0;height:0;visibility:hidden';
  document.body.appendChild(probe);
  function one(name, fallback) {
    probe.style.color = 'var(' + name + ', ' + fallback + ')';
    return new THREE.Color().setStyle(getComputedStyle(probe).color);
  }
  return {
    read() {
      return {
        accent: one('--accent', '#d4891c'),
        accentBright: one('--accent-bright', '#f5a23a'),
        ibad: one('--ls-ibad', '#3fb6c9'),
        bgDeep: one('--bg-deep', '#040302'),
      };
    },
    dispose() { if (probe.parentNode) probe.parentNode.removeChild(probe); },
  };
}

// createHoloViewer(hostEls, opts) -> StageViewer.
// hostEls: { viewport, canvas } (dev-1 supplies the holo canvas; falls back to
//   creating one under viewport so the fixture harness works standalone).
// opts: { quality?, reducedMotion?, onContextLost? }. quality is the
//   detectQuality() object; its reducedMotion flag is honored if present.
export function createHoloViewer(hostEls, opts) {
  opts = opts || {};
  const viewport = hostEls.viewport;
  let ownCanvas = false;
  let canvas = hostEls.canvas;
  if (!canvas) {
    canvas = document.createElement('canvas');
    canvas.setAttribute('aria-hidden', 'true');
    canvas.style.cssText = 'position:absolute;inset:0;width:100%;height:100%';
    viewport.appendChild(canvas);
    ownCanvas = true;
  }
  canvas.setAttribute('aria-hidden', 'true');

  const reducedMotion = opts.reducedMotion != null ? !!opts.reducedMotion
    : (opts.quality && opts.quality.reducedMotion != null)
      ? !!opts.quality.reducedMotion : prefersReducedMotion();

  const tokenReader = makeTokenReader();
  let tokens = tokenReader.read();
  // Fixed normalized world extent. Backend VIEW is globally 1000 today; the
  // relief mesh, camera framing and overlay projection are all built to it
  // before the first frame, so a map that ever ships a different view would
  // render mis-scaled. applyScene guards + logs that case loudly (once).
  const view = 1000;
  let viewChecked = false;

  const stage = createStage(THREE, {
    canvas, viewport, onContextLost: opts.onContextLost || function () {},
  });

  const tier = opts.quality && opts.quality.tier;
  const coarse = !!(opts.quality && opts.quality.coarse);
  const material = createHoloMaterial(THREE, { view, tokens, reducedMotion });
  // layoutBase: where the M4 baked heightfields live under the SvelteKit static
  // tree. terrain stays on the seeded relief until terrainSourceFor() names a
  // bake for the scene's map (Hagga static, DD by layout id).
  const terrain = createTerrain(THREE, {
    view, material: material.material, reducedMotion,
    layoutBase: base + '/terrain/',
    reliefBoost: forcedRelief(),   // null = terrain's default exaggeration
  });
  // maxHeight: true-scale hub dioramas reach ~275 board units (Arrakeen 220 m over 798 m)
  const camera = createCamera(THREE, { view, el: canvas, heightAt: (nx, ny) => terrain.heightAt(nx, ny), maxHeight: view * 0.6 });
  const overlays = createOverlays3d(THREE, {
    scene: stage.scene, view, heightAt: terrain.heightAt,
    tokens, reducedMotion, reckon: { createReckoner }, assetBase: base,
    tier: tier, pixelRatio: stage.renderer.getPixelRatio(),
  });
  const labels = createLabels(THREE, {
    view, grid: { cols: 9, rows: 9 }, heightAt: terrain.heightAt, tokens,
  });

  stage.scene.add(terrain.mesh);
  stage.scene.add(labels.mesh);
  stage.setCamera(camera);

  // Drape texture size: the 8192 backdrop only on a high-tier pointer device
  // whose GPU takes it; touch / weaker devices get the 2048 variant.
  const caps = stage.renderer.capabilities;
  const smallDrape = coarse || tier !== 'high' || caps.maxTextureSize < 8192;
  const anisotropy = Math.min(8, caps.getMaxAnisotropy());

  let destroyed = false;
  let lastScene = null;

  // ---- time of day -----------------------------------------------------------
  // Same viewer-clock presets and 30-minute blends as the Sand Table (light.js ->
  // world/tod.js); the toggle choice and ?hour=N come in the same way. A toggle
  // change eases over ~0.7 s, or snaps under reduced motion.
  const hourPin = hourOverride(typeof location !== 'undefined' ? location.search : '');
  let todMode = readMode();
  let curW = null, trans = null, todInfo = null;
  function applyLook(w) {
    const l = holoLight(w);
    material.setLight(l);
    overlays.setLook(l);
  }
  function relight(ease) {
    if (destroyed) return;
    const f = lightFor(todMode, hourPin);
    todInfo = { mode: todMode, hour: f.hour, preset: f.preset };
    canvas.dataset.tod = f.preset;
    if (!curW || !ease || reducedMotion) { curW = f.weights; trans = null; applyLook(curW); }
    else trans = { from: curW, to: f.weights, t0: performance.now() };
  }
  relight(false);
  const relightTimer = setInterval(function () { relight(false); }, RELIGHT_MS);

  let sizeW = 0, sizeH = 0;
  const ready = stage.start(function (sec) {
    material.setTime(sec);
    overlays.setTime(sec);
    if (trans) {
      const t = Math.min(1, (performance.now() - trans.t0) / 700);
      const e = t * t * (3 - 2 * t);
      curW = lerpWeights(trans.from, trans.to, e);
      applyLook(curW);
      if (t >= 1) trans = null;
    }
    const w = viewport.clientWidth, h = viewport.clientHeight;
    if (w !== sizeW || h !== sizeH) {
      sizeW = w; sizeH = h;
      const bs = stage.renderer.getDrawingBufferSize(new THREE.Vector2());
      overlays.setSize(w, h, bs.x, bs.y);
    }
  });
  overlays.setProjector(camera.projector.project);
  // The engine hit-tests through this: relief-aware ground raycast plus the cluster seam
  const projector = {
    project: camera.projector.project,
    unproject: camera.projector.unproject,
    clusterAt: overlays.clusterAt,
    clusterMembers: overlays.clusterMembers,
  };

  // Review preview only (constant-folded away in production builds): the QA
  // harness reads the spice layer and the projector off the canvas element.
  if (import.meta.env.VITE_REVIEW_PREVIEW === 'true') {
    const qaRay = new THREE.Raycaster(), qaV = new THREE.Vector3(), qaDown = new THREE.Vector3(0, -1, 0);
    canvas.__holoQa = {
      spice: overlays.spiceInfo, anchors: overlays.anchorsInfo, storm: overlays.stormInfo, worms: overlays.wormInfo,
      project: camera.projector.project, heightAt: terrain.heightAt,
      // the drawn relief under (nx, ny): a vertical ray against the terrain mesh
      surfaceY(nx, ny) {
        qaRay.set(qaV.set(nx - view / 2, 5000, ny - view / 2), qaDown);
        const hit = qaRay.intersectObject(terrain.mesh, false)[0];
        return hit ? hit.point.y : null;
      },
      // a board-space world point (x = nx, z = ny) to viewport px
      toScreen(x, y, z) {
        qaV.set(x - view / 2, y, z - view / 2).project(camera.camera);
        return { x: (qaV.x + 1) / 2 * canvas.clientWidth, y: (1 - qaV.y) / 2 * canvas.clientHeight };
      },
    };
  }

  function applyScene(scene) {
    if (destroyed || !scene) return;
    if (!viewChecked) {
      viewChecked = true;
      if (scene.view != null && scene.view !== view) {
        console.warn('[holo] scene.view ' + scene.view + ' != assumed ' + view +
          '; relief/camera/overlays are built to ' + view + ' and will mis-scale');
      }
    }
    lastScene = scene;
    // No survey grid (Hagga): no etched 9x9 lines and no A1-I9 labels.
    material.setGrid(!!scene.grid);
    labels.setVisible(!!scene.grid);
    // M4 / 2.26.0: swap in the real relief for maps that have a bake. Hagga
    // loads its static basin bake; DD loads the backend-identified Coriolis
    // layout (absent/fallback = no-op, seeded relief stays); other maps stay
    // seeded. When the raster loads it changes heightAt, so re-place the
    // overlay markers to sit flush, then lay on the bake's surface textures.
    // Debug override: ?holoLayout=N (DD only) forces a specific layout (0-11)
    // so we can eyeball each baked terrain against the in-game map.
    var src = terrainSourceFor(scene, {
      base: base, forcedLayout: forcedLayout(scene.key), forcedRelief: forcedRelief(), tier: tier,
    });
    if (src) {
      terrain.applyLayout(src.layout, src).then(function (changed) {
        if (changed && !destroyed && lastScene) {
          overlays.reliefChanged();
          overlays.applyScene(lastScene);
          labels.reposition();   // relief height changed: re-lift the labels
          var act = terrain.active();
          var surf = surfaceFor(src, act, lastScene.backdrop, smallDrape);
          material.setSurface({
            normalUrl: surf.normalUrl, drapeUrl: surf.drapeUrl,
            relief: act ? act.relief : 1, anisotropy: anisotropy, drapeGain: surf.drapeGain,
          });
        }
      });
    }
    overlays.applyScene(scene);
  }

  function setView(v) {
    if (destroyed) return;
    camera.setView(v || {});
    overlays.setView(v || {});   // feed engine zoom to the marker size boost
  }

  function setTheme() {
    if (destroyed) return;
    tokens = tokenReader.read();
    material.setTokens(tokens);
    overlays.setTokens(tokens);
    labels.setTokens(tokens);
  }

  function resize() {
    if (destroyed) return;
    stage.resize();
  }

  // setTimeOfDay('auto' | 'dawn' | 'day' | 'dusk' | 'night'): the toggle choice
  function setTimeOfDay(mode) {
    if (destroyed) return;
    todMode = mode;
    relight(true);
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    clearInterval(relightTimer);
    stage.dispose();
    overlays.dispose();
    labels.dispose();
    terrain.dispose();
    material.dispose();
    tokenReader.dispose();
    if (ownCanvas && canvas.parentNode) canvas.parentNode.removeChild(canvas);
  }

  return {
    ready,
    applyScene,
    setView,
    setTheme,
    setTimeOfDay,
    timeOfDay() { return todInfo; },
    clusterInfo: overlays.clusterInfo,
    projector: projector,
    resize,
    destroy,
  };
}

export default createHoloViewer;
