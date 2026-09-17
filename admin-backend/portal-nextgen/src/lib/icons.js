// Resolve a dune-icon basename (what the JSON API returns in item.icon /
// container.icon, e.g. "T_UI_IconResourceMagnetite_D") into a full same-origin
// URL. The full icon library already lives under the admin static mount at
// /admin/static/img/dune-icons/<basename>.png (same origin as the app, so
// CSP img-src 'self' covers it) - we reuse it rather than copy hundreds of PNGs
// into the V2 bundle. An empty/missing basename falls back to the unknown-item
// icon so a cell always renders something.
const BASE = '/admin/static/img/dune-icons/';
const FALLBACK = BASE + 'T_UI_IconItemUnknownS_D.png';

export function iconUrl(basename) {
  if (!basename) return FALLBACK;
  // Basenames arrive without an extension; tolerate one already being present.
  return BASE + (String(basename).endsWith('.png') ? basename : `${basename}.png`);
}

// Container-tile icons live in a SEPARATE mount (/admin/static/img/containers/,
// e.g. "container-bank", "vehicle-buggy"), NOT the dune-icons library. The JSON
// API returns a container_icons basename in container.icon (or null). Resolve it
// against that mount; a missing/unmapped basename falls back to the generic
// storage-container glyph so a tile never shows a broken image.
const CONTAINER_BASE = '/admin/static/img/containers/';
export const CONTAINER_FALLBACK = CONTAINER_BASE + 'container-storage.png';

export function containerIconUrl(basename) {
  if (!basename) return CONTAINER_FALLBACK;
  return CONTAINER_BASE + (String(basename).endsWith('.png') ? basename : `${basename}.png`);
}

// Character stat glyphs (Solari, Intel, Scrip, spec tracks, faction-standing)
// live on the admin static mount at /admin/static/img/stats/<slug>.png (same
// origin as the app, so CSP img-src 'self' covers it). The Character JSON
// returns a stat SLUG (e.g. "intel", "spec-combat"); resolve it against that
// mount. A missing/unmapped slug returns null so the caller can omit the glyph
// rather than render a broken image.
const STAT_BASE = '/admin/static/img/stats/';
export function statIconUrl(slug) {
  if (!slug) return null;
  return STAT_BASE + (String(slug).endsWith('.png') ? slug : `${slug}.png`);
}

// Great-house faction crests (Atreides / Harkonnen) on /admin/static/img/
// factions/<crest>.png. The JSON returns a crest slug (e.g. "atreides" or
// "atreides-banner"); resolve it against that mount. Null when absent.
const FACTION_BASE = '/admin/static/img/factions/';
export function factionCrestUrl(crest) {
  if (!crest) return null;
  return FACTION_BASE + (String(crest).endsWith('.png') ? crest : `${crest}.png`);
}

// Minor-house (Landsraad) crests on /admin/static/img/houses/<crest>.png. The
// board/rewards JSON returns a crest slug per house (e.g. "ecaz"); resolve it
// against that mount. Null when absent so the tile falls back to a monogram.
const HOUSE_BASE = '/admin/static/img/houses/';
export function houseCrestUrl(crest) {
  if (!crest) return null;
  return HOUSE_BASE + (String(crest).endsWith('.png') ? crest : `${crest}.png`);
}
