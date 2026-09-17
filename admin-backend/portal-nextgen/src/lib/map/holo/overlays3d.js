// Live overlay layer for the holo table (contract 4.4). Renders from the engine
// `scene` snapshot (4.1), reusing engine outputs; it never re-fetches. All
// layers are instanced or pooled geometry so draw calls stay in the low tens.
// THREE is injected (index.js is the sole three importer). createReckoner drives
// worm interpolation so dead-reckoning is not re-implemented.
//
// Color rules: Ibad blue is reserved for verified-live data (the active Large
// blow, and the me-pin only while fresh). Everything catalogued or static is
// amber. The worm threat palette is house-independent (fixed hex) and is always
// paired with a size cue, so color is never the sole signal and never collides
// with a Harkonnen red accent.

const WORM_GLIDE_MS = 10000;   // = /live cadence, matches the carved path
const SPICE_MAX = 64;
const WORM_MAX = 32;
// Pin columns are shared by your me-layer, waypoints, AND the live other-player
// crowd (Hagga). Me/waypoints are pushed first so they always claim slots;
// others fill the rest up to this cap.
const PIN_MAX = 256;

// Sandstorm. During an active storm the RAM reader emits live POSITION
// (center_nx/ny, radius_nr, heading_yaw) -> positioned dome; the reader covers
// PvP (dim1) ONLY, so PvE always uses the timing sweep. Storm on the board =
// ACTIVE STORM ONLY (no pre-spawn cue; the HUD countdown carries "incoming").
// Lifecycle measured 2026-07-04 (ops/storm-heading-verify): a storm sweeps the
// board in ~15.5 min and is tracked ~17 min, hence the 18 min windows.
const STORM_ACTIVE_MIN = 18;     // sweep window after a DETECTED spawn (last_spawn_utc)
const STORM_SCAN_FRESH_MIN = 12; // fresh storm_scanned_utc = confirmed active (dashboard rule)
const STORM_ETA_GRACE_MIN = 18;  // after next_eta_utc passes with NO new spawn detected yet
                                 // (PvE detector lag), sweep at reduced intensity = estimated
const STORM_LOC_FRESH_MIN = 20; // live-position freshness window (mirrors V1 locFresh); fades over the last 5 min
// Flip true (or set scene.sandstormForce = true in a temp fixture) to force the
// storm visible regardless of live timing/freshness, for eyeballing.
const STORM_FORCE = false;

const THREAT_HEX = {
  submerged: '#8a6a2a',
  surfaced: '#d4891c',
  enraged: '#ef7a1c',
  breaching: '#ef4038',
};
const THREAT_SIZE = {
  submerged: 0.55, surfaced: 1.0, enraged: 1.35, breaching: 1.7,
};

// ---- shaders ----------------------------------------------------------------

const COLUMN_VERT = `
  attribute float aPulse;
  varying vec3 vColor; varying float vY; varying float vPulse;
  void main() {
    vColor = instanceColor; vY = uv.y; vPulse = aPulse;
    gl_Position = projectionMatrix * viewMatrix * modelMatrix * instanceMatrix * vec4(position, 1.0);
  }
`;
const COLUMN_FRAG = `
  precision highp float;
  uniform float uTime; uniform float uReduce;
  varying vec3 vColor; varying float vY; varying float vPulse;
  void main() {
    float fade = 1.0 - vY;
    // Reduced-motion: steady full intensity, no breathe (contract 5, QA 3).
    float pulse = (uReduce > 0.5) ? 1.0
      : 1.0 + vPulse * 0.5 * (0.5 + 0.5 * sin(uTime * 2.4));
    float a = clamp(fade * 0.85 * pulse, 0.0, 1.0);
    gl_FragColor = vec4(vColor * (0.85 + 0.5 * fade) * pulse, a);
  }
`;

const DISC_VERT = `
  varying vec3 vColor; varying vec2 vUv;
  void main() {
    vColor = instanceColor; vUv = uv;
    gl_Position = projectionMatrix * viewMatrix * modelMatrix * instanceMatrix * vec4(position, 1.0);
  }
`;
const BLIP_FRAG = `
  precision highp float;
  varying vec3 vColor; varying vec2 vUv;
  void main() {
    float d = length(vUv - 0.5) * 2.0;
    if (d > 1.0) discard;
    float a = smoothstep(1.0, 0.0, d);
    gl_FragColor = vec4(vColor, a * 0.9);
  }
`;
const RIPPLE_VERT = `
  attribute float aPhase;
  varying vec3 vColor; varying vec2 vUv; varying float vPhase;
  void main() {
    vColor = instanceColor; vUv = uv; vPhase = aPhase;
    gl_Position = projectionMatrix * viewMatrix * modelMatrix * instanceMatrix * vec4(position, 1.0);
  }
`;
const RIPPLE_FRAG = `
  precision highp float;
  uniform float uTime; uniform float uReduce;
  varying vec3 vColor; varying vec2 vUv; varying float vPhase;
  void main() {
    float d = length(vUv - 0.5) * 2.0;
    // Reduced-motion: freeze the ripple to a static sonar ring (blip retained
    // on its own mesh); no expansion animation (contract 5, QA 3).
    float t = (uReduce > 0.5) ? 0.6 : fract(uTime * 0.5 + vPhase);
    float ring = smoothstep(0.06, 0.0, abs(d - t));
    float fade = (uReduce > 0.5) ? 0.5 : 1.0 - t;
    gl_FragColor = vec4(vColor, ring * fade * 0.6);
  }
`;

const RING_VERT = `
  varying vec2 vUv;
  void main() {
    vUv = uv;
    gl_Position = projectionMatrix * viewMatrix * modelMatrix * instanceMatrix * vec4(position, 1.0);
  }
`;
const RING_FRAG = `
  precision highp float;
  uniform float uTime; uniform vec3 uColor; uniform float uReduce;
  varying vec2 vUv;
  void main() {
    float band = smoothstep(0.0, 0.5, vUv.y) * smoothstep(1.0, 0.5, vUv.y);
    // Reduced-motion: steady rim glow, no pulse (contract 5, QA 3).
    float pulse = (uReduce > 0.5) ? 0.8 : 0.6 + 0.4 * sin(uTime * 1.6);
    gl_FragColor = vec4(uColor, band * 0.5 * pulse);
  }
`;

const POINTS_VERT = `
  attribute vec3 aColor;
  attribute float aSize;                 // per-point emphasis multiplier (1.0 = generic)
  uniform float uSize; uniform float uMin; uniform float uMax;
  uniform float uZoom; uniform float uBoost; uniform float uZoomStart;
  varying vec3 vColor; varying float vHot;
  void main() {
    vColor = aColor; vHot = aSize;
    // Zoom-driven size: the lean framing keeps most of the board far from the
    // camera, so raw dolly attenuation barely grew apparent size on zoom-in.
    // Drive size explicitly from the engine zoom (uZoom, fed via setView): a
    // curve that stays 1x until close range then ramps to uBoost, so zoom-out
    // is unchanged and full zoom-in reaches ~uBoost x. Clamp px so 17k points
    // never become soup zoomed out. Emphasis multiplier applies before clamp so
    // Titanium/Stravidium can exceed the generic cap.
    float boost = mix(1.0, uBoost, smoothstep(uZoomStart, 8.0, uZoom));
    float sz = clamp(uSize * boost * aSize, uMin, uMax);
    gl_PointSize = sz;
    gl_Position = projectionMatrix * viewMatrix * modelMatrix * vec4(position, 1.0);
  }
`;
const POINTS_FRAG = `
  precision highp float;
  varying vec3 vColor; varying float vHot;
  void main() {
    float d = length(gl_PointCoord - 0.5) * 2.0;
    if (d > 1.0) discard;
    float a = smoothstep(1.0, 0.25, d);
    vec3 col = vColor;
    // Emphasized ore (Titanium/Stravidium): static white-hot core + soft halo,
    // category hue preserved at the rim so the legend still maps. No pulse.
    if (vHot > 1.2) {
      float core = smoothstep(0.5, 0.0, d);
      col = mix(col, vec3(1.0), core * 0.7);
      a = max(a, smoothstep(1.0, 0.0, d) * 0.85);
    }
    gl_FragColor = vec4(col, a);
  }
`;

// Eruption spice bloom: pre-seeded particles (position = unit direction in
// x/z, per-particle radial jitter in y) rise from the active blow, phase driven
// by uTime (no per-frame allocation). Radius follows a mushroom profile: narrow
// stem low, blooming to a wide cap in the upper third with a slight inward curl
// at the very top. Two layers, both driven by this one vertex shader:
//   1. DUST (alpha-over, spice-cloud sprite) = the warm amber/rust billowing
//      body, with the purple energy core showing only in the low venting stem.
//   2. MOTES (additive, spice-motes sprite) = the purple energy glow at the
//      stem plus subtle amber catch-light glints rising through the cloud.
// The sprite alpha supplies the soft shape; a procedural soft dot is the
// fallback until (or if) the texture resolves so a failed fetch never errors.
// Point size is clamped in px so it can never go sub-pixel (the prior bug).
// Reduced-motion freezes each particle at its seed phase = a static mushroom.
const PLUME_VERT = `
  attribute float aSeed; attribute float aSpeed;
  uniform float uTime; uniform float uReduce; uniform float uHeight;
  uniform float uStem; uniform float uCap;
  uniform float uPSize; uniform float uView; uniform float uPMin; uniform float uPMax;
  varying float vPh; varying float vRad; varying float vRise; varying float vGlint;
  void main() {
    float ph = (uReduce > 0.5) ? aSeed : fract(uTime * aSpeed * 0.12 + aSeed);
    // mushroom radius vs height: stem then blooming cap then slight curl-in
    float bulge = smoothstep(0.5, 0.82, ph) * (1.0 - 0.45 * smoothstep(0.86, 1.0, ph));
    float radius = (uStem + uCap * bulge) * position.y;   // position.y = radial jitter
    vec3 p;
    p.x = position.x * radius;
    p.z = position.z * radius;
    p.y = ph * uHeight;
    vPh = ph;
    vRad = position.y;                                    // 0.4..1.0 radial jitter (density)
    vRise = 1.0 - smoothstep(0.72, 1.0, ph) * 0.8;        // fade as it dissipates up top
    // Spice glint: ~25% of particles catch the light with a high-frequency
    // twinkle; frozen to a static speck when reduced-motion.
    float isGlint = step(0.75, fract(aSeed * 7.13));
    float tw = (uReduce > 0.5) ? 0.6 : (0.5 + 0.5 * sin(uTime * 6.0 + aSeed * 40.0));
    vGlint = isGlint * tw;
    vec4 mv = viewMatrix * modelMatrix * vec4(p, 1.0);
    float sz = clamp(uPSize * uView / max(-mv.z, 1.0), uPMin, uPMax) * (0.75 + 0.6 * bulge);
    gl_PointSize = sz;
    gl_Position = projectionMatrix * mv;
  }
`;
// Dust body: warm amber highlight blending to rust in the dense mid, alpha-over
// so it reads as billowing melange dust (not neon). Purple energy core only in
// the low venting stem, fading to amber as it rises.
const PLUME_FRAG = `
  precision highp float;
  uniform sampler2D uCloud; uniform float uHasTex;
  uniform vec3 uAmber; uniform vec3 uRust; uniform vec3 uCore; uniform float uOpacity;
  varying float vPh; varying float vRad; varying float vRise; varying float vGlint;
  void main() {
    float d = length(gl_PointCoord - 0.5) * 2.0;
    float soft = smoothstep(1.0, 0.0, d);
    float shape = (uHasTex > 0.5) ? texture2D(uCloud, gl_PointCoord).a : soft;
    if (shape < 0.02) discard;
    float amberMix = clamp(vPh * 0.6 + (1.0 - vRad) * 0.6, 0.0, 1.0);
    vec3 dust = mix(uRust, uAmber, amberMix);
    float stem = 1.0 - smoothstep(0.05, 0.34, vPh);       // 1 at the vent, 0 above
    vec3 col = mix(dust, uCore, stem * 0.85);
    gl_FragColor = vec4(col, shape * vRise * uOpacity);
  }
`;
// Motes: additive sparkle. Purple energy glow concentrated at the venting stem,
// warm amber catch-light glints rising through the body (glint-gated so subtle).
const MOTES_FRAG = `
  precision highp float;
  uniform sampler2D uMotes; uniform float uHasTex;
  uniform vec3 uAmber; uniform vec3 uCore; uniform float uOpacity;
  varying float vPh; varying float vRad; varying float vRise; varying float vGlint;
  void main() {
    float d = length(gl_PointCoord - 0.5) * 2.0;
    float soft = smoothstep(1.0, 0.0, d);
    float shape = (uHasTex > 0.5) ? texture2D(uMotes, gl_PointCoord).r : soft;
    if (shape < 0.02) discard;
    float stem = 1.0 - smoothstep(0.05, 0.30, vPh);
    vec3 spark = mix(uAmber, vec3(1.0), 0.4 * vGlint);    // warm-white catch-light
    vec3 col = mix(spark, uCore, stem);
    float energy = stem * 0.9 + vGlint * 0.5 * (1.0 - stem);
    gl_FragColor = vec4(col, shape * energy * vRise * uOpacity);
  }
`;

// Sandstorm: one board-wide sheet baked to hug the terrain relief, driving two
// render modes (uMode) off the same mesh so draw calls stay at one.
//  - POSITIONED (uMode 1): the live RAM read carries the moving storm CENTER
//    (center_nx/ny), RADIUS (radius_nr) and HEADING (heading_yaw). We render the
//    churning flipbook cloud as a soft-edged dome centered there, sized to the
//    radius, the single 64x64 puff mapped across the disc so it reads as one
//    volumetric storm cloud, with a leading wisp + brighter front edge along the
//    heading. Freshness (from storm_scanned_utc, mirroring V1's 20 min locFresh)
//    is folded into uIntensity so a stale read fades out.
//  - SWEEP (uMode 0): fallback when there is no live position (PvE, or a dim the
//    reader has not scanned). A desert-wide dust WALL translates west->east while
//    the flipbook (tiled a few times) churns; short leading glow, long dust tail.
// Board coords are 0..VIEW and vUv = board/VIEW (isotropic), so center/radius map
// to UV as center/VIEW and radius/VIEW, and the heading (cos yaw, sin yaw) applies
// directly in UV. Heading convention mirrors V1 drawStorm: +x=East, +y=South; sign
// VERIFIED 2026-07-04 vs a live sweep (ops/storm-heading-verify/). Flipbook = 8x8 SubUV, 64 frames, alpha = dust density. Warm sand
// tint, alpha-over so it reads as billowing dust (not neon). Reduced-motion
// freezes churn + drift/sweep to a still cloud. The procedural constant-haze
// fallback keeps a failed texture fetch from ever throwing (uHasTex stays 0).
const STORM_VERT = `
  varying vec2 vUv;
  void main() {
    vUv = uv;
    gl_Position = projectionMatrix * viewMatrix * modelMatrix * vec4(position, 1.0);
  }
`;
// Depth comes from compositing THREE taps of the same flipbook at different
// scale / frame phase / drift (over-blended), so the cloud churns with volume
// instead of reading as one flat puff. Lightning rides on top: a deterministic
// hash off uTime fires a cool blue-white cloud FLASH every ~2-6s plus a short
// jagged BOLT at the flash peak. Both stay inside the one draw call and both
// are killed under reduced-motion (contract 5: no flashing).
const STORM_FRAG = `
  precision highp float;
  uniform sampler2D uStorm; uniform float uHasTex;
  uniform float uTime; uniform float uReduce; uniform float uIntensity;
  uniform float uFps; uniform float uTile; uniform float uSweep;
  uniform float uLead; uniform float uTrail; uniform float uOpacity;
  uniform float uMode; uniform vec2 uCenter; uniform float uRadius;
  uniform vec2 uDir; uniform float uDrift;
  uniform vec3 uColor; uniform vec3 uFlashColor;
  varying vec2 vUv;

  float hash11(float n) { return fract(sin(n * 127.1) * 43758.5453); }
  float vnoise(float x) {
    float i = floor(x), f = fract(x);
    float u = f * f * (3.0 - 2.0 * f);
    return mix(hash11(i), hash11(i + 1.0), u);
  }
  // One flipbook tap: base coord (pre-fract) + explicit frame -> dust alpha.
  float flipTap(vec2 baseUv, float frame) {
    float col = mod(frame, 8.0);
    float row = floor(frame / 8.0);
    vec2 f = fract(baseUv);
    vec2 cell = ((f * 0.96 + 0.02) + vec2(col, row)) / 8.0;   // inset avoids cross-cell bleed
    return (uHasTex > 0.5) ? texture2D(uStorm, cell).a : 0.55;
  }

  void main() {
    // churn gate: reduced-motion freezes flipbook frames + drift to a still cloud.
    float churn = (uReduce > 0.5) ? 0.0 : 1.0;
    float baseFrame = floor(uTime * uFps * churn);       // % 64 applied per layer

    float body;         // coverage mask 0..1
    float glow;         // leading-edge brighten
    vec2  buv;          // base flipbook coord before per-layer scale/drift
    vec2  driftDir;     // internal dust drift direction

    if (uMode > 0.5) {
      // POSITIONED dome at uCenter, sized to uRadius, drifting along uDir.
      vec2 rel = vUv - uCenter;
      float dist = length(rel) / max(uRadius, 1e-4);   // 0 center .. 1 edge
      if (dist > 1.25) discard;                         // allow slight overspill for the wisp
      float disc = smoothstep(1.0, 0.5, dist);          // soft-edged dome, dense center
      float along = dot(normalize(rel + 1e-5), uDir);   // +1 on the heading side
      float wisp = smoothstep(1.25, 0.85, dist) * clamp(along, 0.0, 1.0) * 0.6;
      body = max(disc, wisp);
      glow = smoothstep(0.85, 1.2, dist) * clamp(along, 0.0, 1.0);
      vec2 local = rel / max(uRadius, 1e-4);            // -1..1 across the disc
      buv = local * 0.5 + 0.5;                          // one puff fills the dome
      driftDir = -uDir;                                 // drift the dust ALONG the heading
    } else {
      // SWEEP wall along +x, wrapped for seamless re-entry.
      float sweepPos = (uReduce > 0.5) ? 0.5 : fract(uTime * uSweep);
      float d = vUv.x - sweepPos;
      d = d - floor(d + 0.5);
      float lead = smoothstep(uLead, 0.0, d);
      float trail = smoothstep(-uTrail, 0.0, d);
      body = mix(trail, lead, step(0.0, d));
      glow = lead * step(0.0, d);
      buv = vUv * uTile;                                // tile the puff across the board
      driftDir = vec2(1.0, 0.0);
    }
    if (body < 0.01) discard;

    // Five layers of the SAME puff: base / finer-faster / coarser-slower / mid /
    // fine-fastest, each at its own frame phase, scale, drift, and a decorrelating
    // UV offset so overlaps read thicker -> more volumetric depth, storm mass is
    // easier to see. Still ONE draw call (extra taps are cheap texture reads).
    vec2 dv = driftDir * (uTime * uDrift * churn);
    float a0 = flipTap(buv * 1.0  + dv * 1.0,                 mod(baseFrame +  0.0, 64.0));
    float a1 = flipTap(buv * 1.6  + dv * 1.7,                 mod(baseFrame + 21.0, 64.0));
    float a2 = flipTap(buv * 0.7  + dv * 0.6,                 mod(baseFrame + 40.0, 64.0));
    float a3 = flipTap(buv * 1.15 + dv * 1.15 + vec2(0.37, 0.12), mod(baseFrame + 11.0, 64.0));
    float a4 = flipTap(buv * 2.30 + dv * 2.40 + vec2(0.61, 0.43), mod(baseFrame + 54.0, 64.0));
    float dens = 1.0 - (1.0 - a0 * 0.60) * (1.0 - a1 * 0.32) * (1.0 - a2 * 0.38)
                     * (1.0 - a3 * 0.30) * (1.0 - a4 * 0.24);

    vec3 tint = uColor * (1.0 + 0.35 * glow);
    float a = body * dens * uOpacity * uIntensity;

    // ---- lightning: deterministic hash timing, no per-frame alloc, no churn
    // under reduced-motion (contract 5 forbids flashing). Rides storm intensity.
    if (uReduce < 0.5) {
      // Two independent strike trains at different tempos so lightning reads as
      // FREQUENT + SHORT (snappier flicker) instead of one slow bolt. Each is a
      // deterministic hash-timed slot -> no per-frame alloc, no churn under
      // reduced-motion (contract 5 forbids flashing).
      float flash = 0.0;
      float bolt = 0.0;
      for (int k = 0; k < 2; k++) {
        float kf = float(k);
        // train 0: ~1.6s slots (primary); train 1: ~0.9s slots (quick sub-flicker).
        float period = (k == 0) ? 1.6 : 0.9;
        float seed = kf * 17.0;
        float si = floor(uTime / period);                        // strike slot
        float fire = step((k == 0) ? 0.40 : 0.55,                // train0 ~60% / train1 ~45%
                          hash11((si + seed) * 1.7 + 3.0));
        float tStart = si * period + hash11(si + seed) * period * 0.6;
        float lt = uTime - tStart;
        // FLASH: sharp ~35ms attack, faster decay -> shorter pop.
        flash += fire * clamp(lt / 0.035, 0.0, 1.0)
                      * exp(-max(lt - 0.035, 0.0) * 11.0) * step(0.0, lt)
                      * ((k == 0) ? 1.0 : 0.7);
        // BOLT: snappier (~45ms) jagged vertical line + a short branch fork.
        float boltEnv = fire * clamp(1.0 - lt * 22.0, 0.0, 1.0) * step(0.0, lt);
        float boltX = (uMode > 0.5)
          ? uCenter.x + (hash11(si + seed + 0.5) - 0.5) * uRadius * 1.4
          : hash11(si + seed + 0.5);
        float jag = (vnoise(vUv.y * 9.0 + (si + seed) * 5.0) - 0.5) * 0.05
                  + (vnoise(vUv.y * 3.0 + (si + seed) * 5.0) - 0.5) * 0.09;
        float mainBolt = smoothstep(0.014, 0.0, abs(vUv.x - (boltX + jag)));
        // short branch forking off below a mid-height node.
        float forkY = 0.35 + hash11(si + seed + 2.3) * 0.25;
        float sideDir = hash11(si + seed + 4.1) - 0.5;
        float branchX = boltX + jag + sideDir * clamp(forkY - vUv.y, 0.0, 0.30) * 1.2;
        float branchJag = (vnoise(vUv.y * 12.0 + (si + seed) * 7.0) - 0.5) * 0.04;
        float branch = smoothstep(0.010, 0.0, abs(vUv.x - (branchX + branchJag)))
                     * step(vUv.y, forkY);
        bolt += (mainBolt + branch * 0.7) * boltEnv * ((k == 0) ? 1.0 : 0.75);
      }
      flash = clamp(flash, 0.0, 1.0);
      bolt = clamp(bolt, 0.0, 1.0) * body;

      float lit = uIntensity * body;
      tint = mix(tint, uFlashColor, clamp(flash * 0.85, 0.0, 1.0));
      a += flash * dens * lit * 0.6;
      tint += uFlashColor * bolt;                     // bright line (alpha-over via high a)
      a = max(a, bolt * uIntensity * 0.85);
    }

    gl_FragColor = vec4(tint, clamp(a, 0.0, 1.0));
  }
`;

// ---- geometry builders ------------------------------------------------------

function columnGeo(THREE) {
  const pos = new Float32Array([
    -0.5, 0, 0, 0.5, 0, 0, 0.5, 1, 0, -0.5, 1, 0,
    0, 0, -0.5, 0, 0, 0.5, 0, 1, 0.5, 0, 1, -0.5,
  ]);
  const uv = new Float32Array([
    0, 0, 1, 0, 1, 1, 0, 1,
    0, 0, 1, 0, 1, 1, 0, 1,
  ]);
  const idx = [0, 1, 2, 0, 2, 3, 4, 5, 6, 4, 6, 7];
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  g.setAttribute('uv', new THREE.BufferAttribute(uv, 2));
  g.setIndex(idx);
  return g;
}

function discGeo(THREE) {
  const pos = new Float32Array([
    -0.5, 0, -0.5, 0.5, 0, -0.5, 0.5, 0, 0.5, -0.5, 0, 0.5,
  ]);
  const uv = new Float32Array([0, 0, 1, 0, 1, 1, 0, 1]);
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  g.setAttribute('uv', new THREE.BufferAttribute(uv, 2));
  g.setIndex([0, 1, 2, 0, 2, 3]);
  return g;
}

function ringGeo(THREE, inner, outer, segs) {
  const pos = [], uv = [], idx = [];
  for (let i = 0; i <= segs; i++) {
    const a = (i / segs) * Math.PI * 2;
    const cx = Math.cos(a), cz = Math.sin(a);
    pos.push(cx * inner, 0, cz * inner);
    pos.push(cx * outer, 0, cz * outer);
    uv.push(i / segs, 0, i / segs, 1);
  }
  for (let i = 0; i < segs; i++) {
    const b = i * 2;
    idx.push(b, b + 1, b + 3, b, b + 3, b + 2);
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(pos), 3));
  g.setAttribute('uv', new THREE.BufferAttribute(new Float32Array(uv), 2));
  g.setIndex(idx);
  return g;
}

function dimVisible(dim, sel) {
  if (sel == null || dim == null) return true;
  return Number(dim) === Number(sel);
}

// Partition is the authoritative sietch discriminator when the selected
// instance declares one (Hagga 1/32); otherwise fall back to dimension (DD).
function instVisible(entity, selDim, selPart) {
  if (selPart != null) {
    if (entity.part == null) return true;
    return Number(entity.part) === Number(selPart);
  }
  return dimVisible(entity.dim, selDim);
}

// The public positions/vehicles rows carry partition as `p`. A missing selected
// partition (non-Hagga) draws nothing here; a row missing its own `p` is kept.
function partHere(entPart, selPart) {
  if (selPart == null) return false;
  if (entPart == null) return true;
  return Number(entPart) === Number(selPart);
}

// World (x, y) -> normalized 0..view, mirroring transform.worldToNormalized /
// map_model.normalize (incl. flipY). Inlined to keep the holo THREE boundary
// dependency-free. null on bad input so a malformed coord is skipped.
function worldToNorm(x, y, cal, view) {
  if (!cal || x == null || y == null) return null;
  let nx = (x - cal.originX) / cal.spanX * view;
  let ny = (y - cal.originY) / cal.spanY * view;
  if (cal.flipY) ny = view - ny;
  return { nx: nx, ny: ny };
}

// Board-wide dust sheet, subdivided and baked to hug the terrain relief (the
// heightfield is static, so heightAt is sampled once at construction). Lifts a
// touch above the surface so it hovers as a sweeping haze. UV spans 0..1 across
// the board, driving both the sweep band and the tiled flipbook in the shader.
function stormGeo(THREE, half, heightAt, lift, segs) {
  const pos = [], uv = [], idx = [];
  const stepW = (half * 2) / segs;
  for (let r = 0; r <= segs; r++) {
    for (let c = 0; c <= segs; c++) {
      const x = -half + c * stepW;
      const z = -half + r * stepW;
      const h = (heightAt ? heightAt(x + half, z + half) : 0) + lift;
      pos.push(x, h, z);
      uv.push(c / segs, r / segs);
    }
  }
  const w = segs + 1;
  for (let r = 0; r < segs; r++) {
    for (let c = 0; c < segs; c++) {
      const a = r * w + c, b = a + 1, cc = a + w, dd = cc + 1;
      idx.push(a, cc, b, b, cc, dd);
    }
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(pos), 3));
  g.setAttribute('uv', new THREE.BufferAttribute(new Float32Array(uv), 2));
  g.setIndex(idx);
  return g;
}

function isFiniteNum(v) { return typeof v === 'number' && isFinite(v); }

// Live-position freshness (mirrors V1 locFresh): 1.0 while the scan is recent,
// fading to 0 over the last 5 minutes of the STORM_LOC_FRESH_MIN window, 0 once
// stale or unparseable. Returns 0..1.
function stormLocFresh(info) {
  const scanned = info && info.storm_scanned_utc ? Date.parse(info.storm_scanned_utc) : NaN;
  if (isNaN(scanned)) return 0;
  const ageMin = (Date.now() - scanned) / 60000;
  if (ageMin <= STORM_LOC_FRESH_MIN - 5) return 1;
  if (ageMin >= STORM_LOC_FRESH_MIN) return 0;
  return 1 - (ageMin - (STORM_LOC_FRESH_MIN - 5)) / 5;
}

// Storm-active from timing (no usable position). ACTIVE STORMS ONLY, in trust
// order: (1) fresh storm_scanned_utc = the reader is tracking a storm right now
// (same rule the dashboard uses); (2) last_spawn_utc within the measured storm
// lifetime; (3) next_eta_utc recently PASSED while last_spawn still predates it
// = the storm is statistically in progress but the detector has not caught the
// spawn yet (PvE detection lags), shown fainter as an estimate. No pre-spawn
// cue: the countdown reaching zero is when the board should START showing dust,
// not stop. Returns null when nothing applies.
function stormActivity(info) {
  if (!info) return null;
  const now = Date.now();
  const scanned = info.storm_scanned_utc ? Date.parse(info.storm_scanned_utc) : NaN;
  // (1) The RAM reader is tracking a live storm right now (fresh position).
  if (!isNaN(scanned)) {
    const mins = (now - scanned) / 60000;
    if (mins >= 0 && mins <= STORM_SCAN_FRESH_MIN) return { active: true, intensity: 1 };
  }
  const spawn = info.last_spawn_utc ? Date.parse(info.last_spawn_utc) : NaN;
  // (v1.1 despawn override) The reader stamps storm_ended_utc when it sees the
  // tracked storm die. If that end is at/after the latest detected spawn -- and
  // (1) did not fire, so there is no live position -- the storm is OVER, so draw
  // nothing. This kills the post-storm "sweep tail" the raw last_spawn<18m window
  // below would otherwise paint for the rest of the 18 min. A newer spawn (ended <
  // spawn) supersedes it, so the next storm is unaffected.
  const ended = info.storm_ended_utc ? Date.parse(info.storm_ended_utc) : NaN;
  if (!isNaN(ended) && (isNaN(spawn) || ended >= spawn)) return null;
  // (2) Within the measured storm lifetime of a detected spawn (timing fallback
  // when the reader has no position yet -- e.g. the first storm before a dim's
  // instance addresses are learned).
  if (!isNaN(spawn)) {
    const mins = (now - spawn) / 60000;
    if (mins >= 0 && mins <= STORM_ACTIVE_MIN) return { active: true, intensity: 1 };
  }
  // (3) ETA just passed, spawn not yet detected (PvE detector lag) -- fainter
  // estimate. No storm_ended_utc here, so a genuine pre-detection storm still shows.
  const eta = info.next_eta_utc ? Date.parse(info.next_eta_utc) : NaN;
  if (!isNaN(eta) && (isNaN(spawn) || spawn < eta)) {
    const mins = (now - eta) / 60000;
    if (mins >= 0 && mins <= STORM_ETA_GRACE_MIN) return { active: true, intensity: 0.7 };
  }
  return null;
}

// Deterministic per-particle hash (no Math.random, so both devs + CI seed the
// same plume shape).
function phash(i, k) {
  let h = ((i * 73856093) ^ (k * 19349663)) >>> 0;
  h = (h ^ (h >>> 13)) >>> 0;
  h = (h * 1274126177) >>> 0;
  return ((h ^ (h >>> 16)) >>> 0) / 4294967295;
}

function plumeGeo(THREE, count) {
  const pos = new Float32Array(count * 3);   // position = (cos, radialJitter, sin)
  const seed = new Float32Array(count);
  const speed = new Float32Array(count);
  for (let i = 0; i < count; i++) {
    const a = phash(i, 1) * Math.PI * 2;
    const rj = 0.4 + 0.6 * phash(i, 2);      // per-particle radial jitter 0.4..1.0
    pos[i * 3] = Math.cos(a);
    pos[i * 3 + 1] = rj;
    pos[i * 3 + 2] = Math.sin(a);
    seed[i] = phash(i, 3);
    speed[i] = 0.7 + 0.6 * phash(i, 4);
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  g.setAttribute('aSeed', new THREE.BufferAttribute(seed, 1));
  g.setAttribute('aSpeed', new THREE.BufferAttribute(speed, 1));
  return g;
}

// createOverlays3d(THREE, { scene(Scene), view, heightAt, tokens, reducedMotion })
export function createOverlays3d(THREE, opts) {
  const root = opts.scene;
  const view = opts.view || 1000;
  const half = view / 2;
  const heightAt = opts.heightAt;
  let tokens = opts.tokens;

  const reduce = opts.reducedMotion ? 1 : 0;
  // SvelteKit `base` for same-origin static sprites; index.js wires it in.
  const assetBase = opts.assetBase != null ? opts.assetBase : '';
  const M4 = new THREE.Matrix4();
  const C = new THREE.Color();
  const SPICE_C = new THREE.Color();      // reused each updateSpice
  const WHITE = new THREE.Color(1, 1, 1);
  // Authentic melange bloom palette (extracted from the game SpiceBloom VFX):
  // melange-purple dust: light lavender highlight -> deep spice-violet mid; the
  // brighter energy core is the live spice color set per scene. setStyle keeps
  // these in the same sRGB->linear space as the CSS tokens (the game glows the
  // spice bloom purple, so we tint the real dust sprite melange, not amber).
  const AMBER = new THREE.Color().setStyle('rgb(198,150,224)');
  const RUST = new THREE.Color().setStyle('rgb(112,58,138)');
  const PLUME_COUNT = 420;                 // dense enough to read as a cloud
  const MOTES_COUNT = 130;                 // sparse additive sparkle + stem glow
  const threatColor = {};
  Object.keys(THREAT_HEX).forEach(function (k) {
    threatColor[k] = new THREE.Color(THREAT_HEX[k]);
  });

  const columnMat = new THREE.ShaderMaterial({
    vertexShader: COLUMN_VERT, fragmentShader: COLUMN_FRAG,
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    uniforms: { uTime: { value: 0 }, uReduce: { value: reduce } },
  });
  const blipMat = new THREE.ShaderMaterial({
    vertexShader: DISC_VERT, fragmentShader: BLIP_FRAG,
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    uniforms: {},
  });
  const rippleMat = new THREE.ShaderMaterial({
    vertexShader: RIPPLE_VERT, fragmentShader: RIPPLE_FRAG,
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    uniforms: { uTime: { value: 0 }, uReduce: { value: reduce } },
  });
  const ringMat = new THREE.ShaderMaterial({
    vertexShader: RING_VERT, fragmentShader: RING_FRAG,
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    uniforms: {
      uTime: { value: 0 }, uColor: { value: tokens.accent.clone() },
      uReduce: { value: reduce },
    },
  });
  const pointsMat = new THREE.ShaderMaterial({
    vertexShader: POINTS_VERT, fragmentShader: POINTS_FRAG,
    transparent: true, depthWrite: false,
    uniforms: {
      uSize: { value: 5.0 },        // base device px at zoom-out
      uMin: { value: 2.0 }, uMax: { value: 26.0 },
      uZoom: { value: 1 },          // engine zoom 1..8, driven by setView
      uBoost: { value: 2.7 },       // full zoom-in reaches ~2.7x zoom-out size
      uZoomStart: { value: 2.5 },   // boost only kicks in past mid-zoom
    },
  });
  const ORE_EMPHASIS = 1.8;   // Titanium/Stravidium size multiplier
  // Shared mushroom geometry uniforms (~75% of the prior hero size; owner asked
  // to reduce the whole plume by ~1/4). Both layers use PLUME_VERT.
  const plumeMat = new THREE.ShaderMaterial({
    vertexShader: PLUME_VERT, fragmentShader: PLUME_FRAG,
    transparent: true, depthWrite: false, blending: THREE.NormalBlending,
    uniforms: {
      uTime: { value: 0 }, uReduce: { value: reduce },
      uHeight: { value: view * 0.15 },
      uStem: { value: view * 0.009 },
      uCap: { value: view * 0.045 },
      uPSize: { value: 41 }, uView: { value: view },
      uPMin: { value: 10 }, uPMax: { value: 52 },      // px clamp floor kept: never sub-pixel
      uOpacity: { value: 0.9 },
      uAmber: { value: AMBER.clone() }, uRust: { value: RUST.clone() },
      uCore: { value: new THREE.Color('#a24bd4') },    // spice purple, set per scene
      uCloud: { value: null }, uHasTex: { value: 0 },  // procedural dot until sprite loads
    },
  });
  const motesMat = new THREE.ShaderMaterial({
    vertexShader: PLUME_VERT, fragmentShader: MOTES_FRAG,
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    uniforms: {
      uTime: { value: 0 }, uReduce: { value: reduce },
      uHeight: { value: view * 0.15 },
      uStem: { value: view * 0.009 },
      uCap: { value: view * 0.045 },
      uPSize: { value: 26 }, uView: { value: view },   // smaller than the dust body
      uPMin: { value: 6 }, uPMax: { value: 34 },
      uOpacity: { value: 0.55 },                       // subtle additive layer
      uAmber: { value: AMBER.clone() },
      uCore: { value: new THREE.Color('#a24bd4') },    // spice purple, set per scene
      uMotes: { value: null }, uHasTex: { value: 0 },
    },
  });

  // Sandstorm sweep: one board-wide sheet, warm sand dust, alpha-over. Colors
  // and rates are tunable uniforms; animation is driven purely by uTime.
  const SAND = new THREE.Color().setStyle('rgb(200,170,120)');
  const stormMat = new THREE.ShaderMaterial({
    vertexShader: STORM_VERT, fragmentShader: STORM_FRAG,
    transparent: true, depthWrite: false, blending: THREE.NormalBlending,
    uniforms: {
      uTime: { value: 0 }, uReduce: { value: reduce },
      uIntensity: { value: 1 },
      uFps: { value: 14 },            // flipbook churn rate (12..18)
      uTile: { value: 3.0 },          // sweep-mode puff repeats across the board
      uSweep: { value: 0.05 },        // ~20s per crossing
      uLead: { value: 0.09 },         // short leading glow falloff
      uTrail: { value: 0.34 },        // long trailing dust tail
      uOpacity: { value: 0.5 },       // semi-transparent haze
      uColor: { value: SAND.clone() },
      uFlashColor: { value: new THREE.Color().setStyle('rgb(180,205,255)') },  // lightning tint
      uMode: { value: 0 },            // 0 = sweep fallback, 1 = positioned dome
      uCenter: { value: new THREE.Vector2(0.5, 0.5) },   // UV space (board/VIEW)
      uRadius: { value: 0.15 },       // UV space
      uDir: { value: new THREE.Vector2(1, 0) },          // heading (cos yaw, sin yaw)
      uDrift: { value: 0.03 },        // internal dust drift speed along heading
      uStorm: { value: null }, uHasTex: { value: 0 },
    },
  });

  // Load the spice sprites once (async, same-origin static under the SvelteKit
  // base). On resolve, flip uHasTex so the shader samples the sprite alpha; on
  // error the procedural soft-dot fallback stays, so a failed fetch never
  // throws. Textures are disposed on teardown.
  const texLoader = new THREE.TextureLoader();
  let cloudTex = null, motesTex = null, stormTex = null;
  function loadSprite(name, mat, key, assign) {
    texLoader.load(assetBase + '/img/v2/' + name,
      function (tex) {
        if (tex.colorSpace !== undefined) tex.colorSpace = THREE.SRGBColorSpace;
        tex.needsUpdate = true;
        assign(tex);
        mat.uniforms[key].value = tex;
        mat.uniforms.uHasTex.value = 1;
      },
      undefined,
      function () { /* keep procedural fallback; never error */ });
  }
  loadSprite('spice-cloud.png', plumeMat, 'uCloud', function (t) { cloudTex = t; });
  loadSprite('spice-motes.png', motesMat, 'uMotes', function (t) { motesTex = t; });
  loadSprite('storm-flipbook.png', stormMat, 'uStorm', function (t) { stormTex = t; });

  // ---- instanced meshes (each gets its own geometry so custom instanced
  // attributes never collide across meshes that share a material) -------------
  function instColumn(max) {
    const g = columnGeo(THREE);
    g.setAttribute('aPulse',
      new THREE.InstancedBufferAttribute(new Float32Array(max), 1));
    const m = new THREE.InstancedMesh(g, columnMat, max);
    m.setColorAt(0, C.set(0, 0, 0));   // allocate instanceColor
    m.count = 0;
    m.frustumCulled = false;
    return m;
  }
  const spiceMesh = instColumn(SPICE_MAX);
  const pinMesh = instColumn(PIN_MAX);

  const blipMesh = (function () {
    const m = new THREE.InstancedMesh(discGeo(THREE), blipMat, WORM_MAX);
    m.setColorAt(0, C.set(0, 0, 0));
    m.count = 0; m.frustumCulled = false;
    return m;
  })();
  const rippleMesh = (function () {
    const g = discGeo(THREE);
    g.setAttribute('aPhase',
      new THREE.InstancedBufferAttribute(new Float32Array(WORM_MAX), 1));
    const m = new THREE.InstancedMesh(g, rippleMat, WORM_MAX);
    m.setColorAt(0, C.set(0, 0, 0));
    m.count = 0; m.frustumCulled = false;
    return m;
  })();

  const ringMesh = new THREE.InstancedMesh(
    ringGeo(THREE, half * 0.82, half * 0.99, 96), ringMat, 1);
  ringMesh.setMatrixAt(0, M4.identity());
  ringMesh.instanceMatrix.needsUpdate = true;
  ringMesh.visible = false;
  ringMesh.frustumCulled = false;

  const plumePoints = new THREE.Points(plumeGeo(THREE, PLUME_COUNT), plumeMat);
  plumePoints.visible = false;
  plumePoints.frustumCulled = false;
  const motesPoints = new THREE.Points(plumeGeo(THREE, MOTES_COUNT), motesMat);
  motesPoints.visible = false;
  motesPoints.frustumCulled = false;

  const stormMesh = new THREE.Mesh(
    stormGeo(THREE, half, heightAt, view * 0.015, 24), stormMat);
  stormMesh.visible = false;
  stormMesh.frustumCulled = false;

  let markerPoints = null;   // rebuilt on data/hidden change

  root.add(spiceMesh, pinMesh, rippleMesh, blipMesh, ringMesh, plumePoints, motesPoints, stormMesh);

  const reckoner = createReckonerLazy();
  function createReckonerLazy() {
    // reckon.js is a shared read-only import; index passes it in via opts to
    // avoid a static import here, keeping the three-free boundary clean.
    return opts.reckon.createReckoner({ reducedMotion: !!opts.reducedMotion });
  }

  const wormSlots = {};   // id -> { slot, blipR, rippleR }
  let lastMarkersRef = null;
  let lastHiddenSig = '';
  let lastScene = null;

  function worldSet(mesh, slot, nx, ny, sx, sy, sz) {
    M4.makeScale(sx, sy, sz);
    M4.setPosition(nx - half, heightAt(nx, ny), ny - half);
    mesh.setMatrixAt(slot, M4);
  }

  // Melange purple, sourced from the shared category color so the board can
  // never drift from the LayerTree legend (owner M3.1). Falls back through the
  // legend category color, then a purple constant. States differ by INTENSITY,
  // not hue. Ibad blue stays reserved for the me-pip.
  function spiceColor(scene) {
    let hex = scene.catColors && scene.catColors.spice;
    if (!hex && scene.legend) {
      for (let i = 0; i < scene.legend.length; i++) {
        const c = scene.legend[i];
        if (c && (c.key === 'spice' || /spice/i.test(c.label || ''))) { hex = c.color; break; }
      }
    }
    return SPICE_C.set(hex || '#a24bd4');
  }

  // ---- spice: purple columns/pips (intensity by state) + eruption plume ------
  function updateSpice(scene) {
    const hidden = scene.hidden || {};
    const specs = [];
    const dimKey = String(scene.dim);
    const purple = spiceColor(scene);
    let blow = null;

    if (!hidden['spice_large']) {
      const cand = scene.candidates || {};
      Object.keys(cand).forEach(function (sec) {
        const xy = cand[sec];
        if (xy && xy.length === 2) {
          // large-field candidates: dim purple, a touch wider than mediums
          specs.push({ nx: xy[0], ny: xy[1], color: purple,
            mul: 0.5, h: view * 0.07, pulse: 0, w: 1.5 });
        }
      });
      const sp = (scene.spiceActive && scene.spiceActive.dimensions) || {};
      const info = sp[dimKey];
      if (info && info.large_active) {
        const fields = (info.ram_active_fields && info.ram_active_fields.length)
          ? info.ram_active_fields
          : [{ nx: info.ram_nx, ny: info.ram_ny }];
        fields.forEach(function (f) {
          if (f.nx == null || f.ny == null) return;
          // active blow: hot/bright purple, near-white core via additive boost;
          // widest of the spice columns so it reads as the hero large field
          specs.push({ nx: f.nx, ny: f.ny, color: purple,
            mul: 1.7, h: view * 0.12, pulse: 1, w: 1.6 });
          if (!blow) blow = f;
        });
      }
    }
    if (!hidden['spice_medium']) {
      (scene.mediums || []).forEach(function (md) {
        if (!md || md.length < 2) return;
        const active = md[3] === true;
        // mediums: mid purple (erupted a touch brighter)
        specs.push({ nx: md[0], ny: md[1], color: purple,
          mul: active ? 0.95 : 0.7, h: view * 0.035, pulse: 0 });
      });
    }
    fillColumns(spiceMesh, specs, view * 0.006);
    updatePlume(blow, purple);
  }

  // Plume rides the primary active blow: amber/rust dust body with the live
  // spice purple as the venting energy core. Both layers are hidden entirely
  // when no blow is active. No per-frame allocation (uniform colors copied in).
  function updatePlume(blow, purple) {
    if (!blow) { plumePoints.visible = false; motesPoints.visible = false; return; }
    plumePoints.visible = true;
    motesPoints.visible = true;
    plumePoints.position.set(
      blow.nx - half, heightAt(blow.nx, blow.ny), blow.ny - half);
    motesPoints.position.copy(plumePoints.position);
    plumeMat.uniforms.uCore.value.copy(purple);
    motesMat.uniforms.uCore.value.copy(purple);
  }

  function fillColumns(mesh, specs, wWidth) {
    const n = Math.min(specs.length, mesh.instanceMatrix.count);
    const pulse = mesh.geometry.attributes.aPulse;
    for (let i = 0; i < n; i++) {
      const s = specs[i];
      const w = wWidth * (s.w || 1);   // per-spec width multiplier (large > medium)
      worldSet(mesh, i, s.nx, s.ny, w, s.h, w);
      mesh.setColorAt(i, C.copy(s.color).multiplyScalar(s.mul));
      pulse.array[i] = s.pulse;
    }
    mesh.count = n;
    mesh.instanceMatrix.needsUpdate = true;
    mesh.instanceColor.needsUpdate = true;
    pulse.needsUpdate = true;
  }

  // ---- worms: sonar blip + expanding ripple, dead-reckoned ------------------
  function updateWorms(scene) {
    const hidden = scene.hidden || {};
    const list = hidden['worms'] ? [] : (scene.worms || []);
    const seen = {};
    const phase = rippleMesh.geometry.attributes.aPhase;
    let slot = 0;
    list.forEach(function (w) {
      if (w.nx == null || w.ny == null || slot >= WORM_MAX) return;
      const id = (w.id != null) ? String(w.id) : ('p' + w.nx + '_' + w.ny);
      seen[id] = true;
      const threat = w.threat || (w.surfaced ? 'surfaced' : 'submerged');
      const size = THREAT_SIZE[threat] || 1;
      const age = w.age_s || 0;
      const op = age <= 60 ? 1 : age >= 360 ? 0.45 : 1 - (age - 60) / 545;
      const blipR = view * 0.012 * size;
      const rippleR = view * 0.05 * size;
      const rec = wormSlots[id] || (wormSlots[id] = {});
      const isNew = rec.slot == null;
      rec.slot = slot; rec.blipR = blipR; rec.rippleR = rippleR;

      const col = C.copy(threatColor[threat] || threatColor.surfaced).multiplyScalar(op);
      blipMesh.setColorAt(slot, col);
      rippleMesh.setColorAt(slot, col);
      phase.array[slot] = ((w.id || slot) * 0.37) % 1;

      reckoner.lerpFix('worm:' + id, w.nx, w.ny, {
        intervalMs: WORM_GLIDE_MS, snap: isNew,
        apply: makeWormApply(id),
      });
      slot++;
    });
    // Drop worms no longer in the feed.
    Object.keys(wormSlots).forEach(function (id) {
      if (!seen[id]) { reckoner.remove('worm:' + id); delete wormSlots[id]; }
    });
    blipMesh.count = slot;
    rippleMesh.count = slot;
    blipMesh.instanceColor.needsUpdate = true;
    rippleMesh.instanceColor.needsUpdate = true;
    phase.needsUpdate = true;
  }

  function makeWormApply(id) {
    return function (x, y) {
      const rec = wormSlots[id];
      if (!rec || rec.slot == null) return;
      worldSet(blipMesh, rec.slot, x, y, rec.blipR, 1, rec.blipR);
      worldSet(rippleMesh, rec.slot, x, y, rec.rippleR, 1, rec.rippleR);
      blipMesh.instanceMatrix.needsUpdate = true;
      rippleMesh.instanceMatrix.needsUpdate = true;
    };
  }

  // ---- me pin, bases, vehicles, waypoints (all amber, me Ibad while fresh) ---
  function updatePins(scene) {
    const specs = [];
    const me = scene.me;
    if (scene.meAuthed && me && scene.meShow) {
      const fresh = scene.meFresh !== false;
      const s = me.self;
      if (scene.meShow.self && s && s.nx != null && instVisible(s, scene.dim, scene.part)) {
        specs.push({ nx: s.nx, ny: s.ny,
          color: fresh ? tokens.ibad : tokens.accent,
          mul: 1.0, h: view * 0.065, pulse: fresh ? 1 : 0 });
      }
      if (scene.meShow.bases) {
        (me.bases || []).forEach(function (b) {
          if (b.nx == null || !instVisible(b, scene.dim, scene.part)) return;
          specs.push({ nx: b.nx, ny: b.ny, color: tokens.accent,
            mul: 0.8, h: view * 0.045, pulse: 0 });
        });
      }
      if (scene.meShow.vehicles) {
        (me.vehicles || []).forEach(function (v) {
          if (v.nx == null || !instVisible(v, scene.dim, scene.part)) return;
          specs.push({ nx: v.nx, ny: v.ny, color: tokens.accent,
            mul: 0.7, h: view * 0.04, pulse: 0 });
        });
      }
    }
    (scene.waypoints || []).forEach(function (wp) {
      if (wp.nx == null) return;
      specs.push({ nx: wp.nx, ny: wp.ny, color: tokens.accentBright,
        mul: 1.0, h: view * 0.05, pulse: 0 });
    });
    // Live OTHER players + world vehicles (Hagga): short, non-pulsing dots for
    // the crowd, filtered to the selected sietch by partition. Pushed AFTER the
    // me/waypoint specs so those never lose a slot when the board is busy. World
    // coords are projected to normalized here (they arrive raw from the feed).
    const so = scene.showOthers || { players: true, vehicles: true };
    if (so.vehicles) {
      (scene.otherVehicles || []).forEach(function (v) {
        if (!partHere(v.p, scene.part)) return;
        const n = worldToNorm(v.x, v.y, scene.cal, view);
        if (!n) return;
        specs.push({ nx: n.nx, ny: n.ny, color: tokens.accent,
          mul: 0.5, h: view * 0.022, pulse: 0 });
      });
    }
    if (so.players) {
      (scene.otherPlayers || []).forEach(function (pl) {
        if (!partHere(pl.p, scene.part)) return;
        const n = worldToNorm(pl.x, pl.y, scene.cal, view);
        if (!n) return;
        specs.push({ nx: n.nx, ny: n.ny, color: tokens.ibad,
          mul: 0.55, h: view * 0.03, pulse: 0 });
      });
    }
    fillColumns(pinMesh, specs, view * 0.007);
  }

  // Position/size/orient the dome from the live read. Center + radius are board
  // coords 0..VIEW mapped to UV (=board/VIEW); heading (cos yaw, sin yaw) matches
  // V1 drawStorm (+x=East, +y=South). Sign VERIFIED 2026-07-04 against a live
  // sweep: measured drift bearing 34.47 deg vs heading_yaw 34.5 (I1->B9 crossing,
  // samples in ops/storm-heading-verify/). Uniform vectors are set in place so
  // there is no per-frame allocation.
  function setStormPositioned(cx, cy, r, yaw) {
    const u = stormMat.uniforms;
    u.uCenter.value.set(cx / view, cy / view);
    const rr = (isFiniteNum(r) && r > 0) ? r : view * 0.15;
    u.uRadius.value = rr / view;
    const rad = (isFiniteNum(yaw) ? yaw : 0) * Math.PI / 180;
    u.uDir.value.set(Math.cos(rad), Math.sin(rad));
  }

  // ---- storm: rim glow accent + positioned dome (live) / sweep (fallback) ----
  function updateStorm(scene) {
    const hidden = scene.hidden || {};
    const info = ((scene.sandstorm && scene.sandstorm.dimensions) || {})[String(scene.dim)];
    // Rim glow retained as a subtle edge accent (existing behavior).
    const present = !!(info && (info.storm_sector || info.storm_scanned_utc || info.last_spawn_utc));
    ringMesh.visible = present && !hidden['sandstorm'];

    const toggledOn = hidden['sandstorm'] !== true;
    const force = STORM_FORCE || (scene && scene.sandstormForce === true);
    let show = false, mode = 0, intensity = 1;

    if (toggledOn) {
      const cx = info && info.center_nx, cy = info && info.center_ny;
      const hasCenter = isFiniteNum(cx) && isFiniteNum(cy);
      if (hasCenter) {
        // Live positioned storm. Fade with freshness; force overrides the gate.
        const fresh = stormLocFresh(info);
        if (fresh > 0 || force) {
          show = true; mode = 1;
          intensity = force ? Math.max(fresh, 1) : fresh;
          setStormPositioned(cx, cy, info.radius_nr, info.heading_yaw);
        } else {
          // Center present but the scan went stale mid-storm: drop to the
          // timing sweep rather than hiding an active storm.
          const act = stormActivity(info);
          if (act) { show = true; mode = 0; intensity = act.intensity; }
        }
      } else if (force) {
        // No live center: eyeball the positioned look at board center.
        show = true; mode = 1; intensity = 1;
        setStormPositioned(view * 0.5, view * 0.5, view * 0.18,
          info && isFiniteNum(info.heading_yaw) ? info.heading_yaw : 0);
      } else {
        // Timing-only fallback: desert-wide sweep.
        const act = stormActivity(info);
        if (act) { show = true; mode = 0; intensity = act.intensity; }
      }
    }

    stormMesh.visible = show;
    if (show) {
      stormMat.uniforms.uMode.value = mode;
      stormMat.uniforms.uIntensity.value = intensity;
    }
  }

  // ---- markers: one THREE.Points, rebuilt only on data/hidden change --------
  function updateMarkers(scene) {
    const sig = hiddenSig(scene.hidden);
    if (scene.markers === lastMarkersRef && sig === lastHiddenSig && markerPoints) return;
    lastMarkersRef = scene.markers;
    lastHiddenSig = sig;

    const markers = scene.markers || [];
    const catIndex = scene.catIndex || {};
    const hidden = scene.hidden || {};
    const catColorCache = {};
    function catColor(cat) {
      let c = catColorCache[cat];
      if (!c) c = catColorCache[cat] = new THREE.Color(
        (scene.catColors && scene.catColors[cat]) || '#9a8a6a');
      return c;
    }

    // Emphasized ore types resolved by NAME (never a hardcoded index): all
    // Titanium/Stravidium variants (TitaniumOre, TitaniumPickup, ...) collapse
    // to the emphasis flag, matching how they collapse into one legend row.
    const typeIndex = scene.typeIndex || [];
    const hotType = {};
    for (let i = 0; i < typeIndex.length; i++) {
      const nm = typeIndex[i];
      if (nm && /titanium|stravidium/i.test(String(nm))) hotType[i] = true;
    }

    const px = [], cx = [], sz = [];
    const lift = view * 0.004;
    for (let i = 0; i < markers.length; i++) {
      const m = markers[i];
      const typeIdx = m[3];
      if (typeof typeIdx === 'number' && hidden[typeIdx]) continue;
      const cat = catIndex[m[2]] || 'other';
      if (hidden[cat] === true) continue;
      px.push(m[0] - half, heightAt(m[0], m[1]) + lift, m[1] - half);
      const col = catColor(cat);
      cx.push(col.r, col.g, col.b);
      sz.push((typeof typeIdx === 'number' && hotType[typeIdx]) ? ORE_EMPHASIS : 1.0);
    }

    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(px), 3));
    g.setAttribute('aColor', new THREE.BufferAttribute(new Float32Array(cx), 3));
    g.setAttribute('aSize', new THREE.BufferAttribute(new Float32Array(sz), 1));
    if (markerPoints) {
      root.remove(markerPoints);
      markerPoints.geometry.dispose();
    } else {
      markerPoints = new THREE.Points(g, pointsMat);
      markerPoints.frustumCulled = false;
    }
    markerPoints.geometry = g;
    root.add(markerPoints);
  }

  function hiddenSig(hidden) {
    if (!hidden) return '';
    return Object.keys(hidden).filter(function (k) { return hidden[k]; }).sort().join(',');
  }

  // ---- public surface -------------------------------------------------------
  function applyScene(scene) {
    lastScene = scene;
    updateMarkers(scene);
    updateSpice(scene);
    updateWorms(scene);
    updatePins(scene);
    updateStorm(scene);
  }

  function setTime(sec) {
    columnMat.uniforms.uTime.value = sec;
    rippleMat.uniforms.uTime.value = sec;
    ringMat.uniforms.uTime.value = sec;
    plumeMat.uniforms.uTime.value = sec;
    motesMat.uniforms.uTime.value = sec;
    stormMat.uniforms.uTime.value = sec;
  }

  // Engine zoom -> marker size boost (curve kicks in at close range only).
  function setView(v) {
    const z = (v && v.zoom) ? v.zoom : 1;
    pointsMat.uniforms.uZoom.value = Math.max(1, Math.min(8, z));
  }

  function setTokens(next) {
    tokens = next;
    ringMat.uniforms.uColor.value.copy(next.accent);
    // Recolor token-driven layers; worms/markers keep their fixed/data colors.
    if (lastScene) { updateSpice(lastScene); updatePins(lastScene); }
  }

  function dispose() {
    reckoner.destroy();
    root.remove(spiceMesh, pinMesh, rippleMesh, blipMesh, ringMesh, plumePoints, motesPoints, stormMesh);
    if (markerPoints) { root.remove(markerPoints); markerPoints.geometry.dispose(); }
    plumePoints.geometry.dispose();
    motesPoints.geometry.dispose();
    stormMesh.geometry.dispose();
    if (cloudTex) cloudTex.dispose();
    if (motesTex) motesTex.dispose();
    if (stormTex) stormTex.dispose();
    [spiceMesh, pinMesh, rippleMesh, blipMesh, ringMesh].forEach(function (m) {
      m.geometry.dispose();
      if (m.dispose) m.dispose();   // frees instanceMatrix/instanceColor
    });
    [columnMat, blipMat, rippleMat, ringMat, pointsMat, plumeMat, motesMat, stormMat].forEach(function (m) {
      m.dispose();
    });
  }

  return { applyScene, setView, setTime, setTokens, dispose };
}
