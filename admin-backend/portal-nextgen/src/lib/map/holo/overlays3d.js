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

import { ATLAS_URL, rectFor, lodClass, iconFraction, MID_ZOOM_START, MID_ZOOM_FULL, rankFor, drawRank } from './icon-atlas.js';
import { clusterLevel, buildClusters, discPx, badgeGlyphs, zoomForBounds, isClusterCat, markerVisible } from './cluster.js';
import { stormFootprint, positionAt, isOver } from '../stormFootprint.js';

const WORM_GLIDE_MS = 10000;   // = /live cadence, matches the carved path
// One cycle's full spice set: ~85 mediums + 5 Large candidates + 1-3 active
// Larges. 64 silently dropped the southern mediums (2.26.0 live bug).
const SPICE_MAX = 256;
const WORM_MAX = 32;
// Pin columns are shared by your me-layer, waypoints, AND the live other-player
// crowd (Hagga). Me/waypoints are pushed first so they always claim slots;
// others fill the rest up to this cap.
const PIN_MAX = 256;

// Sandstorm. During an active storm the RAM reader emits live POSITION
// (center_nx/ny, radius_nr, heading_yaw) -> positioned dome. Storm on the board
// = a live position only (2.26.1: the timing sweeps after a detected spawn or a
// passed ETA are gone); no position = nothing drawn, the HUD countdown carries
// "incoming".
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
    // solid threat-coloured disc with a dark rim, so it reads on bright day sand
    vec3 col = mix(vColor * 1.15, vec3(0.08, 0.04, 0.02), smoothstep(0.62, 0.78, d));
    gl_FragColor = vec4(col, smoothstep(1.0, 0.9, d) * 0.95);
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
  attribute vec4 aRect;                  // atlas UV rect; zero area = no icon, draw the dot
  attribute vec2 aLod;                   // x: 1 = dense resource node, y: stable rank 0..1
  attribute float aHide;                 // 1 = folded into a cluster icon this zoom level
  uniform float uSize; uniform float uMin; uniform float uMax;
  uniform float uZoom; uniform float uBoost; uniform float uZoomStart;
  uniform float uIconPx; uniform float uIconDense; uniform float uPR;
  uniform float uIconFrac; uniform float uIconFracMid; uniform float uHasAtlas; uniform float uPull;
  varying vec3 vColor; varying float vHot; varying vec4 vRect; varying float vIcon;
  void main() {
    if (aHide > 0.5) { gl_PointSize = 0.0; gl_Position = vec4(2.0, 2.0, 2.0, 1.0); return; }
    vColor = aColor; vHot = aSize; vRect = aRect;
    // Zoom-driven size: the lean framing keeps most of the board far from the
    // camera, so raw dolly attenuation barely grew apparent size on zoom-in.
    // Drive size explicitly from the engine zoom (uZoom, fed via setView): a
    // curve that stays 1x until close range then ramps to uBoost, so zoom-out
    // is unchanged and full zoom-in reaches ~uBoost x. Clamp px so 17k points
    // never become soup zoomed out. Emphasis multiplier applies before clamp so
    // Titanium/Stravidium can exceed the generic cap.
    float boost = mix(1.0, uBoost, smoothstep(uZoomStart, 8.0, uZoom));
    float sz = clamp(uSize * boost * aSize, uMin, uMax);
    // Icon or dot. POIs (aLod.x = 0) always take their icon; resource nodes
    // take it once their rank falls under the zoom-driven fraction.
    bool hasIcon = uHasAtlas > 0.5 && (aRect.z - aRect.x) > 0.0;
    float frac = aLod.x < 0.5 ? 2.0 : (aLod.x < 1.5 ? uIconFrac : uIconFracMid);
    bool icon = hasIcon && aLod.y < frac;
    vIcon = icon ? 1.0 : 0.0;
    if (icon) {
      float k = aLod.x > 0.5 ? uIconDense : 1.0;   // POIs are the largest
      sz = uIconPx * uPR * k * mix(1.0, 1.7, smoothstep(1.0, 8.0, uZoom))
         * (aSize > 1.2 ? 1.2 : 1.0);
    }
    gl_PointSize = sz;
    // The anchor sits ON the relief (no lift, so no parallax when the camera
    // tilts or pans). Slide it toward the camera along its own view ray for the
    // depth test only: the screen position is unchanged, but the ground just in
    // front of the point no longer bites the lower half of the sprite.
    vec4 mv = viewMatrix * modelMatrix * vec4(position, 1.0);
    mv.xyz -= normalize(mv.xyz) * min(uPull, -mv.z * 0.5);
    gl_Position = projectionMatrix * mv;
  }
`;
const POINTS_FRAG = `
  precision highp float;
  uniform sampler2D uAtlas;
  varying vec3 vColor; varying float vHot; varying vec4 vRect; varying float vIcon;
  vec4 cell(vec2 p) {
    // p in 0..1 across the cell (y down, like gl_PointCoord); clamp inside the
    // cell so the outline taps never read a neighbour.
    vec2 q = clamp(p, 0.02, 0.98);
    vec2 uv = vec2(mix(vRect.x, vRect.z, q.x), mix(vRect.w, vRect.y, q.y));
    return texture2D(uAtlas, uv);
  }
  void main() {
    if (vIcon > 0.5) {
      vec2 p = gl_PointCoord;
      vec4 c = cell(p);
      // Dark halo: the icon's own alpha dilated ~1px, so it reads over bright
      // and busy drape art without a flat backing plate.
      float r = 0.055;
      float h = 0.0;
      h = max(h, cell(p + vec2( r, 0.0)).a); h = max(h, cell(p + vec2(-r, 0.0)).a);
      h = max(h, cell(p + vec2(0.0,  r)).a); h = max(h, cell(p + vec2(0.0, -r)).a);
      h = max(h, cell(p + vec2( r,  r) * 0.7).a); h = max(h, cell(p + vec2(-r,  r) * 0.7).a);
      h = max(h, cell(p + vec2( r, -r) * 0.7).a); h = max(h, cell(p + vec2(-r, -r) * 0.7).a);
      vec3 rim = vec3(0.07, 0.045, 0.02);
      float a = max(c.a, h * 0.8);
      if (a < 0.02) discard;
      vec3 col = mix(rim, c.rgb, c.a);
      gl_FragColor = vec4(col, a);
      return;
    }
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

// Cluster icon: the type's in-game icon on a dark disc ringed in its category colour.
// Disc size (device px) arrives per point so it can scale with the count.
const CLUSTER_VERT = `
  attribute vec3 aColor; attribute vec4 aRect; attribute float aPx;
  varying vec3 vColor; varying vec4 vRect;
  void main() {
    vColor = aColor; vRect = aRect;
    gl_PointSize = aPx;
    gl_Position = projectionMatrix * viewMatrix * modelMatrix * vec4(position, 1.0);
  }
`;
const CLUSTER_FRAG = `
  precision highp float;
  uniform sampler2D uAtlas; uniform float uHasAtlas; uniform float uNight;
  varying vec3 vColor; varying vec4 vRect;
  void main() {
    vec2 p = gl_PointCoord;
    float d = length(p - 0.5) * 2.0;
    if (d > 1.0) discard;
    float edge = smoothstep(1.0, 0.9, d);
    float ring = smoothstep(0.78, 0.86, d);
    vec3 plate = mix(vec3(0.11, 0.075, 0.04), vec3(0.05, 0.06, 0.1), uNight);
    vec3 col = mix(plate, vColor, ring);
    float a = mix(0.92, 1.0, ring) * edge;
    bool hasIcon = uHasAtlas > 0.5 && (vRect.z - vRect.x) > 0.0;
    if (d < 0.8) {
      if (hasIcon) {
        vec2 q = (p - 0.5) / 0.62 + 0.5;
        if (q.x > 0.02 && q.x < 0.98 && q.y > 0.02 && q.y < 0.98) {
          vec2 uv = vec2(mix(vRect.x, vRect.z, q.x), mix(vRect.w, vRect.y, q.y));
          vec4 c = texture2D(uAtlas, uv);
          col = mix(col, c.rgb, c.a);
        }
      } else {
        col = mix(col, vColor, smoothstep(0.34, 0.28, d));
      }
    }
    gl_FragColor = vec4(col, a);
  }
`;
// Count badge: a dark plate with digit glyphs from a small canvas atlas, top-right of its disc.
const BADGE_VERT = `
  attribute vec3 aColor; attribute vec4 aGlyph; attribute vec2 aLenOff;  // x: glyph count, y: disc radius (device px)
  uniform vec2 uRes; uniform float uPR;
  varying vec3 vColor; varying vec4 vGlyph; varying float vLen; varying float vS;
  void main() {
    vColor = aColor; vGlyph = aGlyph; vLen = aLenOff.x;
    float gw = 6.5 * uPR, gh = 10.0 * uPR;
    float w = aLenOff.x * gw + 5.0 * uPR, h = gh + 4.0 * uPR;
    float S = max(w, h);
    vS = S;
    gl_PointSize = S;
    vec4 pos = projectionMatrix * viewMatrix * modelMatrix * vec4(position, 1.0);
    float o = aLenOff.y * 0.72;
    pos.xy += vec2(o, o) * 2.0 / uRes * pos.w;
    gl_Position = pos;
  }
`;
const BADGE_FRAG = `
  precision highp float;
  uniform sampler2D uDigits; uniform float uPR;
  varying vec3 vColor; varying vec4 vGlyph; varying float vLen; varying float vS;
  float glyph(int i) { return i == 0 ? vGlyph.x : (i == 1 ? vGlyph.y : (i == 2 ? vGlyph.z : vGlyph.w)); }
  void main() {
    float gw = 6.5 * uPR, gh = 10.0 * uPR;
    vec2 px = (gl_PointCoord - 0.5) * vS;      // device px from the centre, y down
    float w = vLen * gw + 5.0 * uPR, h = gh + 4.0 * uPR;
    vec2 q = abs(px) - vec2(w, h) * 0.5 + 2.0 * uPR;
    float sd = length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - 2.0 * uPR;
    if (sd > 0.0) discard;
    float rim = smoothstep(-1.2 * uPR, -0.2 * uPR, sd);
    vec3 col = mix(vec3(0.09, 0.06, 0.03), vColor, rim);
    float gx = (px.x + vLen * gw * 0.5) / gw;
    int gi = int(floor(gx));
    if (gx >= 0.0 && gx < vLen && abs(px.y) < gh * 0.5) {
      float code = glyph(gi);
      vec2 uv = vec2((code + fract(gx)) / 12.0, 1.0 - (px.y + gh * 0.5) / gh);
      float t = texture2D(uDigits, uv).a;
      col = mix(col, vec3(1.0, 0.93, 0.78), t);
    }
    gl_FragColor = vec4(col, 1.0);
  }
`;

// Spice blows: one particle system draws every eruption on the board, the
// active Large (hero) and each erupting medium (same effect at aScale ~0.4), so
// 19 medium blows cost no extra draw calls. Each blow owns a fixed slice of the
// preallocated buffers; per-slot attributes carry its centre (on the relief)
// and scale, and the shape animates purely on uTime (no per-frame allocation).
// Look: the in-game Large blow from the owner's screenshots (2026-05-29 /
// 06-04, ~/dd-re/spice-fix/refs): a smoky grey-violet stem billowing into a
// lilac mushroom cap with a slight curl and a second lobe, a bright lavender
// core glowing through the smoke, spice glints, a purple patch at the base.
// Two layers share PLUME_VERT:
//   1. SMOKE (alpha-over, spice-cloud sprite), dimmed at night (uDim).
//   2. GLOW (additive, spice-motes sprite): the lavender core in the cap, the
//      vent glow and glints; it stays lit at night.
// Sprite alpha gives the soft shape, with a procedural dot until the texture
// resolves. Point size is clamped in px (never sub-pixel). Reduced motion
// freezes every particle at its seed phase: a still mushroom.
const PLUME_VERT = `
  attribute float aSeed; attribute float aSpeed; attribute vec3 aCenter; attribute float aScale;
  uniform float uTime; uniform float uReduce; uniform float uHeight;
  uniform float uStem; uniform float uCap;
  uniform float uPSize; uniform float uView; uniform float uPMin; uniform float uPMax;
  varying float vPh; varying float vRad; varying float vRise; varying float vGlint; varying float vCore;
  void main() {
    float ph = (uReduce > 0.5) ? aSeed : fract(uTime * aSpeed * 0.1 / sqrt(max(aScale, 0.2)) + aSeed);
    float cap = smoothstep(0.58, 0.84, ph) * (1.0 - 0.4 * smoothstep(0.9, 1.0, ph));
    float d = (ph - 0.46) / 0.09;
    float lobe = 0.38 * exp(-d * d);
    float skirt = 1.0 - smoothstep(0.0, 0.1, ph);          // ground-level dust ring
    float radius = (uStem * (1.0 + 1.1 * skirt) + uCap * (cap + lobe)) * position.y * aScale;
    float wob = (uReduce > 0.5) ? 0.0 : sin(uTime * 0.7 + aSeed * 23.0) * 0.12;
    float ang = atan(position.z, position.x) + ph * 1.7 + wob;   // slow swirl as it rises
    vec3 p = aCenter;
    p.x += cos(ang) * radius;
    p.z += sin(ang) * radius;
    p.y += ph * uHeight * aScale;
    vPh = ph;
    vRad = position.y;                                    // 0.4..1.0 radial jitter
    vRise = smoothstep(0.0, 0.04, ph) * (1.0 - smoothstep(0.8, 1.0, ph) * 0.85);
    vCore = smoothstep(0.58, 0.76, ph) * (1.0 - smoothstep(0.9, 1.0, ph)) * (1.0 - smoothstep(0.4, 0.75, position.y));
    float isGlint = step(0.8, fract(aSeed * 7.13));
    float tw = (uReduce > 0.5) ? 0.6 : (0.5 + 0.5 * sin(uTime * 6.0 + aSeed * 40.0));
    vGlint = isGlint * tw;
    vec4 mv = viewMatrix * modelMatrix * vec4(p, 1.0);
    float sz = clamp(uPSize * (0.45 + 0.55 * aScale) * uView / max(-mv.z, 1.0), uPMin, uPMax)
      * (0.8 + 0.55 * cap + 0.4 * skirt);
    gl_PointSize = sz;
    gl_Position = projectionMatrix * mv;
  }
`;
// Smoke body: grey-violet stem lightening to a lilac cap, outer puffs a touch
// paler; the core glow bleeds into the inner cap puffs. uDim darkens it at night.
const PLUME_FRAG = `
  precision highp float;
  uniform sampler2D uCloud; uniform float uHasTex;
  uniform vec3 uStemCol; uniform vec3 uCapCol; uniform vec3 uCore;
  uniform float uOpacity; uniform float uDim;
  varying float vPh; varying float vRad; varying float vRise; varying float vGlint; varying float vCore;
  void main() {
    float d = length(gl_PointCoord - 0.5) * 2.0;
    float soft = smoothstep(1.0, 0.0, d);
    float shape = (uHasTex > 0.5) ? texture2D(uCloud, gl_PointCoord).a : soft;
    if (shape < 0.02) discard;
    vec3 dust = mix(uStemCol, uCapCol, smoothstep(0.15, 0.7, vPh));
    dust = mix(dust, uCapCol * 1.15, (vRad - 0.4) * 0.4);
    vec3 col = mix(dust * uDim, uCore, min(vCore * 0.9, 0.8));
    gl_FragColor = vec4(col, shape * vRise * uOpacity);
  }
`;
// Glow: additive. The lavender core in the cap (a broad soft glow), the vent
// glow at the base, and pale spice glints rising through the smoke.
const MOTES_FRAG = `
  precision highp float;
  uniform sampler2D uMotes; uniform float uHasTex;
  uniform vec3 uCore; uniform vec3 uGlint; uniform float uOpacity;
  varying float vPh; varying float vRad; varying float vRise; varying float vGlint; varying float vCore;
  void main() {
    float d = length(gl_PointCoord - 0.5) * 2.0;
    float soft = smoothstep(1.0, 0.0, d);
    float shape = (uHasTex > 0.5) ? texture2D(uMotes, gl_PointCoord).r : soft;
    float vent = 1.0 - smoothstep(0.0, 0.18, vPh);
    float accent = soft * (vent * 0.55 + vCore * vCore * 0.42);
    float glint = shape * vGlint * 0.9 * (1.0 - vent);
    if (accent + glint < 0.01) discard;
    vec3 col = (uCore * accent + uGlint * glint) / max(accent + glint, 0.001);
    gl_FragColor = vec4(col, (accent + glint) * vRise * uOpacity);
  }
`;

// Spice field pips: screen-space diamonds (the 2D board's glyph) on every field,
// drawn after the relief AND the cluster discs with no depth test, so a field is
// never buried in a dune or swallowed by a cluster at any zoom or light. A dark
// ink rim keeps them legible on bright day sand; states differ by fill:
// aKind 0 = Large candidate (hollow), 1 = medium, 2 = erupting medium (brighter,
// white core, breathes), 3 = active Large (hero size, breathes).
const PIP_VERT = `
  attribute vec3 aColor; attribute float aPx; attribute float aKind;
  uniform float uPR; uniform float uZoom;
  varying vec3 vColor; varying float vKind;
  void main() {
    vColor = aColor; vKind = aKind;
    gl_PointSize = aPx * uPR * min(1.0 + 0.1 * (uZoom - 1.0), 1.6);
    gl_Position = projectionMatrix * viewMatrix * modelMatrix * vec4(position, 1.0);
  }
`;
const PIP_FRAG = `
  precision highp float;
  uniform float uTime; uniform float uReduce;
  varying vec3 vColor; varying float vKind;
  void main() {
    vec2 p = gl_PointCoord * 2.0 - 1.0;
    float d = abs(p.x) + abs(p.y);
    if (d > 1.0) discard;
    float breathe = (uReduce > 0.5 || vKind < 1.5) ? 1.0 : 1.0 + 0.22 * sin(uTime * 2.4);
    vec3 ink = vec3(0.07, 0.03, 0.08);
    vec3 fill = vColor * breathe;
    float inner = 1.0 - smoothstep(0.66, 0.74, d);       // 1 inside the colored body
    float a = 0.92;
    if (vKind < 0.5) {
      // candidate: hollow diamond, a colored band with a faint center
      float band = smoothstep(0.34, 0.42, d);
      fill = mix(vColor * 0.35, vColor, band);
      a = mix(0.55, 0.92, max(band, 1.0 - inner));
    } else if (vKind > 1.5) {
      fill = mix(fill, vec3(1.0, 0.94, 1.0), 1.0 - smoothstep(0.18, 0.34, d));
    }
    vec3 col = mix(ink, fill, inner);
    gl_FragColor = vec4(col, a * smoothstep(1.0, 0.93, d));
  }
`;

// Worm marker: a screen-space threat-coloured round pip (PIP_VERT) with a dark
// ink rim, over the relief and the cluster discs like the spice pips, so a
// worm reads at default zoom; the ground blip + ripple ring stay underneath.
const WORM_PIP_FRAG = `
  precision highp float;
  uniform float uTime; uniform float uReduce;
  varying vec3 vColor; varying float vKind;
  void main() {
    float d = length(gl_PointCoord - 0.5) * 2.0;
    if (d > 1.0) discard;
    float breathe = (uReduce > 0.5) ? 1.0 : 1.0 + 0.2 * sin(uTime * 3.0);
    vec3 col = mix(vColor * breathe, vec3(0.07, 0.03, 0.02), smoothstep(0.66, 0.76, d));
    col = mix(col, vec3(1.0, 0.95, 0.85), 1.0 - smoothstep(0.16, 0.28, d));
    gl_FragColor = vec4(col, smoothstep(1.0, 0.92, d) * 0.95);
  }
`;

// Spice field patch: a purple patch on the sand under every field, bubbling
// (owner spec 2026-10-07): a gentle shimmer plus small bubbles that swell and
// pop on a jittered grid, all in this one instanced draw. aAmt = opacity by
// state; aSeed decorrelates fields. Reduced motion freezes the bubbles.
const STAIN_VERT = `
  attribute float aAmt; attribute float aSeed;
  varying vec3 vColor; varying vec2 vUv; varying float vAmt; varying float vSeed;
  void main() {
    vColor = instanceColor; vUv = uv; vAmt = aAmt; vSeed = aSeed;
    gl_Position = projectionMatrix * viewMatrix * modelMatrix * instanceMatrix * vec4(position, 1.0);
  }
`;
const STAIN_FRAG = `
  precision highp float;
  uniform float uTime; uniform float uReduce;
  varying vec3 vColor; varying vec2 vUv; varying float vAmt; varying float vSeed;
  float h21(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7)) + vSeed * 17.0) * 43758.5453); }
  void main() {
    vec2 q = vUv - 0.5;
    float d = length(q) * 2.0;
    float ang = atan(q.y, q.x);
    float edge = 0.84 + 0.08 * sin(ang * 5.0 + vSeed * 31.0) + 0.05 * sin(ang * 11.0 + vSeed * 7.0);
    float body = 1.0 - smoothstep(edge - 0.2, edge, d);
    if (body < 0.01) discard;
    float t = (uReduce > 0.5) ? 0.0 : uTime;
    float shimmer = 0.88 + 0.12 * sin(t * 1.3 + d * 9.0 - vSeed * 6.0);
    // bubbles: one per cell of a 5x5 grid, swelling then popping
    vec2 g = vUv * 5.0;
    vec2 cell = floor(g);
    float h = h21(cell);
    float ph = (uReduce > 0.5) ? h : fract(t * (0.25 + 0.2 * h) + h);
    vec2 c = cell + 0.25 + 0.5 * vec2(h, fract(h * 13.7));
    float r = 0.08 + 0.3 * ph;
    float ring = (1.0 - smoothstep(0.0, 0.07, abs(length(g - c) - r))) * (1.0 - ph) * step(0.35, h);
    vec3 col = vColor * shimmer * (0.85 + 0.25 * (1.0 - d));
    col = mix(col, vec3(0.93, 0.8, 1.0), ring * 0.7);
    gl_FragColor = vec4(col, body * vAmt * (0.75 + 0.25 * smoothstep(0.0, 0.7, d)) + ring * 0.35 * body);
  }
`;

// Rising spice motes over the idle fields: a few lavender specks per field
// drift up and fade (shared Points, one draw; per-point field centre and
// radius). Reduced motion freezes them where they are.
const BUBBLE_VERT = `
  attribute vec3 aCenter; attribute float aSeed; attribute float aRad;
  uniform float uTime; uniform float uReduce; uniform float uRise; uniform float uPR; uniform float uZoom;
  varying float vA;
  void main() {
    float ph = (uReduce > 0.5) ? aSeed : fract(uTime * (0.18 + 0.12 * fract(aSeed * 5.3)) + aSeed);
    float a = aSeed * 43.0;
    vec3 p = aCenter + vec3(cos(a), 0.0, sin(a)) * aRad * (0.25 + 0.6 * fract(aSeed * 9.1));
    p.y += ph * uRise * (aRad / 10.0);
    vA = smoothstep(0.0, 0.15, ph) * (1.0 - smoothstep(0.6, 1.0, ph));
    gl_PointSize = uPR * clamp(2.0 + 0.8 * uZoom, 2.5, 7.0);
    gl_Position = projectionMatrix * viewMatrix * modelMatrix * vec4(p, 1.0);
  }
`;
const BUBBLE_FRAG = `
  precision highp float;
  uniform vec3 uColor;
  varying float vA;
  void main() {
    float d = length(gl_PointCoord - 0.5) * 2.0;
    if (d > 1.0 || vA < 0.01) discard;
    gl_FragColor = vec4(uColor, smoothstep(1.0, 0.2, d) * vA * 0.85);
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
  uniform vec3 uColor; uniform vec3 uFlashColor; uniform vec3 uEdgeColor; uniform vec3 uFootColor;
  uniform vec3 uDangerColor; uniform vec3 uGustColor; uniform float uFootAlpha;
  // FOOTPRINT (uMode 2): up to 4 zone ellipses in UV (centre, semi-axes, heading
  // cos/sin, damage flag), the route A->B and the live centre uPos.
  uniform vec2 uZoneC[4]; uniform vec2 uZoneAx[4]; uniform vec2 uZoneRot[4];
  uniform float uZoneDmg[4]; uniform float uZoneN;
  uniform vec2 uRouteA; uniform vec2 uRouteB; uniform float uHasRoute; uniform vec2 uPos;
  varying vec2 vUv;

  float hash11(float n) { return fract(sin(n * 127.1) * 43758.5453); }
  float hash21(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
  float vnoise2(vec2 p) {
    vec2 i = floor(p), f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash21(i), hash21(i + vec2(1.0, 0.0)), f.x),
               mix(hash21(i + vec2(0.0, 1.0)), hash21(i + vec2(1.0, 1.0)), f.x), f.y);
  }
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
    float edge = 0.0;   // footprint: crisp damage boundary
    float rim = 0.0;    // footprint: faint gust-front rim
    float route = 0.0;  // footprint: dashed route + centre dot
    float dmgBody = 0.0; float gustBody = 0.0; float swirl = 0.0;

    if (uMode > 1.5) {
      // FOOTPRINT: union of the damage ellipses = the storm body, its boundary
      // drawn as a crisp edge; the non-damage gust front as a faint rim + haze.
      float eD = 1e3; float eG = 1e3;
      for (int i = 0; i < 4; i++) {
        if (float(i) >= uZoneN) break;
        vec2 r = vUv - uZoneC[i];
        vec2 q = vec2(dot(r, uZoneRot[i]), dot(r, vec2(-uZoneRot[i].y, uZoneRot[i].x)));
        float e = length(q / max(uZoneAx[i], vec2(1e-4)));   // 1.0 on the ellipse
        if (uZoneDmg[i] > 0.5) eD = min(eD, e); else eG = min(eG, e);
      }
      float px = fwidth(eD);
      dmgBody = smoothstep(1.0, 0.9, eD);
      edge = smoothstep(px * 3.4, px * 1.2, abs(eD - 1.0));          // ~3 px danger line at any zoom
      float pg = fwidth(eG);
      rim = smoothstep(pg * 2.2, pg * 0.6, abs(eG - 1.0)) * step(1.0, eD);
      gustBody = smoothstep(1.0, 0.85, eG) * step(1.0, eD);
      body = max(dmgBody, gustBody);
      // Slow swirl: two octaves of value noise in a frame that turns about the storm
      // centre and drifts along the heading (static under reduced motion).
      float churnS = (uReduce > 0.5) ? 0.0 : 1.0;
      vec2 sr = (vUv - uPos) / max(uRadius, 1e-4);
      float ang = uTime * 0.05 * churnS + length(sr) * 1.4;
      vec2 sw = vec2(cos(ang) * sr.x - sin(ang) * sr.y, sin(ang) * sr.x + cos(ang) * sr.y);
      vec2 drift = uDir * (uTime * 0.04 * churnS);
      swirl = 0.65 * vnoise2(sw * 3.0 - drift * 3.0) + 0.35 * vnoise2(sw * 7.0 + drift * 5.0 + 3.1);
      glow = smoothstep(0.7, 1.0, eD) * step(eD, 1.0)
           * clamp(dot(normalize(vUv - uPos + 1e-5), uDir), 0.0, 1.0);
      if (uHasRoute > 0.5) {
        vec2 ab = uRouteB - uRouteA;
        float L = max(length(ab), 1e-4);
        float t = clamp(dot(vUv - uRouteA, ab) / (L * L), 0.0, 1.0);
        float d = length(vUv - uRouteA - ab * t);
        float dash = step(0.45, fract(t * L / 0.018));
        route = smoothstep(0.0022, 0.0008, d) * dash;
      }
      route = max(route, smoothstep(0.0055, 0.0035, length(vUv - uPos)));
      vec2 rel = vUv - uPos;
      vec2 local = vec2(dot(rel, uDir), dot(rel, vec2(-uDir.y, uDir.x))) / max(uRadius, 1e-4);
      buv = local * 0.5 + 0.5;
      driftDir = -vec2(1.0, 0.0);
    } else if (uMode > 0.5) {
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
    if (body < 0.01 && edge + rim + route < 0.01) discard;

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
    if (uMode > 1.5) {
      // Damage body: dense rust/ochre sand-dust with a slow swirl, translucent enough
      // that the terrain still reads. Gust front: a lighter dust haze ahead. Lines on
      // top: red-orange danger edge on the damage boundary, pale gust rim, sand route.
      float grain = 0.55 + 0.45 * mix(swirl, dens, 0.35);
      vec3 dust = uFootColor * (0.8 + 0.45 * grain) * (1.0 + 0.25 * glow);
      float aD = dmgBody * uFootAlpha * (0.7 + 0.3 * grain);
      float aG = gustBody * uFootAlpha * 0.32 * (0.6 + 0.4 * swirl);
      tint = mix(uGustColor, dust, smoothstep(0.0, 0.35, dmgBody));
      a = max(aD, aG) * uIntensity;
      tint = mix(tint, uGustColor, clamp(rim * 0.9, 0.0, 1.0));
      a = max(a, rim * 0.7 * uIntensity);
      tint = mix(tint, uEdgeColor, clamp(route * 0.9, 0.0, 1.0));
      a = max(a, route * 0.75 * uIntensity);
      tint = mix(tint, uDangerColor, clamp(edge, 0.0, 1.0));
      a = max(a, edge * uIntensity);
    }

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
  // Blow palette sampled from the owner's in-game screenshots: grey-violet
  // stem, lilac cap; the core is the live spice color lifted toward white
  // (lands on the sampled #dcb7ec for the default purple). The field patches
  // are the live spice purple. setStyle keeps these in the same sRGB->linear
  // space as the CSS tokens.
  const STEM_COL = new THREE.Color().setStyle('#6c4d6c');
  const CAP_COL = new THREE.Color().setStyle('#a68eb2');
  const GLINT_COL = new THREE.Color().setStyle('#f6e4ff');
  const BUBBLE_COL = new THREE.Color().setStyle('#e2c6ff');
  const CORE_C = new THREE.Color();
  const PATCH_C = new THREE.Color();
  // Per-blow particle budgets. Low tier halves the medium blows' share.
  const LOW_TIER = opts.tier === 'low' || !!opts.reducedMotion;
  const BLOW_LARGE_MAX = 3;
  const BLOW_MED_MAX = 40;
  const LARGE_DUST = 520, LARGE_GLOW = 150;
  const MED_DUST = LOW_TIER ? 24 : 44, MED_GLOW = LOW_TIER ? 8 : 14;
  const BUBBLE_PER = LOW_TIER ? 2 : 5;
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
    // flat ground disc: discGeo winds clockwise seen from above (a front-side disc
    // is culled) and the relief would bite it; the worm reads over the dunes
    transparent: true, depthWrite: false, depthTest: false, side: THREE.DoubleSide,
    uniforms: {},
  });
  const rippleMat = new THREE.ShaderMaterial({
    vertexShader: RIPPLE_VERT, fragmentShader: RIPPLE_FRAG,
    // flat ground disc: discGeo winds clockwise seen from above (a front-side disc
    // is culled) and the relief would bite it; the worm reads over the dunes
    transparent: true, depthWrite: false, depthTest: false, side: THREE.DoubleSide, blending: THREE.AdditiveBlending,
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
      uPull: { value: view * 0.02 }, // depth-only slide toward the camera (POINTS_VERT)
      uMin: { value: 2.0 }, uMax: { value: 26.0 },
      uZoom: { value: 1 },          // engine zoom 1..8, driven by setView
      uBoost: { value: 2.7 },       // full zoom-in reaches ~2.7x zoom-out size
      uZoomStart: { value: 2.5 },   // boost only kicks in past mid-zoom
      uAtlas: { value: null }, uHasAtlas: { value: 0 },   // dots until the atlas resolves
      uIconPx: { value: 15 },       // POI icon edge at zoom-out, CSS px (x uPR); ~25 px zoomed in
      uIconDense: { value: 0.7 },  // resource-node icons are smaller than POI icons
      uPR: { value: Math.min(opts.pixelRatio || 1, 2) },
      uIconFrac: { value: 0 }, uIconFracMid: { value: 0 },      // share of resource nodes drawn as icons (zoom LOD)
    },
  });
  const PR = Math.min(opts.pixelRatio || 1, 2);
  const clusterMat = new THREE.ShaderMaterial({
    vertexShader: CLUSTER_VERT, fragmentShader: CLUSTER_FRAG,
    // no depth test: a 40 px disc is a screen-space glyph, a ridge in front must not bite a slice out of it
    transparent: true, depthWrite: false, depthTest: false,
    uniforms: { uAtlas: { value: null }, uHasAtlas: { value: 0 }, uNight: { value: 0 } },
  });
  // Digit glyphs 0-9, 'k', '.' in one 12-cell strip (white on clear; tinted in the shader).
  let digitTex = null;
  if (typeof document !== 'undefined') {
    const cv = document.createElement('canvas');
    cv.width = 12 * 32; cv.height = 64;
    const cx = cv.getContext('2d');
    if (cx) {
      cx.fillStyle = '#fff'; cx.textAlign = 'center'; cx.textBaseline = 'middle';
      cx.font = '700 46px "JetBrains Mono", ui-monospace, monospace';
      '0123456789k.'.split('').forEach(function (ch, i) { cx.fillText(ch, i * 32 + 16, 34); });
      digitTex = new THREE.CanvasTexture(cv);
      digitTex.minFilter = THREE.LinearFilter; digitTex.generateMipmaps = false;
    }
  }
  const badgeMat = new THREE.ShaderMaterial({
    vertexShader: BADGE_VERT, fragmentShader: BADGE_FRAG,
    transparent: true, depthWrite: false, depthTest: false,
    uniforms: { uDigits: { value: digitTex }, uRes: { value: new THREE.Vector2(1, 1) }, uPR: { value: PR } },
  });
  const ORE_EMPHASIS = 1.8;   // Titanium/Stravidium size multiplier
  // Mushroom shape at aScale 1 (the Large): stem ~18, cap ~1.9x the stem,
  // ~150 tall in board units; a medium blow is the same at aScale ~0.4.
  const plumeShape = { uHeight: view * 0.15, uStem: view * 0.018, uCap: view * 0.016 };
  const plumeMat = new THREE.ShaderMaterial({
    vertexShader: PLUME_VERT, fragmentShader: PLUME_FRAG,
    transparent: true, depthWrite: false, blending: THREE.NormalBlending,
    uniforms: {
      uTime: { value: 0 }, uReduce: { value: reduce },
      uHeight: { value: plumeShape.uHeight }, uStem: { value: plumeShape.uStem }, uCap: { value: plumeShape.uCap },
      uPSize: { value: 44 }, uView: { value: view },
      uPMin: { value: 5 }, uPMax: { value: 56 },       // px clamp floor kept: never sub-pixel
      uOpacity: { value: 0.9 },
      uStemCol: { value: STEM_COL.clone() }, uCapCol: { value: CAP_COL.clone() },
      uCore: { value: new THREE.Color('#dcb7ec') },    // set per scene from the spice color
      uDim: { value: 1 },                              // night darkens the dust (setLook)
      uCloud: { value: null }, uHasTex: { value: 0 },  // procedural dot until sprite loads
    },
  });
  const motesMat = new THREE.ShaderMaterial({
    vertexShader: PLUME_VERT, fragmentShader: MOTES_FRAG,
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    uniforms: {
      uTime: { value: 0 }, uReduce: { value: reduce },
      uHeight: { value: plumeShape.uHeight }, uStem: { value: plumeShape.uStem }, uCap: { value: plumeShape.uCap },
      uPSize: { value: 30 }, uView: { value: view },
      uPMin: { value: 3 }, uPMax: { value: 40 },
      uOpacity: { value: 0.85 },
      uCore: { value: new THREE.Color('#d9b0f0') },
      uGlint: { value: GLINT_COL.clone() },
      uMotes: { value: null }, uHasTex: { value: 0 },
    },
  });
  const bubbleMat = new THREE.ShaderMaterial({
    vertexShader: BUBBLE_VERT, fragmentShader: BUBBLE_FRAG,
    transparent: true, depthWrite: false, depthTest: false, blending: THREE.AdditiveBlending,
    uniforms: {
      uTime: { value: 0 }, uReduce: { value: reduce }, uRise: { value: view * 0.012 },
      uPR: { value: Math.min(opts.pixelRatio || 1, 2) }, uZoom: { value: 1 },
      uColor: { value: BUBBLE_COL.clone() },
    },
  });
  const pipMat = new THREE.ShaderMaterial({
    vertexShader: PIP_VERT, fragmentShader: PIP_FRAG,
    transparent: true, depthWrite: false, depthTest: false,
    uniforms: {
      uTime: { value: 0 }, uReduce: { value: reduce },
      uPR: { value: Math.min(opts.pixelRatio || 1, 2) }, uZoom: { value: 1 },
    },
  });
  const wormPipMat = new THREE.ShaderMaterial({
    vertexShader: PIP_VERT, fragmentShader: WORM_PIP_FRAG,
    transparent: true, depthWrite: false, depthTest: false,
    uniforms: {
      uTime: { value: 0 }, uReduce: { value: reduce },
      uPR: { value: Math.min(opts.pixelRatio || 1, 2) }, uZoom: { value: 1 },
    },
  });
  const stainMat = new THREE.ShaderMaterial({
    vertexShader: STAIN_VERT, fragmentShader: STAIN_FRAG,
    // discGeo winds clockwise seen from above, so a single-sided disc is culled
    transparent: true, depthWrite: false, depthTest: false, side: THREE.DoubleSide,
    uniforms: { uTime: { value: 0 }, uReduce: { value: reduce } },
  });

  // Sandstorm sweep: one board-wide sheet, warm sand dust, alpha-over. Colors
  // and rates are tunable uniforms; animation is driven purely by uTime.
  const SAND = new THREE.Color().setStyle('rgb(200,170,120)');
  const EDGE_DAY = new THREE.Color().setStyle('rgb(255,228,176)');
  const EDGE_NIGHT = new THREE.Color().setStyle('rgb(226,232,240)');
  // Footprint dust tones (owner 2.26.2): rust/ochre sand-dust, red-orange danger edge,
  // pale gust haze. Night dims the dust; the danger edge stays bright.
  const FOOT_DAY = new THREE.Color().setStyle('rgb(128,70,34)');
  const FOOT_NIGHT = new THREE.Color().setStyle('rgb(96,58,38)');
  const DANGER_DAY = new THREE.Color().setStyle('rgb(255,78,36)');
  const DANGER_NIGHT = new THREE.Color().setStyle('rgb(255,104,60)');
  const GUST_DAY = new THREE.Color().setStyle('rgb(240,212,164)');
  const GUST_NIGHT = new THREE.Color().setStyle('rgb(176,160,140)');
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
      uMode: { value: 0 },            // 0 = sweep (unused), 1 = positioned dome, 2 = footprint
      uCenter: { value: new THREE.Vector2(0.5, 0.5) },   // UV space (board/VIEW)
      uRadius: { value: 0.15 },       // UV space
      uDir: { value: new THREE.Vector2(1, 0) },          // heading (cos yaw, sin yaw)
      uDrift: { value: 0.03 },        // internal dust drift speed along heading
      uStorm: { value: null }, uHasTex: { value: 0 },
      uEdgeColor: { value: new THREE.Color().setStyle('rgb(255,228,176)') },
      uFootColor: { value: new THREE.Color().setStyle('rgb(128,70,34)') },
      uDangerColor: { value: new THREE.Color().setStyle('rgb(255,78,36)') },
      uGustColor: { value: new THREE.Color().setStyle('rgb(240,212,164)') },
      uFootAlpha: { value: 0.72 },
      uZoneC: { value: [0, 1, 2, 3].map(function () { return new THREE.Vector2(); }) },
      uZoneAx: { value: [0, 1, 2, 3].map(function () { return new THREE.Vector2(0.01, 0.01); }) },
      uZoneRot: { value: [0, 1, 2, 3].map(function () { return new THREE.Vector2(1, 0); }) },
      uZoneDmg: { value: [0, 0, 0, 0] }, uZoneN: { value: 0 },
      uRouteA: { value: new THREE.Vector2() }, uRouteB: { value: new THREE.Vector2() },
      uHasRoute: { value: 0 }, uPos: { value: new THREE.Vector2(0.5, 0.5) },
    },
    extensions: { derivatives: true },
  });

  // Load the spice sprites once (async, same-origin static under the SvelteKit
  // base). On resolve, flip uHasTex so the shader samples the sprite alpha; on
  // error the procedural soft-dot fallback stays, so a failed fetch never
  // throws. Textures are disposed on teardown.
  const texLoader = new THREE.TextureLoader();
  let cloudTex = null, motesTex = null, stormTex = null, atlasTex = null;
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
  // Marker icon atlas (the game's own map glyphs, packed at build time). Left in
  // raw sRGB (no colour-space decode) because this material writes unencoded
  // output: the icons then land on screen exactly as authored.
  texLoader.load(assetBase + '/' + ATLAS_URL,
    function (tex) {
      tex.generateMipmaps = true;
      tex.minFilter = THREE.LinearMipmapLinearFilter;
      tex.magFilter = THREE.LinearFilter;
      tex.needsUpdate = true;
      atlasTex = tex;
      pointsMat.uniforms.uAtlas.value = tex;
      pointsMat.uniforms.uHasAtlas.value = 1;
      clusterMat.uniforms.uAtlas.value = tex;
      clusterMat.uniforms.uHasAtlas.value = 1;
    },
    undefined,
    function () { /* dots stay; never error */ });
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
  const pinMesh = instColumn(PIN_MAX);

  const stainMesh = (function () {
    const g = discGeo(THREE);
    g.setAttribute('aAmt', new THREE.InstancedBufferAttribute(new Float32Array(SPICE_MAX), 1));
    const seeds = new Float32Array(SPICE_MAX);
    for (let i = 0; i < SPICE_MAX; i++) seeds[i] = phash(i, 9);
    g.setAttribute('aSeed', new THREE.InstancedBufferAttribute(seeds, 1));
    const m = new THREE.InstancedMesh(g, stainMat, SPICE_MAX);
    m.setColorAt(0, C.set(0, 0, 0));
    m.count = 0; m.frustumCulled = false;
    return m;
  })();
  const pipPoints = (function () {
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(SPICE_MAX * 3), 3));
    g.setAttribute('aColor', new THREE.BufferAttribute(new Float32Array(SPICE_MAX * 3), 3));
    g.setAttribute('aPx', new THREE.BufferAttribute(new Float32Array(SPICE_MAX), 1));
    g.setAttribute('aKind', new THREE.BufferAttribute(new Float32Array(SPICE_MAX), 1));
    g.setDrawRange(0, 0);
    const pts = new THREE.Points(g, pipMat);
    pts.frustumCulled = false;
    return pts;
  })();

  const wormPips = (function () {
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(WORM_MAX * 3), 3));
    g.setAttribute('aColor', new THREE.BufferAttribute(new Float32Array(WORM_MAX * 3), 3));
    g.setAttribute('aPx', new THREE.BufferAttribute(new Float32Array(WORM_MAX), 1));
    g.setAttribute('aKind', new THREE.BufferAttribute(new Float32Array(WORM_MAX), 1));
    g.setDrawRange(0, 0);
    const pts = new THREE.Points(g, wormPipMat);
    pts.frustumCulled = false;
    return pts;
  })();
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

  // Blow particles: fixed slices, Larges first then mediums (blowSlices).
  const DUST_CAP = BLOW_LARGE_MAX * LARGE_DUST + BLOW_MED_MAX * MED_DUST;
  const GLOW_CAP = BLOW_LARGE_MAX * LARGE_GLOW + BLOW_MED_MAX * MED_GLOW;
  function blowPoints(cap, mat) {
    const g = plumeGeo(THREE, cap);
    g.setAttribute('aCenter', new THREE.BufferAttribute(new Float32Array(cap * 3), 3));
    g.setAttribute('aScale', new THREE.BufferAttribute(new Float32Array(cap), 1));
    g.setDrawRange(0, 0);
    const pts = new THREE.Points(g, mat);
    pts.frustumCulled = false;
    return pts;
  }
  const plumePoints = blowPoints(DUST_CAP, plumeMat);
  const motesPoints = blowPoints(GLOW_CAP, motesMat);
  const bubblePoints = (function () {
    const cap = SPICE_MAX * BUBBLE_PER;
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(cap * 3), 3));
    g.setAttribute('aCenter', new THREE.BufferAttribute(new Float32Array(cap * 3), 3));
    g.setAttribute('aRad', new THREE.BufferAttribute(new Float32Array(cap), 1));
    const seed = new Float32Array(cap);
    for (let i = 0; i < cap; i++) seed[i] = phash(i, 11);
    g.setAttribute('aSeed', new THREE.BufferAttribute(seed, 1));
    g.setDrawRange(0, 0);
    const pts = new THREE.Points(g, bubbleMat);
    pts.frustumCulled = false;
    return pts;
  })();

  const stormMesh = new THREE.Mesh(
    stormGeo(THREE, half, heightAt, view * 0.015, 24), stormMat);
  stormMesh.visible = false;
  stormMesh.frustumCulled = false;

  let markerPoints = null;   // rebuilt on data/hidden change
  // clustering state (dense layers fold into cluster icons at low zoom)
  let clScene = null, clItems = [], clMarkers = [], clHide = null, clColors = [], clRects = [], clTypeName = [];
  let clLevel = 0, clList = [], clMembers = new Set(), clPoints = null, badgePoints = null;
  let dispNow = 600, projectFn = null;
  let denseCount = 0, midCount = 0;        // visible resource nodes (icon budget)
  let zoomNow = 1;
  function emptyPoints(mat) {
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(0), 3));
    const pts = new THREE.Points(g, mat);
    pts.frustumCulled = false;
    root.add(pts);
    return pts;
  }
  clPoints = emptyPoints(clusterMat);
  badgePoints = emptyPoints(badgeMat);
  // Draw order. The relief material is transparent, so everything shares the
  // sorted transparent pass: without an explicit renderOrder three sorts by each
  // object's bounding-sphere centre, and an InstancedMesh's sphere is computed
  // once from whatever instances existed on the first frame. A layer whose
  // centre sorted behind the terrain's was painted over by the relief (2.26.0:
  // every spice column on DD). Terrain is 0; stains lie on it; 3D overlays next;
  // cluster discs + badges after; spice pips last so clusters never hide a field.
  stainMesh.renderOrder = 1;
  bubblePoints.renderOrder = 1;
  [pinMesh, rippleMesh, blipMesh, ringMesh, plumePoints].forEach(function (m) { m.renderOrder = 2; });
  // The storm stays in the 3D-overlay band but draws FIRST within it (after the
  // stains), so its denser footprint body never ties-sorts over the family-2
  // markers and pins inside it; clusters, spice and worms are above anyway.
  stormMesh.renderOrder = 1.9;
  motesPoints.renderOrder = 3;   // the additive glow shows through the dust, never under it
  clPoints.renderOrder = 4; badgePoints.renderOrder = 5;
  pipPoints.renderOrder = 6;
  wormPips.renderOrder = 7;   // a worm is a threat: on top of everything

  root.add(stainMesh, bubblePoints, pinMesh, rippleMesh, blipMesh, ringMesh, plumePoints, motesPoints, stormMesh, pipPoints, wormPips);

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
  let lastSpice = [];   // the spice specs last drawn (QA readback)

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

  // ---- spice: patch + pip per field, bubbles on idle fields, blows ----------
  // kind: 0 Large candidate, 1 medium, 2 erupting medium, 3 active Large.
  // Idle fields (0, 1) bubble; erupting mediums get a medium blow and the
  // active Large the hero blow. The screen-space pip keeps every field legible
  // at any zoom, and above the cluster discs.
  const PIP_PX = [13, 10, 12, 18];
  const PIP_LIFT = [0.1, 0.12, 0.35, 0.3];   // fill lifted toward white so it reads on day sand
  const PATCH_D = [view * 0.05, view * 0.03, view * 0.034, view * 0.08];   // patch diameter
  const PATCH_A = [0.6, 0.72, 0.8, 0.85];
  const MED_SCALE = 0.38;
  function updateSpice(scene) {
    const hidden = scene.hidden || {};
    const specs = [];
    const dimKey = String(scene.dim);
    const purple = spiceColor(scene);

    if (!hidden['spice_large']) {
      const cand = scene.candidates || {};
      const sp = (scene.spiceActive && scene.spiceActive.dimensions) || {};
      const info = sp[dimKey];
      const act = [];
      if (info && info.large_active) {
        const fields = (info.ram_active_fields && info.ram_active_fields.length)
          ? info.ram_active_fields
          : [{ nx: info.ram_nx, ny: info.ram_ny }];
        fields.forEach(function (f) {
          if (f.nx != null && f.ny != null) act.push({ nx: f.nx, ny: f.ny, kind: 3 });
        });
      }
      Object.keys(cand).forEach(function (sec) {
        const xy = cand[sec];
        if (!xy || xy.length !== 2) return;
        // a candidate that is blowing right now is drawn as the active Large only
        for (let i = 0; i < act.length; i++) if (Math.abs(act[i].nx - xy[0]) < 1 && Math.abs(act[i].ny - xy[1]) < 1) return;
        specs.push({ nx: xy[0], ny: xy[1], kind: 0 });
      });
      act.forEach(function (a) { specs.push(a); });
    }
    if (!hidden['spice_medium']) {
      (scene.mediums || []).forEach(function (md) {
        if (!md || md.length < 2) return;
        specs.push({ nx: md[0], ny: md[1], kind: md[3] === true ? 2 : 1 });
      });
    }
    fillSpiceGround(specs, purple);
    fillBlows(specs, purple);
    lastSpice = specs;
  }

  // Patches + pips + idle bubbles for the same specs (buffers preallocated).
  function fillSpiceGround(specs, purple) {
    const n = Math.min(specs.length, SPICE_MAX);
    const g = pipPoints.geometry;
    const pos = g.attributes.position.array, col = g.attributes.aColor.array;
    const px = g.attributes.aPx.array, kind = g.attributes.aKind.array;
    const amt = stainMesh.geometry.attributes.aAmt;
    const bg = bubblePoints.geometry;
    const bc = bg.attributes.aCenter.array, br = bg.attributes.aRad.array;
    const lift = view * 0.0005;   // pips draw without depth test: no lift, no parallax
    PATCH_C.copy(purple);
    let nb = 0;
    for (let i = 0; i < n; i++) {
      const s = specs[i], k = s.kind;
      const y = heightAt(s.nx, s.ny);
      pos[i * 3] = s.nx - half; pos[i * 3 + 1] = y + lift; pos[i * 3 + 2] = s.ny - half;
      C.copy(purple).lerp(WHITE, PIP_LIFT[k]);
      col[i * 3] = C.r; col[i * 3 + 1] = C.g; col[i * 3 + 2] = C.b;
      px[i] = PIP_PX[k]; kind[i] = k;
      worldSet(stainMesh, i, s.nx, s.ny, PATCH_D[k], 1, PATCH_D[k]);
      stainMesh.setColorAt(i, PATCH_C);
      amt.array[i] = PATCH_A[k];
      if (k < 2) {
        for (let j = 0; j < BUBBLE_PER; j++, nb++) {
          bc[nb * 3] = s.nx - half; bc[nb * 3 + 1] = y; bc[nb * 3 + 2] = s.ny - half;
          br[nb] = PATCH_D[k] * 0.5;
        }
      }
    }
    g.setDrawRange(0, n);
    ['position', 'aColor', 'aPx', 'aKind'].forEach(function (a) { g.attributes[a].needsUpdate = true; });
    stainMesh.count = n;
    stainMesh.instanceMatrix.needsUpdate = true;
    stainMesh.instanceColor.needsUpdate = true;
    amt.needsUpdate = true;
    bg.setDrawRange(0, nb);
    bg.attributes.aCenter.needsUpdate = true;
    bg.attributes.aRad.needsUpdate = true;
  }

  // Blow slices: up to BLOW_LARGE_MAX Larges at scale 1, then up to
  // BLOW_MED_MAX erupting mediums at MED_SCALE, packed from slot 0 so the draw
  // range covers only live blows. The glow accent is the live spice color
  // lifted toward white. No per-frame allocation.
  let blowCount = { large: 0, medium: 0 };
  function fillBlows(specs, purple) {
    const dg = plumePoints.geometry, gg = motesPoints.geometry;
    const dc = dg.attributes.aCenter.array, ds = dg.attributes.aScale.array;
    const gc = gg.attributes.aCenter.array, gs = gg.attributes.aScale.array;
    let nd = 0, ng = 0, nl = 0, nm = 0;
    function put(s, scale, nDust, nGlow) {
      const y = heightAt(s.nx, s.ny);
      for (let j = 0; j < nDust; j++, nd++) {
        dc[nd * 3] = s.nx - half; dc[nd * 3 + 1] = y; dc[nd * 3 + 2] = s.ny - half; ds[nd] = scale;
      }
      for (let j = 0; j < nGlow; j++, ng++) {
        gc[ng * 3] = s.nx - half; gc[ng * 3 + 1] = y; gc[ng * 3 + 2] = s.ny - half; gs[ng] = scale;
      }
    }
    for (let i = 0; i < specs.length && nl < BLOW_LARGE_MAX; i++) {
      if (specs[i].kind === 3) { put(specs[i], 1, LARGE_DUST, LARGE_GLOW); nl++; }
    }
    for (let i = 0; i < specs.length && nm < BLOW_MED_MAX; i++) {
      if (specs[i].kind === 2) { put(specs[i], MED_SCALE, MED_DUST, MED_GLOW); nm++; }
    }
    dg.setDrawRange(0, nd); gg.setDrawRange(0, ng);
    dg.attributes.aCenter.needsUpdate = true; dg.attributes.aScale.needsUpdate = true;
    gg.attributes.aCenter.needsUpdate = true; gg.attributes.aScale.needsUpdate = true;
    plumePoints.visible = nd > 0; motesPoints.visible = ng > 0;
    blowCount = { large: nl, medium: nm };
    CORE_C.copy(purple).lerp(WHITE, 0.55);
    plumeMat.uniforms.uCore.value.copy(CORE_C);
    motesMat.uniforms.uCore.value.copy(CORE_C);
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
      const blipR = view * 0.014 * size;
      const rippleR = view * 0.05 * size;
      const rec = wormSlots[id] || (wormSlots[id] = {});
      const isNew = rec.slot == null;
      rec.slot = slot; rec.blipR = blipR; rec.rippleR = rippleR;

      const col = C.copy(threatColor[threat] || threatColor.surfaced).multiplyScalar(op);
      blipMesh.setColorAt(slot, col);
      const wg = wormPips.geometry.attributes;
      const pc = threatColor[threat] || threatColor.surfaced;   // the pip keeps full colour; age dims the ground blip
      wg.aColor.array[slot * 3] = pc.r; wg.aColor.array[slot * 3 + 1] = pc.g; wg.aColor.array[slot * 3 + 2] = pc.b;
      wg.aPx.array[slot] = 12 * size; wg.aKind.array[slot] = 0;
      wg.aColor.needsUpdate = true; wg.aPx.needsUpdate = true;
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
    wormPips.geometry.setDrawRange(0, slot);
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
      const wp = wormPips.geometry.attributes.position;
      wp.array[rec.slot * 3] = x - half; wp.array[rec.slot * 3 + 1] = heightAt(x, y); wp.array[rec.slot * 3 + 2] = y - half;
      wp.needsUpdate = true;
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

  // Reader v2 footprint: zone ellipses + route, placed every frame from the
  // deterministic route motion (stormFootprint.js), so the storm glides between
  // polls and a new poll does not jump. Board coords -> UV = board/VIEW.
  let stormFp = null;
  function setStormFootprint(fp) {
    const u = stormMat.uniforms;
    const n = Math.min(4, fp.zones.length);
    let rmax = 0;
    for (let i = 0; i < 4; i++) {
      const z = fp.zones[i];
      if (i >= n) { u.uZoneDmg.value[i] = 0; continue; }
      const rad = z.yaw * Math.PI / 180;
      u.uZoneAx.value[i].set(z.ax / view, z.ay / view);
      u.uZoneRot.value[i].set(Math.cos(rad), Math.sin(rad));
      u.uZoneDmg.value[i] = z.damage ? 1 : 0;
      if (z.damage) rmax = Math.max(rmax, z.ay, z.ax);
    }
    u.uZoneN.value = n;
    u.uRadius.value = (rmax || view * 0.1) / view;   // flipbook scale + lightning spread
    const yr = fp.yaw * Math.PI / 180;
    u.uDir.value.set(Math.cos(yr), Math.sin(yr));
    u.uHasRoute.value = fp.route ? 1 : 0;
    if (fp.route) {
      u.uRouteA.value.set(fp.route.sx / view, fp.route.sy / view);
      u.uRouteB.value.set(fp.route.ex / view, fp.route.ey / view);
    }
  }
  function placeStormFootprint(wallMs) {
    const p = positionAt(stormFp, wallMs, !!reduce);
    const u = stormMat.uniforms;
    for (let i = 0; i < Math.min(4, stormFp.zones.length); i++) {
      const z = stormFp.zones[i];
      u.uZoneC.value[i].set((p.x + z.dx) / view, (p.y + z.dy) / view);
    }
    u.uPos.value.set(p.x / view, p.y / view);
    u.uCenter.value.set(p.x / view, p.y / view);
    return p;
  }

  // ---- storm: rim glow accent + positioned dome (live) / sweep (fallback) ----
  function updateStorm(scene) {
    const hidden = scene.hidden || {};
    const info = ((scene.sandstorm && scene.sandstorm.dimensions) || {})[String(scene.dim)];
    const toggledOn = hidden['sandstorm'] !== true;
    const force = STORM_FORCE || (scene && scene.sandstormForce === true);
    // A live position or nothing (owner, 2.26.1): no timing-only sweeps after a
    // detected spawn or a passed ETA; those painted storms nobody saw. The relay's
    // storm_state 'clear' (reader healthy, no storm) draws nothing.
    const clear = !!(info && info.storm_state === 'clear');
    let show = false, mode = 0, intensity = 1;
    stormFp = null;

    if (toggledOn && (!clear || force)) {
      const cx = info && info.center_nx, cy = info && info.center_ny;
      const hasCenter = isFiniteNum(cx) && isFiniteNum(cy);
      if (hasCenter) {
        // Live positioned storm. Fade with freshness; force overrides the gate.
        const fresh = stormLocFresh(info);
        if (fresh > 0 || force) {
          show = true; mode = 1;
          intensity = force ? Math.max(fresh, 1) : fresh;
          const fp = stormFootprint(info);
          const now = Date.now();
          if (fp && isOver(fp, now) && !force) {
            show = false;   // the storm's lifetime ran out before the next poll
          } else if (fp) {
            // Real boundary (reader v2); the circle is only the no-zones fallback.
            mode = 2;
            stormFp = fp;
            setStormFootprint(fp);
            placeStormFootprint(now);
          } else {
            setStormPositioned(cx, cy, info.radius_nr, info.heading_yaw);
          }
        }
      } else if (force) {
        // No live center: eyeball the positioned look at board center.
        show = true; mode = 1; intensity = 1;
        setStormPositioned(view * 0.5, view * 0.5, view * 0.18,
          info && isFiniteNum(info.heading_yaw) ? info.heading_yaw : 0);
      }
    }

    stormMesh.visible = show;
    ringMesh.visible = show;   // rim accent only while a positioned storm is drawn
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

    // Visible markers, resource layers first and POIs last so icons of the
    // landmarks draw over the dense node field (Points draw in index order).
    const typeIcons = scene.typeIcons || [];
    const vis = [];
    for (let i = 0; i < markers.length; i++) {
      const m = markers[i];
      if (!markerVisible(m, catIndex, hidden)) continue;
      const cat = catIndex[m[2]] || 'other';
      vis.push({ m: m, i: i, cat: cat, dr: drawRank(cat) });
    }
    vis.sort(function (a, b) { return a.dr - b.dr || a.i - b.i; });

    const px = [], cx = [], sz = [], rc = [], lod = [];
    const lift = view * 0.0005;   // on the surface; POINTS_VERT handles the depth
    let dense = 0, mid = 0;
    clItems = []; clMarkers = []; clScene = scene;
    for (let k = 0; k < vis.length; k++) {
      const v = vis[k], m = v.m, typeIdx = m[3];
      px.push(m[0] - half, heightAt(m[0], m[1]) + lift, m[1] - half);
      const col = catColor(v.cat);
      cx.push(col.r, col.g, col.b);
      const hot = typeof typeIdx === 'number' && hotType[typeIdx];
      sz.push(hot ? ORE_EMPHASIS : 1.0);
      const r = (typeof typeIdx === 'number') ? rectFor(typeIcons[typeIdx]) : null;
      if (r) rc.push(r[0], r[1], r[2], r[3]); else rc.push(0, 0, 0, 0);
      const cls = lodClass(v.cat);
      if (cls === 1) dense++; else if (cls === 2) mid++;
      lod.push(cls, hot ? 0 : rankFor(v.i));
      clMarkers.push(m);
      // POIs (class 0) never cluster; dense resource, camp and hazard layers do
      if (isClusterCat(v.cat)) clItems.push({ x: m[0], y: m[1], key: typeof typeIdx === 'number' ? typeIdx : 'c' + v.cat, k: k });
    }
    clColors = cx; clRects = rc;
    clHide = new Float32Array(vis.length);
    denseCount = dense; midCount = mid;
    // Hubs carry ~30 markers on a small diorama: read as proper map pins.
    pointsMat.uniforms.uIconPx.value = vis.length < 200 ? 22 : 15;
    applyLod();

    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(px), 3));
    g.setAttribute('aColor', new THREE.BufferAttribute(new Float32Array(cx), 3));
    g.setAttribute('aSize', new THREE.BufferAttribute(new Float32Array(sz), 1));
    g.setAttribute('aRect', new THREE.BufferAttribute(new Float32Array(rc), 4));
    g.setAttribute('aLod', new THREE.BufferAttribute(new Float32Array(lod), 2));
    g.setAttribute('aHide', new THREE.BufferAttribute(clHide, 1));
    if (markerPoints) {
      root.remove(markerPoints);
      markerPoints.geometry.dispose();
    } else {
      markerPoints = new THREE.Points(g, pointsMat);
      markerPoints.frustumCulled = false;
      markerPoints.renderOrder = 2;   // see the draw-order note at root.add
    }
    markerPoints.geometry = g;
    root.add(markerPoints);
    recluster(true);
  }

  // ---- clusters -----------------------------------------------------------------
  // Dense markers fold into one cluster icon per (quadtree cell, type); the level
  // follows the zoom (cluster.js), so this only reruns when the level actually steps
  // or the marker set changes. Folded markers get aHide = 1 on the static point cloud.
  function typeNameOf(c) {
    const idx = clScene && clScene.typeIndex;
    if (typeof c.key === 'number') return (idx && idx[c.key]) || 'Markers';
    const cat = String(c.key).slice(1);
    const leg = (clScene && clScene.legend) || [];
    for (let i = 0; i < leg.length; i++) if (leg[i].key === cat) return leg[i].label;
    return cat;
  }

  function recluster(force) {
    if (!markerPoints || !clHide) return;
    const lvl = clusterLevel(zoomNow, dispNow, clLevel);
    if (!force && lvl === clLevel) return;
    clLevel = lvl;
    const res = buildClusters(clItems, lvl, view, opts.tier);
    clList = res.clusters;
    clHide.fill(0);
    clMembers = new Set();
    for (let i = 0; i < clList.length; i++) {
      const c = clList[i];
      for (let j = 0; j < c.ks.length; j++) { clHide[c.ks[j]] = 1; clMembers.add(clMarkers[c.ks[j]]); }
    }
    const aHide = markerPoints.geometry.getAttribute('aHide');
    if (aHide) aHide.needsUpdate = true;

    const n = clList.length;
    const pos = new Float32Array(n * 3), col = new Float32Array(n * 3), rect = new Float32Array(n * 4);
    const px = new Float32Array(n), gl = new Float32Array(n * 4), lo = new Float32Array(n * 2);
    const lift = view * 0.0005;   // discs draw without depth test: no lift, no parallax
    for (let i = 0; i < n; i++) {
      const c = clList[i], k0 = c.ks[0];
      pos[i * 3] = c.x - half; pos[i * 3 + 1] = heightAt(c.x, c.y) + lift; pos[i * 3 + 2] = c.y - half;
      col[i * 3] = clColors[k0 * 3]; col[i * 3 + 1] = clColors[k0 * 3 + 1]; col[i * 3 + 2] = clColors[k0 * 3 + 2];
      for (let q = 0; q < 4; q++) rect[i * 4 + q] = clRects[k0 * 4 + q];
      const d = discPx(c.count);
      c.r = d / 2;                       // CSS px, for screen-space hit-testing
      px[i] = d * PR;
      const b = badgeGlyphs(c.count);
      for (let q = 0; q < b.glyphs.length && q < 4; q++) gl[i * 4 + q] = b.glyphs[q];
      lo[i * 2] = Math.min(4, b.glyphs.length); lo[i * 2 + 1] = d * PR / 2;
      c.name = typeNameOf(c);
    }
    function swap(pts, attrs) {
      const g = new THREE.BufferGeometry();
      attrs.forEach(function (a) { g.setAttribute(a[0], new THREE.BufferAttribute(a[1], a[2])); });
      pts.geometry.dispose();
      pts.geometry = g;
    }
    swap(clPoints, [['position', pos, 3], ['aColor', col, 3], ['aRect', rect, 4], ['aPx', px, 1]]);
    swap(badgePoints, [['position', pos, 3], ['aColor', col, 3], ['aGlyph', gl, 4], ['aLenOff', lo, 2]]);
  }

  // Cluster under a viewport pixel, or null. Screen-space against the same relief-aware
  // projector the engine uses, so what you see is what you hit. -> { x, y, count, name,
  // label, zoom } (board units; zoom = camera zoom that frames the cluster's bounds)
  function clusterAt(mx, my) {
    if (!projectFn || !clList.length) return null;
    let best = null, bestT = Infinity;
    for (let i = 0; i < clList.length; i++) {
      const c = clList[i];
      const s = projectFn(c.x, c.y);
      if (!s) continue;
      const dx = s.screenX - mx, dy = s.screenY - my;
      const r = c.r + 3;
      const t = (dx * dx + dy * dy) / (r * r);
      if (t <= 1 && t < bestT) { bestT = t; best = c; }
    }
    if (!best) return null;
    return {
      x: best.x, y: best.y, count: best.count, name: best.name,
      label: best.name + ': ' + best.count.toLocaleString('en-US') + ' nodes. Zoom in to see each one',
      zoom: zoomForBounds(best, zoomNow, view),
    };
  }
  function clusterMembers() { return clMembers; }
  // QA readback: what the spice layer drew vs the specs it was asked for.
  function spiceInfo() {
    return {
      specs: lastSpice.length,
      pips: pipPoints.geometry.drawRange.count, patches: stainMesh.count,
      bubbles: bubblePoints.geometry.drawRange.count,
      blows: blowCount,
      fields: lastSpice.map(function (s) {
        return { kind: ['candidate', 'medium', 'erupting', 'active'][s.kind], nx: s.nx, ny: s.ny, ground: heightAt(s.nx, s.ny) };
      }),
    };
  }
  // QA readback: world anchors as uploaded to the GPU (every k-th marker, all
  // cluster discs, all spice pips), in board coords (x = nx, z = ny).
  function anchorsInfo(k) {
    function read(pts, every) {
      const out = [];
      if (!pts) return out;
      const p = pts.geometry.attributes.position;
      const n = pts.geometry.drawRange.count === Infinity ? p.count : Math.min(p.count, pts.geometry.drawRange.count);
      for (let i = 0; i < n; i += every) out.push([p.getX(i) + half, p.getY(i), p.getZ(i) + half]);
      return out;
    }
    return { markers: read(markerPoints, k || 50), clusters: read(clPoints, 1), pips: read(pipPoints, 1) };
  }
  function wormInfo() {
    const out = [];
    for (let i = 0; i < blipMesh.count; i++) {
      blipMesh.getMatrixAt(i, M4);
      out.push([M4.elements[12] + half, M4.elements[13], M4.elements[14] + half]);
    }
    return { blips: blipMesh.count, ripples: rippleMesh.count, pips: wormPips.geometry.drawRange.count, at: out };
  }
  function stormInfo() {
    const u = stormMat.uniforms;
    const out = { visible: stormMesh.visible, mode: u.uMode.value, ring: ringMesh.visible };
    if (stormFp) {
      out.zones = u.uZoneN.value;
      out.damage = u.uZoneDmg.value.slice(0, u.uZoneN.value);
      out.pos = [u.uPos.value.x * view, u.uPos.value.y * view];
      out.route = !!u.uHasRoute.value;
    }
    return out;
  }
  function clusterInfo() {
    return { level: clLevel, clusters: clList.length, folded: clMembers.size, total: clMarkers.length };
  }
  function setProjector(fn) { projectFn = fn; }
  function setSize(w, h, bufW, bufH) {
    dispNow = Math.max(1, Math.min(w, h));
    badgeMat.uniforms.uRes.value.set(bufW || w * PR, bufH || h * PR);
    recluster(false);
  }
  // Time of day (holo/light.js): night cools the cluster plates and tints the storm sand
  function setLook(l) {
    clusterMat.uniforms.uNight.value = l.night;
    plumeMat.uniforms.uDim.value = 1 - 0.5 * l.night;
    const t = l.night * 0.6;
    stormMat.uniforms.uColor.value.copy(SAND).multiply(new THREE.Color(1 - 0.4 * t, 1 - 0.22 * t, 1));
    // Footprint lines: bright sand on the amber day relief, pale moonlight at night.
    stormMat.uniforms.uEdgeColor.value.copy(EDGE_DAY).lerp(EDGE_NIGHT, l.night);
    stormMat.uniforms.uFootColor.value.copy(FOOT_DAY).lerp(FOOT_NIGHT, l.night);
    stormMat.uniforms.uDangerColor.value.copy(DANGER_DAY).lerp(DANGER_NIGHT, l.night);
    stormMat.uniforms.uGustColor.value.copy(GUST_DAY).lerp(GUST_NIGHT, l.night);
    stormMat.uniforms.uFootAlpha.value = 0.72 - 0.14 * l.night;   // dimmer dust at night
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
    pipMat.uniforms.uTime.value = sec;
    wormPipMat.uniforms.uTime.value = sec;
    stainMat.uniforms.uTime.value = sec;
    bubbleMat.uniforms.uTime.value = sec;
    stormMat.uniforms.uTime.value = sec;
    if (stormFp && stormMesh.visible) {
      const now = Date.now();
      if (isOver(stormFp, now)) { stormMesh.visible = false; ringMesh.visible = false; }
      else if (!reduce) placeStormFootprint(now);
    }
  }

  // Engine zoom -> marker size boost (curve kicks in at close range only).
  function setView(v) {
    const z = (v && v.zoom) ? v.zoom : 1;
    zoomNow = Math.max(1, Math.min(8, z));
    pointsMat.uniforms.uZoom.value = zoomNow;
    pipMat.uniforms.uZoom.value = zoomNow;
    bubbleMat.uniforms.uZoom.value = zoomNow;
    applyLod();
    recluster(false);
  }

  // Resource-node dot -> icon share for the current zoom, tier and visible count.
  function applyLod() {
    pointsMat.uniforms.uIconFrac.value = iconFraction(zoomNow, opts.tier, denseCount);
    pointsMat.uniforms.uIconFracMid.value =
      iconFraction(zoomNow, opts.tier, midCount, MID_ZOOM_START, MID_ZOOM_FULL);
  }

  function setTokens(next) {
    tokens = next;
    ringMat.uniforms.uColor.value.copy(next.accent);
    // Recolor token-driven layers; worms/markers keep their fixed/data colors.
    if (lastScene) { updateSpice(lastScene); updatePins(lastScene); }
  }

  function dispose() {
    reckoner.destroy();
    root.remove(stainMesh, bubblePoints, pinMesh, rippleMesh, blipMesh, ringMesh, plumePoints, motesPoints, stormMesh, pipPoints, wormPips);
    wormPips.geometry.dispose();
    if (markerPoints) { root.remove(markerPoints); markerPoints.geometry.dispose(); }
    root.remove(clPoints, badgePoints);
    clPoints.geometry.dispose(); badgePoints.geometry.dispose();
    if (digitTex) digitTex.dispose();
    plumePoints.geometry.dispose();
    motesPoints.geometry.dispose();
    pipPoints.geometry.dispose();
    bubblePoints.geometry.dispose();
    stormMesh.geometry.dispose();
    if (cloudTex) cloudTex.dispose();
    if (motesTex) motesTex.dispose();
    if (stormTex) stormTex.dispose();
    if (atlasTex) atlasTex.dispose();
    [stainMesh, pinMesh, rippleMesh, blipMesh, ringMesh].forEach(function (m) {
      m.geometry.dispose();
      if (m.dispose) m.dispose();   // frees instanceMatrix/instanceColor
    });
    [columnMat, blipMat, rippleMat, ringMat, pointsMat, clusterMat, badgeMat, plumeMat, motesMat, pipMat, wormPipMat, stainMat, bubbleMat, stormMat].forEach(function (m) {
      m.dispose();
    });
  }

  // The relief under the overlays changed (a baked layout replaced the seeded
  // terrain): heights cached at build time are stale. Re-drape the storm layer
  // and force the next applyScene to rebuild the markers on the new heightAt.
  function reliefChanged() {
    const old = stormMesh.geometry;
    stormMesh.geometry = stormGeo(THREE, half, heightAt, view * 0.015, 24);
    old.dispose();
    lastMarkersRef = null;
  }

  return { applyScene, setView, setTime, setTokens, reliefChanged, dispose, setSize, setProjector, setLook, clusterAt, clusterMembers, clusterInfo, spiceInfo, anchorsInfo, stormInfo, wormInfo };
}
