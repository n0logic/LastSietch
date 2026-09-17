// Sector ID labels for the holo grid (owner M3.1 addon). The 9x9 grid itself is
// etched in the terrain material shader (no geometry), so labels are a separate
// element: ONE text-atlas texture (all 81 "A1".."I9" strings baked to a small
// canvas once) + ONE InstancedMesh of camera-facing quads, one per sector, each
// with a per-instance UV offset selecting its atlas cell. One draw call total.
//
// Sector convention (verified against admin-backend/map_model.py sector_for,
// deep-desert cal flipY=false, VIEW=1000): the LETTER is the ROW A..I south->
// north (A=south=+y=high ny, I=north=-y=low ny); the NUMBER is the COLUMN 1..9
// west->east (col grows with nx). In normalized space:
//   col   = floor(nx / (view/9)) + 1
//   rowIdx= floor(9 - 9*ny/view)   (0='A')
// so a cell's screen top-left (north-west = low nx, low ny) is
//   (ci*step, (8-ri)*step)   for column index ci and letter-row index ri.
//
// Chrome, not live data: dry amber, low opacity, so labels never read as a live
// cue (Ibad/purple stay reserved). THREE is injected (index.js is the sole three
// importer). Holo-tier only by construction.

const AX = 9;            // atlas columns
const AY = 9;            // atlas rows
const CELL_W = 96;       // atlas cell px
const CELL_H = 48;

const LABEL_VERT = `
  attribute vec2 aUv;
  uniform vec2 uCell; uniform float uW; uniform float uH;
  varying vec2 vUv;
  void main() {
    vUv = aUv + uv * uCell;
    // Camera-facing billboard: offset the instance center in view space so the
    // label stays upright and readable under the fixed lean (apparent size
    // scales with depth naturally).
    vec4 mv = viewMatrix * modelMatrix * instanceMatrix * vec4(0.0, 0.0, 0.0, 1.0);
    mv.x += position.x * uW;
    mv.y += position.y * uH;
    gl_Position = projectionMatrix * mv;
  }
`;
const LABEL_FRAG = `
  precision highp float;
  uniform sampler2D uAtlas; uniform vec3 uColor; uniform float uOpacity;
  varying vec2 vUv;
  void main() {
    float a = texture2D(uAtlas, vUv).a;
    if (a < 0.02) discard;
    gl_FragColor = vec4(uColor, a * uOpacity);
  }
`;

function buildAtlas(THREE) {
  const canvas = document.createElement('canvas');
  canvas.width = AX * CELL_W;
  canvas.height = AY * CELL_H;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = '#ffffff';                 // white glyphs; tinted amber in-shader
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.font = 'bold 30px "JetBrains Mono", ui-monospace, monospace';
  for (let ri = 0; ri < AY; ri++) {
    for (let ci = 0; ci < AX; ci++) {
      const label = String.fromCharCode(65 + ri) + (ci + 1);
      ctx.fillText(label, ci * CELL_W + CELL_W / 2, ri * CELL_H + CELL_H / 2);
    }
  }
  const tex = new THREE.CanvasTexture(canvas);
  tex.minFilter = THREE.LinearFilter;        // NPOT-safe (no mipmaps)
  tex.magFilter = THREE.LinearFilter;
  tex.wrapS = tex.wrapT = THREE.ClampToEdgeWrapping;
  tex.generateMipmaps = false;
  tex.needsUpdate = true;
  return tex;
}

function quadGeo(THREE) {
  const pos = new Float32Array([
    -0.5, -0.5, 0, 0.5, -0.5, 0, 0.5, 0.5, 0, -0.5, 0.5, 0,
  ]);
  const uv = new Float32Array([0, 0, 1, 0, 1, 1, 0, 1]);
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  g.setAttribute('uv', new THREE.BufferAttribute(uv, 2));
  g.setIndex([0, 1, 2, 0, 2, 3]);
  return g;
}

// createLabels(THREE, { view, grid, heightAt, tokens }) -> { mesh, setTokens,
//   setVisible, reposition, dispose }. grid defaults to 9x9 (the only grid map).
export function createLabels(THREE, opts) {
  const view = opts.view || 1000;
  const half = view / 2;
  const cols = (opts.grid && opts.grid.cols) || 9;
  const rows = (opts.grid && opts.grid.rows) || 9;
  const heightAt = opts.heightAt;
  const step = view / cols;
  const inset = step * 0.14;
  const lift = view * 0.012;
  const count = cols * rows;

  const atlas = buildAtlas(THREE);
  const geo = quadGeo(THREE);
  const aUv = new Float32Array(count * 2);
  const corners = new Float32Array(count * 2);   // nx,ny per instance for reposition

  let idx = 0;
  for (let ri = 0; ri < rows; ri++) {
    for (let ci = 0; ci < cols; ci++) {
      // top-left of the cell on screen = north-west = (low nx, low ny)
      const nx = ci * step + inset;
      const ny = (rows - 1 - ri) * step + inset;
      corners[idx * 2] = nx;
      corners[idx * 2 + 1] = ny;
      aUv[idx * 2] = ci / AX;
      aUv[idx * 2 + 1] = 1 - (ri + 1) / AY;     // texture flipY=true -> row from bottom
      idx++;
    }
  }
  geo.setAttribute('aUv', new THREE.InstancedBufferAttribute(aUv, 2));

  const material = new THREE.ShaderMaterial({
    vertexShader: LABEL_VERT, fragmentShader: LABEL_FRAG,
    transparent: true, depthWrite: false,
    uniforms: {
      uAtlas: { value: atlas },
      uColor: { value: opts.tokens.accent.clone() },
      uOpacity: { value: 0.55 },
      uCell: { value: new THREE.Vector2(1 / AX, 1 / AY) },
      uW: { value: view * 0.055 }, uH: { value: view * 0.028 },
    },
  });

  const mesh = new THREE.InstancedMesh(geo, material, count);
  mesh.frustumCulled = false;
  mesh.renderOrder = 1;                          // over the relief, under the popups

  const M4 = new THREE.Matrix4();
  function place() {
    for (let i = 0; i < count; i++) {
      const nx = corners[i * 2], ny = corners[i * 2 + 1];
      M4.makeTranslation(nx - half, heightAt(nx, ny) + lift, ny - half);
      mesh.setMatrixAt(i, M4);
    }
    mesh.instanceMatrix.needsUpdate = true;
  }
  place();

  function setTokens(tokens) {
    material.uniforms.uColor.value.copy(tokens.accent);
  }
  function setVisible(on) {
    mesh.visible = on !== false;
  }
  function reposition() {   // heightAt changed (M4 relief swap): re-lift labels
    place();
  }
  function dispose() {
    geo.dispose();
    material.dispose();
    atlas.dispose();
  }

  return { mesh, setTokens, setVisible, reposition, dispose };
}
