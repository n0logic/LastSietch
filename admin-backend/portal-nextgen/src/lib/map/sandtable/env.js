import { blendLook, todWeights } from '../../world/tod.js';
// The sietch chamber around the table: carved stone rim, dark surround with a few
// pillars, a sun-leak slit and the key light that follows the server clock. Textures
// are procedural (canvas), so nothing here ships as an asset and nothing invents a
// map position. THREE is injected.

function rng(seed) {
  let s = seed >>> 0;
  return () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; };
}

// Tileable value noise on a canvas (several octaves), as a gray/colour ramp.
function noiseCanvas(size, seed, ramp, octaves) {
  const c = document.createElement('canvas');
  c.width = c.height = size;
  const g = c.getContext('2d');
  const img = g.createImageData(size, size);
  const rnd = rng(seed);
  const grids = [];
  for (let o = 0; o < octaves; o++) {
    const n = 4 << o;
    const a = new Float32Array(n * n);
    for (let i = 0; i < a.length; i++) a[i] = rnd();
    grids.push({ n, a });
  }
  const smooth = (t) => t * t * (3 - 2 * t);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      let v = 0, amp = 0.55, tot = 0;
      for (let o = 0; o < octaves; o++) {
        const { n, a } = grids[o];
        const fx = (x / size) * n, fy = (y / size) * n;
        const x0 = Math.floor(fx) % n, y0 = Math.floor(fy) % n, x1 = (x0 + 1) % n, y1 = (y0 + 1) % n;
        const tx = smooth(fx - Math.floor(fx)), ty = smooth(fy - Math.floor(fy));
        const top = a[y0 * n + x0] * (1 - tx) + a[y0 * n + x1] * tx;
        const bot = a[y1 * n + x0] * (1 - tx) + a[y1 * n + x1] * tx;
        v += (top * (1 - ty) + bot * ty) * amp; tot += amp; amp *= 0.5;
      }
      v /= tot;
      const k = (y * size + x) * 4;
      const c3 = ramp(v);
      img.data[k] = c3[0]; img.data[k + 1] = c3[1]; img.data[k + 2] = c3[2]; img.data[k + 3] = 255;
    }
  }
  g.putImageData(img, 0, 0);
  return c;
}

// Dressed stone blocks: noise + dark mortar seams (a running bond).
function stoneBlockCanvas(w, h, rows, seed) {
  const base = noiseCanvas(512, seed, (v) => {
    const t = 0.55 + v * 0.7;
    return [Math.min(255, 78 * t), Math.min(255, 58 * t), Math.min(255, 40 * t)];
  }, 5);
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  const g = c.getContext('2d');
  g.drawImage(base, 0, 0, w, h);
  g.strokeStyle = 'rgba(8,5,3,.85)';
  g.lineWidth = 3;
  const rh = h / rows, rnd = rng(seed + 7);
  for (let r = 0; r <= rows; r++) {
    g.beginPath(); g.moveTo(0, r * rh); g.lineTo(w, r * rh); g.stroke();
    let x = rnd() * 200;
    while (x < w) {
      g.beginPath(); g.moveTo(x, r * rh); g.lineTo(x, (r + 1) * rh); g.stroke();
      x += 180 + rnd() * 260;
    }
  }
  return c;
}

function radialSpriteCanvas(stops) {
  const c = document.createElement('canvas');
  c.width = c.height = 128;
  const g = c.getContext('2d');
  const gr = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  stops.forEach((s) => gr.addColorStop(s[0], s[1]));
  g.fillStyle = gr; g.fillRect(0, 0, 128, 128);
  return c;
}

export function createGlowTexture(THREE, stops) {
  const t = new THREE.CanvasTexture(radialSpriteCanvas(stops || [[0, 'rgba(255,255,255,1)'], [0.35, 'rgba(255,255,255,.35)'], [1, 'rgba(255,255,255,0)']]));
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

export function createEnv(THREE, opts) {
  const { cx, cz, R, hi } = opts;
  const group = new THREE.Group();
  const disposables = [];
  const track = (o) => { disposables.push(o); return o; };

  // ---- carved rim (lathe) -------------------------------------------------
  const texC = stoneBlockCanvas(1024, 256, 3, 11);
  const map = track(new THREE.CanvasTexture(texC));
  map.colorSpace = THREE.SRGBColorSpace;
  map.wrapS = THREE.RepeatWrapping; map.wrapT = THREE.RepeatWrapping;
  map.repeat.set(6, 1);
  map.anisotropy = 4;
  const rimMat = track(new THREE.MeshStandardMaterial({ map, bumpMap: map, bumpScale: 2.2, roughness: 0.88, metalness: 0.02, color: 0xffffff }));
  const rin = R * 0.992, w = R * 0.075;
  const top = 0.7;
  const prof = [
    [rin, -1.2], [rin, 0.2], [rin + 0.06, top - 0.2], [rin + 0.2, top], [rin + w, top], [rin + w + 0.18, top - 0.2],
    [rin + w + 0.3, -2.4], [rin + w + 0.3, -9],
  ].map((p) => new THREE.Vector2(p[0], p[1]));
  const rimGeo = track(new THREE.LatheGeometry(prof, 192));
  // lathe v runs along the profile; spread the block texture around the ring
  const rim = new THREE.Mesh(rimGeo, rimMat);
  rim.position.set(cx, 0, cz);
  rim.castShadow = true; rim.receiveShadow = true;
  group.add(rim);

  // ---- engraved rim plate: ticks, N, countdown ------------------------------
  const plateSize = 2048;
  const pc = document.createElement('canvas');
  pc.width = pc.height = plateSize;
  const pg = pc.getContext('2d');
  const plateTex = track(new THREE.CanvasTexture(pc));
  plateTex.colorSpace = THREE.SRGBColorSpace;
  plateTex.anisotropy = 8;
  const outerR = rin + w;
  const plate = new THREE.Mesh(
    track(new THREE.RingGeometry(rin + 0.18, outerR, 192, 1)),
    track(new THREE.MeshBasicMaterial({ map: plateTex, transparent: true, depthWrite: false, polygonOffset: true, polygonOffsetFactor: -2, toneMapped: false })),
  );
  plate.rotation.x = -Math.PI / 2;
  plate.position.set(cx, top + 0.012, cz);
  group.add(plate);
  const toPx = (r) => (r / outerR) * (plateSize / 2);
  let lastText = null;
  function drawPlate(text) {
    if (text === lastText) return;
    lastText = text;
    pg.clearRect(0, 0, plateSize, plateSize);
    pg.save(); pg.translate(plateSize / 2, plateSize / 2);
    const rMid = toPx(rin + (w + 0.18) / 2);
    // ticks every 5 deg, long every 45 (north up on the plate: -90 deg)
    for (let a = 0; a < 360; a += 5) {
      const long = a % 45 === 0;
      const r0 = toPx(rin + 0.2), r1 = toPx(rin + (long ? 0.2 + w * 0.42 : 0.2 + w * 0.22));
      const rad = (a - 90) * Math.PI / 180;
      pg.strokeStyle = long ? 'rgba(240,190,110,.8)' : 'rgba(210,160,90,.45)';
      pg.lineWidth = long ? 5 : 2.5;
      pg.beginPath(); pg.moveTo(Math.cos(rad) * r0, Math.sin(rad) * r0); pg.lineTo(Math.cos(rad) * r1, Math.sin(rad) * r1); pg.stroke();
    }
    pg.fillStyle = 'rgba(245,200,120,.92)';
    pg.font = '600 66px "Saira Condensed", sans-serif';
    pg.textAlign = 'center'; pg.textBaseline = 'middle';
    pg.save(); pg.translate(0, -rMid * 1.0 + 0); pg.fillText('N', 0, 12); pg.restore();
    // arced countdown, centred at the south-east (angle 55 deg from +x, screen y down)
    if (text) {
      pg.font = '600 52px "Saira Condensed", sans-serif';
      const chars = text.toUpperCase().split('');
      const rText = toPx(rin + (w + 0.18) * 0.62);
      const step = 0.0168 * (3.9 / (rText / 560)) ;
      const span = step * (chars.length - 1);
      const mid = 58 * Math.PI / 180;
      pg.fillStyle = 'rgba(0,0,0,.65)';
      chars.forEach((ch, i) => {
        const a = mid + (i - (chars.length - 1) / 2) * step * -1;
        pg.save(); pg.rotate(a); pg.translate(rText, 0); pg.rotate(-Math.PI / 2); pg.fillText(ch, 2, 2); pg.restore();
      });
      pg.fillStyle = 'rgba(247,196,116,.95)';
      chars.forEach((ch, i) => {
        const a = mid + (i - (chars.length - 1) / 2) * step * -1;
        pg.save(); pg.rotate(a); pg.translate(rText, 0); pg.rotate(-Math.PI / 2); pg.fillText(ch, 0, 0); pg.restore();
      });
      void span;
    }
    pg.restore();
    plateTex.needsUpdate = true;
  }
  drawPlate('');

  // ---- chamber: floor, walls, pillars --------------------------------------
  const floorMat = track(new THREE.MeshStandardMaterial({ color: 0x1a120c, roughness: 1 }));
  const floor = new THREE.Mesh(track(new THREE.CircleGeometry(R * 6, 48)), floorMat);
  floor.rotation.x = -Math.PI / 2; floor.position.set(cx, -9.05, cz); floor.receiveShadow = true;
  group.add(floor);
  // the table drum
  const drum = new THREE.Mesh(track(new THREE.CylinderGeometry(rin + w + 0.3, rin + w + 0.8, 9, 96, 1, true)), rimMat);
  drum.position.set(cx, -4.5, cz); drum.receiveShadow = true;
  group.add(drum);

  const wallCanvas = noiseCanvas(512, 5, (v) => { const t = 0.4 + v * 0.8; return [46 * t, 32 * t, 22 * t]; }, 5);
  const wallMap = track(new THREE.CanvasTexture(wallCanvas));
  wallMap.colorSpace = THREE.SRGBColorSpace;
  wallMap.wrapS = wallMap.wrapT = THREE.RepeatWrapping;
  wallMap.repeat.set(10, 3);
  const wallMat = track(new THREE.MeshStandardMaterial({ map: wallMap, roughness: 1, side: THREE.BackSide, color: 0x9a8570 }));
  const wall = new THREE.Mesh(track(new THREE.CylinderGeometry(R * 5.2, R * 5.2, R * 5, 64, 1, true)), wallMat);
  wall.position.set(cx, R * 2.0, cz); wall.receiveShadow = true;
  group.add(wall);

  const pillarGeo = track(new THREE.BoxGeometry(R * 0.32, R * 4, R * 0.32));
  const pillarMat = track(new THREE.MeshStandardMaterial({ map: wallMap, color: 0x3a2e24, roughness: 0.95 }));
  const prnd = rng(99);
  for (let i = 0; i < 9; i++) {
    const a = (i / 9) * Math.PI * 2 + 0.3;
    const rr = R * (2.6 + prnd() * 0.7);
    const p = new THREE.Mesh(pillarGeo, pillarMat);
    p.position.set(cx + Math.cos(a) * rr, R * 1.9 - 9, cz + Math.sin(a) * rr);
    p.rotation.y = a;
    p.receiveShadow = true;
    group.add(p);
  }

  // ---- lights ---------------------------------------------------------------
  const key = new THREE.DirectionalLight(0xffc489, 3.2);
  key.castShadow = true;
  const sm = hi ? 4096 : 2048;
  key.shadow.mapSize.set(sm, sm);
  const sc = key.shadow.camera;
  sc.left = -R * 1.12; sc.right = R * 1.12; sc.top = R * 1.12; sc.bottom = -R * 1.12;
  sc.near = 1; sc.far = R * 8;
  key.shadow.bias = -0.0004; key.shadow.normalBias = 0.04; key.shadow.radius = 3;
  key.target.position.set(cx, 0, cz);
  group.add(key, key.target);
  const hemi = new THREE.HemisphereLight(0xc9b08f, 0x3a2414, 0.24);
  group.add(hemi);
  const ambient = new THREE.AmbientLight(0xa88a6a, 0.14);
  group.add(ambient);

  // sun-leak slit in the chamber wall, on the sun's side
  const slitMat = track(new THREE.MeshBasicMaterial({ color: 0xffb468, transparent: true, opacity: 0.95, fog: false, toneMapped: false }));
  const slit = new THREE.Mesh(track(new THREE.PlaneGeometry(R * 0.28, R * 2.6)), slitMat);
  group.add(slit);
  // volumetric shaft: a long additive prism from the slit toward the table
  const shaftMat = track(new THREE.MeshBasicMaterial({ color: 0xffa650, transparent: true, opacity: 0.075, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide, fog: false, toneMapped: false }));
  const shaft = new THREE.Mesh(track(new THREE.PlaneGeometry(R * 1.5, R * 7)), shaftMat);
  group.add(shaft);

  // braziers: two ember lights on the far side
  const glowTex = createGlowTexture(THREE, [[0, 'rgba(255,200,120,1)'], [0.25, 'rgba(255,140,50,.55)'], [1, 'rgba(255,100,20,0)']]);
  disposables.push(glowTex);
  const braziers = [];
  [[-0.52, -1.05], [0.62, -1.0]].forEach((p, i) => {
    const sp = new THREE.Sprite(track(new THREE.SpriteMaterial({ map: glowTex, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false, fog: false, toneMapped: false })));
    sp.scale.set(R * 0.5, R * 0.5, 1);
    sp.position.set(cx + p[0] * R * 2.2, -3 + i * 0.4, cz + p[1] * R * 2.2);
    group.add(sp);
    const pl = new THREE.PointLight(0xff9a4a, 40, R * 2.6, 1.6);
    pl.position.copy(sp.position); pl.position.y += 0.5;
    group.add(pl);
    braziers.push({ sp, pl, ph: i * 2.1 });
  });

  const dir = new THREE.Vector3();
  let fire = 1;
  const bgCol = new THREE.Color();
  // Apply a blended look (tod.js). Returns it so the scene can set exposure and glow scale.
  function setSun(hour) {
    const s = blendLook(todWeights(hour));
    const az = s.az * Math.PI / 180, el = s.el * Math.PI / 180;
    dir.set(Math.sin(az) * Math.cos(el), Math.sin(el), -Math.cos(az) * Math.cos(el)).normalize();
    key.position.set(cx + dir.x * R * 4, dir.y * R * 4, cz + dir.z * R * 4);
    key.color.setRGB(s.color[0], s.color[1], s.color[2]);
    key.intensity = s.intensity;
    hemi.color.setRGB(...s.hemiSky); hemi.intensity = s.hemi;
    ambient.color.setRGB(...s.ambColor); ambient.intensity = s.amb;
    fire = s.fire;
    // slit on the wall behind the sun, shaft midway
    const wr = R * 5.1, hl = Math.hypot(dir.x, dir.z) || 1;
    slit.position.set(cx + dir.x / hl * wr, R * 1.6, cz + dir.z / hl * wr);
    slit.lookAt(cx, R * 1.6, cz);
    slitMat.color.setRGB(...s.slit); slitMat.opacity = s.slitOp;
    shaftMat.color.setRGB(...s.slit); shaftMat.opacity = s.shaft;
    const mid = new THREE.Vector3(cx + dir.x * R * 2.6, dir.y * R * 2.6, cz + dir.z * R * 2.6);
    shaft.position.copy(mid);
    shaft.lookAt(cx, 0, cz);
    bgCol.setRGB(...s.bg);
    return s;
  }
  setSun(12.5);

  function tick(sec, reduced) {
    braziers.forEach((b) => {
      const f = reduced ? 1 : 0.85 + 0.15 * Math.sin(sec * 7.3 + b.ph) * Math.sin(sec * 3.1 + b.ph * 2);
      b.pl.intensity = 40 * f * fire;
      b.sp.material.opacity = Math.min(1, 0.8 * f * (0.7 + 0.3 * fire));
    });
  }

  function dispose() {
    disposables.forEach((d) => d.dispose && d.dispose());
  }
  return { group, key, hemi, bgCol, setSun, drawPlate, tick, dispose, rimMat, rimTop: top, innerR: rin };
}
