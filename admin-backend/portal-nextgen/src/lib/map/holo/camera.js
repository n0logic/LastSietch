// Constrained lean-over-the-table camera (contract 4.4, owner Q1: no free 360
// orbit). Fixed pitch, azimuth locked, dolly maps engine zoom, pan maps engine
// panX/panY. This keeps the ground-plane <-> screen mapping stable and cheap and
// is reduced-motion friendly. It exposes the `projector` the engine uses for
// hit-testing (a flat ground-plane raycast at y=0, ignoring relief height so it
// matches the flat 0..1000 world the engine expects). THREE is injected.
//
// The camera is DRIVEN by engine view (onViewChange -> setView); it attaches no
// pointer listeners of its own, so the engine stays the single owner of
// gestures and the two viewers stay conceptually in step.

const PITCH_ELEV = 35 * Math.PI / 180;   // 35 deg above ground = 55 deg from top-down
const FOV = 34;
const DIST_FRAC = 1.95;                  // base dolly distance as a multiple of view

export function createCamera(THREE, opts) {
  const view = opts.view || 1000;
  const el = opts.el;                     // canvas/viewport for size + NDC math
  const half = view / 2;
  const baseDist = view * DIST_FRAC;

  const camera = new THREE.PerspectiveCamera(FOV, 1, 1, view * 8);

  // Preallocated scratch: no per-frame allocation.
  const ray = new THREE.Raycaster();
  const ndc = new THREE.Vector2();
  const proj = new THREE.Vector3();
  const camDir = new THREE.Vector3(0, Math.sin(PITCH_ELEV), Math.cos(PITCH_ELEV));

  let target = { x: 0, z: 0 };
  let zoom = 1;

  function size() {
    const w = (el && el.clientWidth) || 1;
    const h = (el && el.clientHeight) || 1;
    return { w, h, disp: Math.min(w, h) || 1 };
  }

  function place() {
    const dist = baseDist / zoom;
    camera.position.set(
      target.x,
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

  // Ground-plane raycast at y=0. mx,my are viewport-relative pixels (engine
  // gesture space). Returns { nx, ny } in 0..1000 or null when off-board.
  function unproject(mx, my) {
    const s = size();
    ndc.set((mx / s.w) * 2 - 1, -((my / s.h) * 2 - 1));
    ray.setFromCamera(ndc, camera);
    const o = ray.ray.origin, d = ray.ray.direction;
    if (Math.abs(d.y) < 1e-6) return null;
    const t = -o.y / d.y;
    if (t < 0) return null;
    const wx = o.x + d.x * t;
    const wz = o.z + d.z * t;
    const nx = wx + half, ny = wz + half;
    if (nx < 0 || nx > view || ny < 0 || ny > view) return null;
    return { nx, ny };
  }

  // World point (y=0) -> viewport pixels for popup anchoring.
  function project(nx, ny) {
    proj.set(nx - half, 0, ny - half).project(camera);
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
