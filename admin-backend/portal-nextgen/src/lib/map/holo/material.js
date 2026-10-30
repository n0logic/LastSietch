// Emissive amber hologram material for the relief (contract 4.4). WebGL2 GLSL,
// no TSL, no WebGPU. Features: fresnel rim glow (view-dependent edge
// brightening, our bloom substitute so we keep one canvas and no
// EffectComposer), a slow scanline sweep, and etched 9x9 sector grid lines.
// Theme-aware: colors are fed from the CSS token bridge (index reads --accent /
// --accent-bright / --ls-ibad / --bg-deep and hands them in). Amber is the
// chrome; Ibad blue stays reserved for live cues, so it is not used here. Only
// uTime changes per frame; everything else changes on theme/resize.
//
// 2.26.0 surface options (all off by default, so DD and the seeded maps render
// exactly as before):
//   - grid off (uGrid 0) for maps without a survey grid (Hagga);
//   - a baked heightfield normal map (RGB = -dh/dx, -dh/dy, nz; rows = world +Y,
//     i.e. board +ny = three +Z), replacing the coarse mesh normal so cliffs and
//     hoodoos light without extra vertices;
//   - the map-art drape: the 2D backdrop image laid over the relief with simple
//     NW-sun shading, dimmed so additive markers still read on top.
// Both textures cover the board 0..uView exactly (the bake's world rect equals
// the map cal, pinned by the artifact test), with image row 0 at the north edge.

const VERT = `
  varying vec3 vWorld;
  varying vec3 vNormal;
  varying vec3 vView;
  void main() {
    vec4 wp = modelMatrix * vec4(position, 1.0);
    vWorld = wp.xyz;
    vNormal = normalize(mat3(modelMatrix) * normal);
    vView = normalize(cameraPosition - wp.xyz);
    gl_Position = projectionMatrix * viewMatrix * wp;
  }
`;

const FRAG = `
  precision highp float;
  uniform vec3 uAccent;
  uniform vec3 uAccentBright;
  uniform vec3 uBgDeep;
  uniform float uTime;
  uniform float uView;
  uniform float uCols;
  uniform float uReduce;
  uniform float uGrid;
  uniform float uHasNormal;
  uniform float uHasDrape;
  uniform float uRelief;
  uniform sampler2D uNormalMap;
  uniform sampler2D uDrape;
  uniform float uDrapeGain;   // per-bake exposure (meta.drapeGain): dark hub night renders read brighter
  uniform vec3 uSunDir;       // time of day (holo/light.js): key light direction, colour, fills
  uniform vec3 uSunCol;
  uniform vec3 uAmbCol;
  uniform vec3 uHemiCol;
  uniform float uExposure;
  uniform vec3 uTint;         // hologram branch: subtle tint shift only
  varying vec3 vWorld;
  varying vec3 vNormal;
  varying vec3 vView;

  // Antialiased grid line at every sector border on the XZ plane.
  float grid(vec2 p, float cell) {
    vec2 g = abs(fract(p / cell - 0.5) - 0.5) / fwidth(p / cell);
    float line = 1.0 - min(min(g.x, g.y), 1.0);
    return line;
  }

  void main() {
    vec2 xz = vWorld.xz + uView * 0.5;      // back to 0..uView
    // Texture uv: flipY textures put image row 0 (north, ny 0) at v = 1.
    vec2 tuv = vec2(xz.x / uView, 1.0 - xz.y / uView);

    vec3 n = normalize(vNormal);
    if (uHasNormal > 0.5) {
      // Baked at true scale; slopes scale with the runtime relief boost.
      vec3 t = texture2D(uNormalMap, tuv).rgb * 2.0 - 1.0;
      n = normalize(vec3(t.r * uRelief, t.b, t.g * uRelief));
    }
    float fres = pow(1.0 - clamp(dot(n, normalize(vView)), 0.0, 1.0), 3.0);

    float cell = uView / uCols;
    float g = grid(xz, cell) * uGrid;

    // Slow diagonal scan band; frozen when reduced-motion is forced.
    float sweepPhase = (uReduce > 0.5) ? 0.0 : uTime * 0.06;
    float sweep = smoothstep(0.92, 1.0,
      sin((xz.x + xz.y) / uView * 6.2831 - sweepPhase * 6.2831) * 0.5 + 0.5);

    if (uHasDrape > 0.5) {
      float lambert = max(dot(n, normalize(uSunDir)), 0.0);
      vec3 art = texture2D(uDrape, tuv).rgb;
      vec3 light = uAmbCol + uHemiCol * (0.5 + 0.5 * n.y) + uSunCol * lambert;
      vec3 lit = min(art * uDrapeGain, vec3(1.0)) * light * 0.8 * uExposure;
      lit = mix(lit, uAccent, g * 0.6);
      lit = mix(lit, uAccentBright, fres * 0.2);
      lit += uAccentBright * sweep * 0.08;
      gl_FragColor = vec4(lit, 1.0);
      return;
    }

    vec3 col = mix(uBgDeep, uAccent, 0.35);
    col = mix(col, uAccent, g * 0.9);
    col = mix(col, uAccentBright, fres * 0.85);
    col += uAccentBright * sweep * 0.25;
    col *= uTint;

    float alpha = 0.30 + fres * 0.55 + g * 0.45 + sweep * 0.12;
    alpha = clamp(alpha, 0.0, 0.95);
    gl_FragColor = vec4(col, alpha);
  }
`;

// createHoloMaterial(THREE, { view, tokens, reducedMotion }) ->
//   { material, setTokens, setTime, setGrid, setSurface, dispose }
export function createHoloMaterial(THREE, opts) {
  const view = opts.view || 1000;
  const t = opts.tokens;
  const material = new THREE.ShaderMaterial({
    vertexShader: VERT,
    fragmentShader: FRAG,
    transparent: true,
    depthWrite: true,
    side: THREE.DoubleSide,
    uniforms: {
      uAccent: { value: t.accent.clone() },
      uAccentBright: { value: t.accentBright.clone() },
      uBgDeep: { value: t.bgDeep.clone() },
      uTime: { value: 0 },
      uView: { value: view },
      uCols: { value: 9 },
      uReduce: { value: opts.reducedMotion ? 1 : 0 },
      uGrid: { value: 1 },
      uHasNormal: { value: 0 },
      uHasDrape: { value: 0 },
      uRelief: { value: 1 },
      uNormalMap: { value: null },
      uDrape: { value: null },
      uDrapeGain: { value: 1.0 },
      // default = the old fixed look (NW sun, ambient 0.35, key 0.75) until setLight runs
      uSunDir: { value: new THREE.Vector3(-0.5, 0.8, -0.35).normalize() },
      uSunCol: { value: new THREE.Vector3(0.75, 0.75, 0.75) },
      uAmbCol: { value: new THREE.Vector3(0.35, 0.35, 0.35) },
      uHemiCol: { value: new THREE.Vector3(0, 0, 0) },
      uExposure: { value: 1 },
      uTint: { value: new THREE.Vector3(1, 1, 1) },
    },
  });
  const u = material.uniforms;
  let surfaceToken = 0;

  function setTokens(tokens) {
    material.uniforms.uAccent.value.copy(tokens.accent);
    material.uniforms.uAccentBright.value.copy(tokens.accentBright);
    material.uniforms.uBgDeep.value.copy(tokens.bgDeep);
  }

  function setTime(sec) {
    material.uniforms.uTime.value = sec;
  }

  // setLight(holoLight(...)): drape sun / ambient / exposure and the hologram tint
  function setLight(l) {
    u.uSunDir.value.set(l.sunDir[0], l.sunDir[1], l.sunDir[2]).normalize();
    u.uSunCol.value.set(l.sun[0], l.sun[1], l.sun[2]);
    u.uAmbCol.value.set(l.ambient[0], l.ambient[1], l.ambient[2]);
    u.uHemiCol.value.set(l.hemi[0], l.hemi[1], l.hemi[2]);
    u.uExposure.value = l.exposure;
    u.uTint.value.set(l.tint[0], l.tint[1], l.tint[2]);
  }

  function setGrid(on) {
    u.uGrid.value = on ? 1 : 0;
  }

  function loadTexture(url, anisotropy, onLoad) {
    new THREE.TextureLoader().load(url, function (tex) {
      tex.wrapS = tex.wrapT = THREE.ClampToEdgeWrapping;
      tex.anisotropy = anisotropy || 1;
      onLoad(tex);
    }, undefined, function (err) {
      console.warn('[holo] surface texture ' + url + ' failed; keeping the plain relief', err);
    });
  }

  function swapTexture(slot, flag, tex) {
    const old = u[slot].value;
    u[slot].value = tex;
    u[flag].value = tex ? 1 : 0;
    if (old && old !== tex) old.dispose();
  }

  // setSurface({ normalUrl, drapeUrl, relief, anisotropy, drapeGain }): load the optional
  // normal map / drape for the rendered bake. Null urls clear them. Each texture
  // switches on when it arrives; a failed load just leaves that slot off. A
  // newer call supersedes any load still in flight.
  function setSurface(s) {
    s = s || {};
    const token = ++surfaceToken;
    u.uRelief.value = typeof s.relief === 'number' && isFinite(s.relief) ? s.relief : 1;
    u.uDrapeGain.value = typeof s.drapeGain === 'number' && isFinite(s.drapeGain) && s.drapeGain > 0 ? s.drapeGain : 1;
    if (!s.normalUrl) swapTexture('uNormalMap', 'uHasNormal', null);
    if (!s.drapeUrl) swapTexture('uDrape', 'uHasDrape', null);
    if (s.normalUrl) {
      loadTexture(s.normalUrl, s.anisotropy, function (tex) {
        if (token !== surfaceToken) { tex.dispose(); return; }
        swapTexture('uNormalMap', 'uHasNormal', tex);
      });
    }
    if (s.drapeUrl) {
      loadTexture(s.drapeUrl, s.anisotropy, function (tex) {
        if (token !== surfaceToken) { tex.dispose(); return; }
        swapTexture('uDrape', 'uHasDrape', tex);
      });
    }
  }

  function dispose() {
    surfaceToken++;
    swapTexture('uNormalMap', 'uHasNormal', null);
    swapTexture('uDrape', 'uHasDrape', null);
    material.dispose();
  }

  return { material, setTokens, setTime, setGrid, setLight, setSurface, dispose };
}
