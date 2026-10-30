// Garment colours for the 3D gear stage. PURE: no three.js, no fetch, so the
// suite runs every rule under node.
//
// A garment material has up to 12 layers. The layer under a pixel comes from the
// material's index map (R channel = layer 0..11, sampled at UV1). Each layer
// carries {cat, ovr, tint} from /portal/gear/layers:
//   cat   0 = never dyed, 1..5 = takes dye cat-1 of the equipped swatch
//   ovr   the layer's own undyed colour (linear RGB), or null
//   tint  a multiplier (linear RGB), or null = white
// All colours are LINEAR 0..1, which is what the shader wants.

export const LAYERS = 12;
export const NEUTRAL = [0.5, 0.5, 0.5];

/** The five dyes of a swatch-lut entry as [r,g,b,a]. The LUT carries no alpha
 *  today; a dye is only applied when its alpha is 1, so absent means 1. */
export function swatchDyes(lut, swatchId) {
  const entry = swatchId && lut && lut.swatches ? lut.swatches[swatchId] : null;
  if (!entry || !Array.isArray(entry.rgb)) return null;
  return entry.rgb.map((c, i) => [c[0], c[1], c[2], entry.alpha ? entry.alpha[i] : 1]);
}

/** One layer's colour: the dye when the layer takes one and that dye is on,
 *  otherwise the authored undyed colour, otherwise neutral grey; then the tint. */
export function layerColor(layer, dyes) {
  if (!layer) return NEUTRAL.slice();
  const dye = layer.cat > 0 && dyes ? dyes[layer.cat - 1] : null;
  const on = dye && Math.abs((dye[3] ?? 1) - 1) <= 1e-6;
  const base = on ? dye : (layer.ovr || NEUTRAL);
  const tint = layer.tint || [1, 1, 1];
  return [base[0] * tint[0], base[1] * tint[1], base[2] * tint[2]];
}

/** The shader's uLayer[12], flattened to 36 floats. */
export function layerUniforms(entry, dyes) {
  const out = new Float32Array(LAYERS * 3);
  const layers = (entry && entry.layers) || [];
  for (let i = 0; i < LAYERS; i++) {
    const c = layerColor(layers[i] || null, dyes);
    out[i * 3] = c[0]; out[i * 3 + 1] = c[1]; out[i * 3 + 2] = c[2];
  }
  return out;
}

/** How to colour one material: 'layered' (per-layer shader) only when the layer
 *  data exists AND its index map has been verified; anything else gets the old
 *  single representative tint. */
export function colourMode(material, gearLayers) {
  const entry = gearLayers && gearLayers.materials ? gearLayers.materials[material] : null;
  if (!entry) return { mode: 'tint' };
  const meta = gearLayers._meta || {};
  const idmap = entry.idmap || meta.default_idmap;
  const verified = (meta.idmaps_verified || []).includes(idmap);
  if (!verified || entry.index_mode === 'uv') return { mode: 'tint' };
  return { mode: 'layered', entry, idmap };
}

/** The single tint used where the layered shader does not apply. */
export function reprTint(lut, swatchId) {
  const entry = swatchId && lut && lut.swatches ? lut.swatches[swatchId] : null;
  const rgb = entry && (entry.repr_rgb || entry.primary_rgb);
  return Array.isArray(rgb) && rgb.length === 3 ? rgb : null;
}

/** body_type from the equipped feed -> mesh body. 1 = male and 2 = female is
 *  inferred, not yet confirmed; absent or unknown is female, which is the body
 *  most baked sets have. */
export function bodyFor(bodyType) {
  return bodyType === 1 ? 'male' : 'female';
}

/** Which mesh and which of its materials to show for each equipped garment.
 *  Items of the same set share one mesh (union of their slots). Returns
 *  [{set, body, file, hash, materials: [names], swatches: {material: swatchId}}]. */
export function stagePlan(items, manifest, bodyType) {
  const want = bodyFor(bodyType);
  const bySet = new Map();
  for (const it of items || []) {
    const tpl = manifest && manifest.templates ? manifest.templates[it.template] : null;
    const set = tpl ? manifest.sets[tpl.set] : null;
    if (!set) continue;
    const body = set[want] ? want : (want === 'male' ? 'female' : 'male');
    const mesh = set[body];
    if (!mesh) continue;
    const mats = (mesh.slots && mesh.slots[it.slot]) || [];
    if (!mats.length) continue;
    let plan = bySet.get(tpl.set);
    if (!plan) {
      plan = { set: tpl.set, body, file: mesh.file, hash: mesh.hash, materials: [], swatches: {} };
      bySet.set(tpl.set, plan);
    }
    for (const m of mats) {
      if (!plan.materials.includes(m)) plan.materials.push(m);
      plan.swatches[m] = it.swatch_id || '';
    }
  }
  return [...bySet.values()];
}

/** Linear -> sRGB hex, for the colour keys next to the stage. */
export function linearToHex(rgb) {
  return rgb.map((v) => {
    const c = Math.min(1, Math.max(0, v));
    const s = c <= 0.0031308 ? c * 12.92 : 1.055 * Math.pow(c, 1 / 2.4) - 0.055;
    return Math.round(s * 255).toString(16).padStart(2, '0');
  }).join('').toUpperCase();
}
