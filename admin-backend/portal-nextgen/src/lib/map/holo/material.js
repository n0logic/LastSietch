// Emissive amber hologram material for the relief (contract 4.4). WebGL2 GLSL,
// no TSL, no WebGPU. Features: fresnel rim glow (view-dependent edge
// brightening, our bloom substitute so we keep one canvas and no
// EffectComposer), a slow scanline sweep, and etched 9x9 sector grid lines.
// Theme-aware: colors are fed from the CSS token bridge (index reads --accent /
// --accent-bright / --ls-ibad / --bg-deep and hands them in). Amber is the
// chrome; Ibad blue stays reserved for live cues, so it is not used here. Only
// uTime changes per frame; everything else changes on theme/resize.

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
    vec3 n = normalize(vNormal);
    float fres = pow(1.0 - clamp(dot(n, normalize(vView)), 0.0, 1.0), 3.0);

    vec2 xz = vWorld.xz + uView * 0.5;      // back to 0..uView
    float cell = uView / uCols;
    float g = grid(xz, cell);

    // Slow diagonal scan band; frozen when reduced-motion is forced.
    float sweepPhase = (uReduce > 0.5) ? 0.0 : uTime * 0.06;
    float sweep = smoothstep(0.92, 1.0,
      sin((xz.x + xz.y) / uView * 6.2831 - sweepPhase * 6.2831) * 0.5 + 0.5);

    vec3 col = mix(uBgDeep, uAccent, 0.35);
    col = mix(col, uAccent, g * 0.9);
    col = mix(col, uAccentBright, fres * 0.85);
    col += uAccentBright * sweep * 0.25;

    float alpha = 0.30 + fres * 0.55 + g * 0.45 + sweep * 0.12;
    alpha = clamp(alpha, 0.0, 0.95);
    gl_FragColor = vec4(col, alpha);
  }
`;

// createHoloMaterial(THREE, { view, tokens, reducedMotion }) ->
//   { material, setTokens, setTime, dispose }
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
    },
  });

  function setTokens(tokens) {
    material.uniforms.uAccent.value.copy(tokens.accent);
    material.uniforms.uAccentBright.value.copy(tokens.accentBright);
    material.uniforms.uBgDeep.value.copy(tokens.bgDeep);
  }

  function setTime(sec) {
    material.uniforms.uTime.value = sec;
  }

  function dispose() {
    material.dispose();
  }

  return { material, setTokens, setTime, dispose };
}
