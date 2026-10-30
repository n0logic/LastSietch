// Lean-over-the-table camera (contract 4.4; 2.26.0 owner ruling replaces "no
// free 360 orbit"). Orbits the pan target: engine view.azimuth is the heading
// it faces (degrees clockwise from north, full 360), view.tilt its elevation
// (clamped 20..80 by the engine); defaults 0 / 35 reproduce the old fixed lean.
// Dolly maps engine zoom, pan maps engine panX/panY. It exposes the `projector`
// the engine uses for hit-testing (a flat ground-plane raycast at y=0, ignoring
// relief height so it matches the flat 0..1000 world the engine expects); the
// raycast follows the live camera, so it holds at any heading/tilt. THREE is
// injected.
//
// The camera is DRIVEN by engine view (onViewChange -> setView); it attaches no
// pointer listeners of its own, so the engine stays the single owner of
// gestures and the two viewers stay conceptually in step.

const DEFAULT_TILT = 35;                 // deg above ground = 55 deg from top-down
const MIN_TILT = 20, MAX_TILT = 80;     // guard; the engine already clamps
const FOV = 34;
const DIST_FRAC = 1.95;                  // base dolly distance as a multiple of view

export function createCamera(THREE, opts) {
  const view = opts.view || 1000;
  const el = opts.el;                     // canvas/viewport for size + NDC math
  // Terrain height in board units at (nx, ny); null = flat table. Hit-testing and
  // popup anchors follow the relief so markers drawn at heightAt are clickable.
  const heightAt = opts.heightAt || null;
  const H_MAX = opts.maxHeight || view * 0.25;   // ceiling for the ray march
  const half = view / 2;
  const baseDist = view * DIST_FRAC;

  const camera = new THREE.PerspectiveCamera(FOV, 1, 1, view * 8);

  // Preallocated scratch: no per-frame allocation.
  const ray = new THREE.Raycaster();
  const ndc = new THREE.Vector2();
  const proj = new THREE.Vector3();
  // Unit offset from target to camera. Heading h faces (sin h, -cos h) in x/z
  // (north = -z), so the camera sits behind: (-sin h cos e, sin e, cos h cos e).
  const camDir = new THREE.Vector3();

  let target = { x: 0, z: 0 };
  let zoom = 1;
  let azimuth = 0, tilt = DEFAULT_TILT;

  function aim() {
    const h = azimuth * Math.PI / 180;
    const e = tilt * Math.PI / 180;
    camDir.set(-Math.sin(h) * Math.cos(e), Math.sin(e), Math.cos(h) * Math.cos(e));
  }
  aim();

  function size() {
    const w = (el && el.clientWidth) || 1;
    const h = (el && el.clientHeight) || 1;
    return { w, h, disp: Math.min(w, h) || 1 };
  }

  function place() {
    const dist = baseDist / zoom;
    camera.position.set(
      target.x + dist * camDir.x,
      dist * camDir.y,
      target.z + dist * camDir.z
    );
    camera.up.set(0, 1, 0);
    camera.lookAt(target.x, 0, target.z);
    camera.updateMatrixWorld();
  }

  // Engine view -> camera. panX/panY are display pixels in the same square the
  // engine sizes (view.display = min(viewport w,h)); invert to a normalized
  // center fraction, clamp inside the table so the lean never leaves the board.
  function setView(v) {
    zoom = Math.max(1, Math.min(8, v && v.zoom ? v.zoom : 1));
    azimuth = v && Number.isFinite(v.azimuth) ? v.azimuth : 0;
    tilt = v && Number.isFinite(v.tilt) ? Math.max(MIN_TILT, Math.min(MAX_TILT, v.tilt)) : DEFAULT_TILT;
    aim();
    const disp = size().disp;
    const px = (v && v.panX) || 0;
    const py = (v && v.panY) || 0;
    let fx = (0.5 - px / disp) / zoom;
    let fy = (0.5 - py / disp) / zoom;
    fx = Math.max(0, Math.min(1, fx));
    fy = Math.max(0, Math.min(1, fy));
    target.x = fx * view - half;
    target.z = fy * view - half;
    place();
  }

  function resize() {
    const s = size();
    camera.aspect = s.w / s.h;
    camera.updateProjectionMatrix();
    place();
  }

  // Raycast onto the terrain surface (or the y=0 plane when flat). mx,my are
  // viewport-relative pixels (engine gesture space). Returns { nx, ny } in
  // 0..1000 or null when off-board. With relief: march the ray from the H_MAX
  // ceiling down to y=0 and take the first crossing below the surface, then
  // bisect it, so a click on a cliff top lands on the cliff, not the sand behind.
  function above(o, d, t) {
    const nx = o.x + d.x * t + half, ny = o.z + d.z * t + half;
    const inside = nx >= 0 && nx <= view && ny >= 0 && ny <= view;
    return (o.y + d.y * t) - (inside ? heightAt(nx, ny) : 0);
  }
  function unproject(mx, my) {
    const s = size();
    ndc.set((mx / s.w) * 2 - 1, -((my / s.h) * 2 - 1));
    ray.setFromCamera(ndc, camera);
    const o = ray.ray.origin, d = ray.ray.direction;
    if (d.y > -1e-6) return null;                   // looking up or level: no hit
    let t = -o.y / d.y;                             // flat-plane hit
    if (heightAt) {
      const tTop = Math.max(0, (o.y - H_MAX) / -d.y);
      const STEPS = 96;
      let prev = tTop, hit = -1;
      for (let i = 1; i <= STEPS; i++) {
        const ti = tTop + (t - tTop) * i / STEPS;
        if (above(o, d, ti) <= 0) { hit = ti; break; }
        prev = ti;
      }
      if (hit >= 0) {
        let lo = prev, hi = hit;
        for (let k = 0; k < 10; k++) {
          const mid = (lo + hi) / 2;
          if (above(o, d, mid) > 0) lo = mid; else hi = mid;
        }
        t = hi;
      }
    }
    if (t < 0) return null;
    const nx = o.x + d.x * t + half, ny = o.z + d.z * t + half;
    if (nx < 0 || nx > view || ny < 0 || ny > view) return null;
    return { nx, ny };
  }

  // World point on the surface -> viewport pixels for popup anchoring.
  function project(nx, ny) {
    proj.set(nx - half, heightAt ? heightAt(nx, ny) : 0, ny - half).project(camera);
    const s = size();
    return {
      screenX: (proj.x + 1) / 2 * s.w,
      screenY: (1 - proj.y) / 2 * s.h,
    };
  }

  resize();
  place();

  return {
    camera,
    setView,
    resize,
    projector: { project, unproject },
  };
}
