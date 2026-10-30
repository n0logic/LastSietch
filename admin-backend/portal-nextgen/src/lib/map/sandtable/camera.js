// Sand Table camera. Same engine view (transform.js: zoom, pan, azimuth, tilt) and
// the same lean-over-the-table maths as holo/camera.js, but for a round table of
// radius R centred at (cx, cz) instead of the 1000-unit square board. Driven by
// the view; attaches no listeners (the index owns gestures). THREE is injected.

const FOV = 32;

export function createTableCamera(THREE, opts) {
  const { cx, cz, R, el } = opts;
  const camera = new THREE.PerspectiveCamera(FOV, 1, 0.5, R * 40);
  const camDir = new THREE.Vector3();
  const target = new THREE.Vector3(cx, 0, cz);
  const ray = new THREE.Raycaster();
  const ndc = new THREE.Vector2();
  const proj = new THREE.Vector3();
  let zoom = 1, azimuth = 0, tilt = 35, baseDist = R * 2.2, dolly = 1;

  function size() {
    const w = (el && el.clientWidth) || 1, h = (el && el.clientHeight) || 1;
    return { w, h, disp: Math.min(w, h) || 1 };
  }
  function aim() {
    const h = azimuth * Math.PI / 180, e = tilt * Math.PI / 180;
    camDir.set(-Math.sin(h) * Math.cos(e), Math.sin(e), Math.cos(h) * Math.cos(e));
  }
  function place() {
    const d = baseDist * dolly / zoom;
    camera.position.set(target.x + d * camDir.x, d * camDir.y + target.y, target.z + d * camDir.z);
    camera.up.set(0, 1, 0);
    camera.lookAt(target);
    camera.updateMatrixWorld();
  }
  // view.panX/Y are display pixels in the square the engine sizes (min of w,h).
  function setView(v) {
    zoom = Math.max(1, Math.min(8, v.zoom || 1));
    azimuth = Number.isFinite(v.azimuth) ? v.azimuth : 0;
    tilt = Number.isFinite(v.tilt) ? Math.max(20, Math.min(80, v.tilt)) : 35;
    aim();
    const disp = size().disp;
    const fx = Math.max(0, Math.min(1, (0.5 - (v.panX || 0) / disp) / zoom));
    const fz = Math.max(0, Math.min(1, (0.5 - (v.panY || 0) / disp) / zoom));
    target.set(cx - R + fx * 2 * R, 0, cz - R + fz * 2 * R);
    place();
  }
  function resize() {
    const s = size();
    camera.aspect = s.w / s.h;
    camera.updateProjectionMatrix();
    // fit the table width on narrow views, height on wide ones
    baseDist = R * Math.max(2.15, 3.9 / camera.aspect);
    place();
  }
  // viewport px -> point on the y = h plane (default the table top), or null
  function unprojectPlane(mx, my, h) {
    const s = size();
    ndc.set((mx / s.w) * 2 - 1, -((my / s.h) * 2 - 1));
    ray.setFromCamera(ndc, camera);
    const o = ray.ray.origin, d = ray.ray.direction;
    if (d.y > -1e-6) return null;
    const t = (h - o.y) / d.y;
    return t > 0 ? { x: o.x + d.x * t, z: o.z + d.z * t } : null;
  }
  // world point -> viewport px (+ visibility in front of the camera)
  function project(x, y, z) {
    proj.set(x, y, z).project(camera);
    const s = size();
    return { x: (proj.x + 1) / 2 * s.w, y: (1 - proj.y) / 2 * s.h, visible: proj.z > -1 && proj.z < 1 };
  }
  // normalized table coords (0..1000, like the engine's panToNormalized) of a world point
  function normalized(x, z) {
    return { nx: ((x - (cx - R)) / (2 * R)) * 1000, ny: ((z - (cz - R)) / (2 * R)) * 1000 };
  }
  function ndcRay(mx, my) {
    const s = size();
    ndc.set((mx / s.w) * 2 - 1, -((my / s.h) * 2 - 1));
    ray.setFromCamera(ndc, camera);
    return ray;
  }
  function setDolly(f) { dolly = f; place(); }
  aim(); resize();
  return { camera, setDolly, setView, resize, project, unprojectPlane, normalized, ndcRay, target };
}
