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
import { createTerrain } from './terrain.js';
import { createOverlays3d } from './overlays3d.js';
import { createCamera } from './camera.js';
import { createLabels } from './labels.js';
import { forcedLayout, forcedRelief } from './debug.js';

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

  const material = createHoloMaterial(THREE, { view, tokens, reducedMotion });
  // layoutBase: where the M4 baked heightfields live under the SvelteKit static
  // tree. terrain stays on the seeded relief until a scene carries layout.id.
  const terrain = createTerrain(THREE, {
    view, material: material.material, reducedMotion,
    layoutBase: base + '/terrain/',
    reliefBoost: forcedRelief(),   // null = terrain's default exaggeration
  });
  const camera = createCamera(THREE, { view, el: canvas });
  const overlays = createOverlays3d(THREE, {
    scene: stage.scene, view, heightAt: terrain.heightAt,
    tokens, reducedMotion, reckon: { createReckoner }, assetBase: base,
  });
  const labels = createLabels(THREE, {
    view, grid: { cols: 9, rows: 9 }, heightAt: terrain.heightAt, tokens,
  });

  stage.scene.add(terrain.mesh);
  stage.scene.add(labels.mesh);
  stage.setCamera(camera);

  let destroyed = false;
  let lastScene = null;
  const ready = stage.start(function (sec) {
    material.setTime(sec);
    overlays.setTime(sec);
  });

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
    // M4: swap in the real Coriolis-layout relief when the backend identifies it.
    // Absent/fallback layout is a no-op (seeded relief stays). When the raster
    // loads it changes heightAt, so re-place the overlay markers to sit flush.
    // Debug override: ?holoLayout=N forces a specific layout (0-11) so we can
    // eyeball each baked terrain against the in-game map before the backend
    // matcher is trusted. Takes precedence over scene.layout when present.
    var forced = forcedLayout();
    var layoutToApply = (forced != null) ? { id: forced, source: 'debug' } : scene.layout;
    if (layoutToApply) {
      terrain.applyLayout(layoutToApply).then(function (changed) {
        if (changed && !destroyed && lastScene) {
          overlays.applyScene(lastScene);
          labels.reposition();   // relief height changed: re-lift the labels
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

  function destroy() {
    if (destroyed) return;
    destroyed = true;
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
    projector: camera.projector,
    resize,
    destroy,
  };
}

export default createHoloViewer;
