import { travelBadge } from '../../world/data.js';
// One stone per overland location: the game's own miniature on its basalt disc and
// bronze ring, plus OUR live layer (status glow on the ring, band marks, viewer pin).
// Geometry and colours come from overmap-miniatures.json; nothing is invented.
// THREE is injected.

const WARM_BRONZE = 0xa8794a;
const ALUMINIUM = 0xb9a58a;

export function createStones(THREE, opts) {
  const { world, heightAt, glowTex, hi } = opts;
  const group = new THREE.Group();
  const byId = new Map();
  const hitMeshes = [];
  const disposables = [];
  const mauve = new THREE.MeshStandardMaterial({ color: 0x51283f, roughness: 0.62, metalness: 0.05, emissive: 0x1a0710, emissiveIntensity: 1 });
  disposables.push(mauve);
  const unitCyl = new THREE.CylinderGeometry(1, 1, 1, 56);
  const skirtMat = opts.skirtMat || new THREE.MeshStandardMaterial({ color: 0x2c1b14, roughness: 1 });
  const markGeo = new THREE.SphereGeometry(1, 12, 8);
  disposables.push(unitCyl, skirtMat, markGeo);

  // glyph for "available on travel": a chevron pair
  const gc = document.createElement('canvas'); gc.width = gc.height = 128;
  const gg = gc.getContext('2d');
  gg.strokeStyle = '#f0d9a8'; gg.lineWidth = 11; gg.lineCap = 'round'; gg.lineJoin = 'round';
  gg.beginPath(); gg.moveTo(34, 38); gg.lineTo(64, 64); gg.lineTo(34, 90); gg.moveTo(66, 38); gg.lineTo(96, 64); gg.lineTo(66, 90); gg.stroke();
  const glyphTex = new THREE.CanvasTexture(gc); glyphTex.colorSpace = THREE.SRGBColorSpace;
  disposables.push(glyphTex);
  const pinTex = (() => {
    const c = document.createElement('canvas'); c.width = c.height = 128;
    const g = c.getContext('2d');
    g.fillStyle = '#3fb6c9'; g.strokeStyle = '#06222a'; g.lineWidth = 8;
    g.beginPath(); g.moveTo(64, 118); g.lineTo(26, 40); g.arc(64, 40, 38, Math.PI, 0); g.closePath(); g.fill(); g.stroke();
    g.fillStyle = '#06222a'; g.beginPath(); g.arc(64, 40, 12, 0, 7); g.fill();
    const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace; return t;
  })();
  disposables.push(pinTex);

  // ---- disc and ring materials: weathered bronze, verdigris in the crevices, on dark basalt ----
  const hash = (x, y) => { const n = Math.sin(x * 127.1 + y * 311.7) * 43758.5453; return n - Math.floor(n); };
  const vnoise = (x, y) => {
    const xi = Math.floor(x), yi = Math.floor(y), xf = x - xi, yf = y - yi;
    const u = xf * xf * (3 - 2 * xf), v = yf * yf * (3 - 2 * yf);
    return (hash(xi, yi) * (1 - u) + hash(xi + 1, yi) * u) * (1 - v) + (hash(xi, yi + 1) * (1 - u) + hash(xi + 1, yi + 1) * u) * v;
  };
  const fbm = (x, y) => vnoise(x, y) * 0.5 + vnoise(x * 2.1, y * 2.1) * 0.3 + vnoise(x * 4.3, y * 4.3) * 0.2;
  function paintTex(w, h, fn, repeat) {
    const c = document.createElement('canvas'); c.width = w; c.height = h;
    const g = c.getContext('2d'), img = g.createImageData(w, h);
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      const col = fn(x / w, y / h), k = (y * w + x) * 4;
      img.data[k] = col[0]; img.data[k + 1] = col[1]; img.data[k + 2] = col[2]; img.data[k + 3] = 255;
    }
    g.putImageData(img, 0, 0);
    const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace;
    t.wrapS = t.wrapT = THREE.RepeatWrapping; if (repeat) t.repeat.set(repeat[0], repeat[1]);
    disposables.push(t); return t;
  }
  const lerp3 = (a, b, t) => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
  const plateMat = new THREE.MeshStandardMaterial({ roughness: 0.55, metalness: 0.1, envMap: opts.envTex || null, envMapIntensity: 0.6,
    map: paintTex(256, 256, (u, v) => lerp3([22, 21, 22], [52, 49, 50], fbm(u * 14, v * 14)), [2, 2]) });
  const patina = paintTex(512, 64, (u, v) => {
    const n = fbm(u * 40, v * 6), m = fbm(u * 18 + 7, v * 4 + 3);
    const bronze = lerp3([86, 54, 28], [176, 120, 62], n);
    const gr = Math.max(0, Math.min(1, (0.55 - n) * 5)) * Math.max(0, Math.min(1, (m - 0.38) * 4));
    return lerp3(bronze, [78, 150, 120], gr * 0.9);
  }, [10, 1]);
  disposables.push(plateMat);
  // status = the ring's emissive warmth (the bronze colour itself never changes)
  const PAL = {
    halo: 0xffa23a,
    up: { emissive: 0xff8a1e, ei: 1.1, halo: 0.2 }, down: { emissive: 0, ei: 0, halo: 0 },
    'on-demand': { emissive: 0x2a1a08, ei: 0.4, halo: 0.07 }, blocked: { emissive: 0x3a0f05, ei: 0.45, halo: 0 },
    unknown: { emissive: 0, ei: 0, halo: 0 }, none: { emissive: 0, ei: 0, halo: 0 },
  };

  world.stones.forEach((s) => {
    const r = opts.radii[s.id];
    const gameR = ((s.disc && s.disc.radiusCm) || 1250) / 10000;       // scene units at 1:1
    const ms = r / gameR;                                               // miniature scale
    const cxu = (s.disc ? s.disc.centerCm[0] : s.x) * 1e-4;
    const czu = (s.disc ? s.disc.centerCm[1] : s.y) * 1e-4;
    // plate sits on the highest ground under it, so exaggerated relief never buries the stone
    let gy = -Infinity;
    for (let a = 0; a < 8; a++) for (let k = 0; k <= 1; k++) {
      const h = heightAt(cxu + Math.cos(a * 0.785) * r * k, czu + Math.sin(a * 0.785) * r * k);
      if (h > gy) gy = h;
    }
    const plateY = gy + 0.02;
    const g = new THREE.Group();
    g.position.set(cxu, plateY, czu);
    g.userData.id = s.id;

    const plate = new THREE.Mesh(unitCyl, plateMat);
    plate.scale.set(r, 0.06, r); plate.position.y = -0.02;
    plate.castShadow = true; plate.receiveShadow = true;
    plate.userData.id = s.id; hitMeshes.push(plate);
    g.add(plate);
    const skirt = new THREE.Mesh(unitCyl, skirtMat);
    skirt.scale.set(r * 1.02, 0.3, r * 1.02); skirt.position.y = -0.2;
    g.add(skirt);

    const top = 0.01;
    const ringMat = new THREE.MeshStandardMaterial({ map: patina, roughness: 0.5, metalness: 0.85, envMap: opts.envTex || null, envMapIntensity: 1.1, emissive: 0x000000 });
    const ring = new THREE.Mesh(new THREE.TorusGeometry(r, Math.max(0.02, r * 0.05), 12, 96), ringMat);
    ring.position.y = top + 0.01;
    ring.rotation.x = Math.PI / 2;
    ring.castShadow = true;
    g.add(ring);
    disposables.push(ring.geometry, ringMat);

    // status glow
    const haloMat = new THREE.SpriteMaterial({ map: glowTex, color: 0xffa23a, transparent: true, opacity: 0, depthWrite: false, blending: THREE.AdditiveBlending, toneMapped: false });
    const halo = new THREE.Sprite(haloMat);
    halo.scale.set(r * 3.6, r * 3.6, 1); halo.position.y = 0.12;
    halo.visible = false;
    g.add(halo);
    disposables.push(haloMat);
    // travel badge: a chevron inlaid flat on the plate at the ring edge, not floating
    const glyphMat = new THREE.MeshBasicMaterial({ map: glyphTex, transparent: true, depthWrite: false, opacity: 0.95, toneMapped: false, polygonOffset: true, polygonOffsetFactor: -2 });
    const glyph = new THREE.Mesh(new THREE.PlaneGeometry(r * 0.5, r * 0.5), glyphMat);
    glyph.rotation.x = -Math.PI / 2; glyph.rotation.z = Math.PI / 2;
    glyph.position.set(0, top + 0.012, r * 0.72); glyph.visible = false;
    disposables.push(glyph.geometry);
    g.add(glyph);
    disposables.push(glyphMat);
    const pinMat = new THREE.SpriteMaterial({ map: pinTex, transparent: true, depthWrite: false, toneMapped: false, depthTest: false });
    const pin = new THREE.Sprite(pinMat);
    pin.scale.set(r * 0.9, r * 0.9, 1); pin.position.y = r * 3.2; pin.visible = false; pin.renderOrder = 10;
    g.add(pin);
    disposables.push(pinMat);

    // marks container (rebuilt when bands change)
    const marks = new THREE.Group();
    g.add(marks);

    // invisible hit volume
    const hit = new THREE.Mesh(unitCyl, skirtMat);
    // thin, disc-only fallback: a tall volume would swallow picks aimed at the stone behind it
    hit.scale.set(r * 1.1, 0.2, r * 1.1); hit.position.y = 0.1;
    hit.visible = false; hit.userData.id = s.id;
    g.add(hit);
    hitMeshes.push(hit);

    // miniature (already placed in the shared GLB)
    const root = opts.takeMini(s.id);
    if (root) {
      const dz = s.disc ? s.disc.centerCm[2] : s.z;
      root.position.set((s.x - (s.disc ? s.disc.centerCm[0] : s.x)) * 1e-4 * ms, (s.z - dz) * 1e-4 * ms, (s.y - (s.disc ? s.disc.centerCm[1] : s.y)) * 1e-4 * ms);
      root.scale.setScalar(1e-2 * ms);
      root.rotation.set(0, 0, 0);
      // game z offsets are relative to ITS ground; keep the miniature on the plate
      g.add(root);
      root.traverse((o) => { if (o.isMesh) { o.castShadow = true; o.receiveShadow = true; o.userData.id = s.id; hitMeshes.push(o); } });
    }
    group.add(g);
    byId.set(s.id, { s, g, r, ms, plateY, cxu, czu, ringMat, ring, halo, haloMat, glyph, glyphMat, pin, marks, status: null, sel: false, hover: false, root,
      anchor: root ? { x: g.position.x + root.position.x, y: g.position.y + root.position.y, z: g.position.z + root.position.z } : { x: g.position.x, y: g.position.y, z: g.position.z } });
  });

  // warm stand-in tones for the game's procedural windsack metal, and the erythrite glow
  const crystalMats = [];
  function tuneMaterials(root) {
    const seen = new Set();
    root.traverse((o) => {
      if (!o.isMesh) return;
      const mats = Array.isArray(o.material) ? o.material : [o.material];
      mats.forEach((m) => {
        if (!m || seen.has(m)) return; seen.add(m);
        const nm = (m.name || '') + ' ' + (o.name || '');
        if (/erythrite|crystal/i.test(nm)) {
          m.emissive = new THREE.Color(0xb7123c); m.emissiveIntensity = 1.2; crystalMats.push(m);
        } else {
          // base-colour textures survive; the procedural patina does not, so tint warm and add sheen
          const c = m.color || (m.color = new THREE.Color(1, 1, 1));
          const grey = Math.abs(c.r - c.g) < 0.04 && Math.abs(c.g - c.b) < 0.04;
          if (m.map) c.multiply(new THREE.Color(1.45, 1.2, 0.95));
          else c.setHex(/alum/i.test(nm) ? ALUMINIUM : WARM_BRONZE);
          if (grey && !m.map) c.setHex(WARM_BRONZE);
          m.metalness = 0.35; m.roughness = 0.5;
        }
        m.needsUpdate = true;
      });
    });
  }

  function setStatus(map) {
    byId.forEach((e, id) => {
      const st = map[id] || null;
      e.status = st;
      const d = PAL[st || 'none'] || PAL.none;
      const m = e.ringMat;
      m.emissive.setHex(d.emissive); m.emissiveIntensity = d.ei;
      e.haloMat.color.setHex(PAL.halo);
      e.halo.visible = d.halo > 0; e.haloMat.opacity = d.halo; e.haloBase = d.halo;
      e.glyph.visible = !!travelBadge(st);
      e.dark = st === 'down';
    });
  }

  // bands: { [id]: [{ facet, band: {marks}|null }] } ; only entries with a band draw marks
  function setBands(map) {
    byId.forEach((e, id) => {
      while (e.marks.children.length) {
        const c = e.marks.children.pop(); c.material.dispose();
      }
      const list = (map && map[id] || []).filter((b) => b.band);
      const n = list.length; if (!n) return;
      const lit = new THREE.Color(0xffc060), off = new THREE.Color(0x1a130d);
      list.forEach((b, bi) => {
        for (let k = 0; k < 3; k++) {
          const spread = n > 1 ? 0.32 : 0.55;
          const a = (-Math.PI / 2) + (bi / n) * Math.PI * 2 + (k - 1) * spread;
          const on = k < b.band.marks;
          const m = new THREE.Mesh(markGeo, new THREE.MeshStandardMaterial({ color: on ? lit : off, emissive: on ? 0xff9a2a : 0x000000, emissiveIntensity: on ? 2 : 0, roughness: 0.5, metalness: 0.2 }));
          const mr = Math.max(0.05, e.r * 0.085);
          m.scale.setScalar(mr);
          m.position.set(Math.cos(a) * e.r * 1.2, mr * 0.6, Math.sin(a) * e.r * 1.2);
          e.marks.add(m);
        }
      });
    });
  }

  let night = 0;
  function setNight(n) { night = n; }
  function setAnchor(id) { byId.forEach((e, k) => { e.pin.visible = k === id; }); }
  function setSelected(id) { byId.forEach((e, k) => { e.sel = k === id; }); }
  function setHover(id) { byId.forEach((e, k) => { e.hover = k === id; }); }

  const scratch = new THREE.Color();
  function tick(sec, reduced) {
    byId.forEach((e) => {
      const base = e.haloBase || 0;
      const pulse = (reduced ? 1 : 0.78 + 0.22 * Math.sin(sec * 1.7 + e.r * 9)) * (1 + 0.9 * night);
      if (e.status === 'up') {
        e.haloMat.opacity = base * pulse * ((e.sel || e.hover) ? 1.5 : 1);
        e.ringMat.emissiveIntensity = 1.2 + 0.7 * pulse;
      } else if (e.status === 'on-demand') {
        e.haloMat.opacity = base * ((e.sel || e.hover) ? 2.2 : 1);
        if (e.glyph.visible) e.glyphMat.opacity = reduced ? 0.95 : 0.7 + 0.25 * Math.sin(sec * 1.6);
      } else if (!e.status) {
        const hl = e.sel || e.hover;
        e.ringMat.emissive.setHex(hl ? 0xffb04a : 0x000000);
        e.ringMat.emissiveIntensity = hl ? 0.9 : 0;
      }
      if (e.status && (e.sel || e.hover) && e.status !== 'up') {
        e.ringMat.emissive.setHex(0xffd890); e.ringMat.emissiveIntensity = Math.max(e.ringMat.emissiveIntensity, 0.8);
      }
      if (e.pin.visible) e.pin.position.y = e.r * 3.2 + (reduced ? 0 : Math.sin(sec * 2.2) * e.r * 0.15);
    });
    crystalMats.forEach((m) => { m.emissiveIntensity = reduced ? 1.2 : 0.8 + 0.8 * (0.5 + 0.5 * Math.sin(sec * 0.4 * 2.4)); });
    void scratch;
  }

  function dispose() {
    disposables.forEach((d) => d.dispose && d.dispose());
    byId.forEach((e) => e.marks.children.forEach((c) => c.material.dispose()));
  }
  return { group, byId, hitMeshes, setNight, tuneMaterials, setStatus, setBands, setAnchor, setSelected, setHover, tick, dispose };
}
