// Shared Bases / Solido state. One $state proxy imported by the route + every
// bases component so the linked-character blueprint sections, the community
// market gallery, and the open 3D preview stay in sync without prop-drilling.
// Mutate the proxy's PROPERTIES in place (never reassign `bases` itself), so the
// shared reference every component holds stays live.
//
// Scope: ALL-linked accounts (V1 parity, contract owner decision #3) — blueprints
// across every linked character, per-character sections; NOT re-scoped to the
// selected char. Reads are session/public + never gated. Writes (publish /
// unpublish / rename / import) are optimistic with rollback: mutate locally, fire
// the CSRF write, and on any throw revert to the pre-write snapshot. Rename is
// OFFLINE-gated server-side; a player_online refusal surfaces as a warn, not a
// hard failure. The publish path re-exports the blueprint server-side (the client
// blob is never trusted). Envelope shapes are frozen in MODULE 3 of the contract.
import { replaceState } from '$app/navigation';
import { api, uuidv4 } from './api.js';

const MARKET_PAGE = 24; // gallery page size (mirrors V1 GALLERY_LIMIT_DEFAULT)

// Same-origin V1 engine + catalog, shared with ThreeDPreview.svelte (one pin,
// one cached module; the edge serves /admin/static un-queried, so the ?v= key
// is what busts it). ASSET_V mirrors portal-solido.js. The publish path renders
// its thumbnail here with no preview modal open, which is why the store owns it.
export const VIEWER_URL = '/admin/static/js/portal-solido-viewer.js?v=20260612q';
export const CATALOG_URL = '/admin/static/js/data/solido-piece-catalog.json';
const THUMB_W = 800;
const THUMB_H = 500;
const THUMB_TIMEOUT_MS = 25000; // hard cap: a slow GLB load never stalls the row

export const bases = $state({
  status: 'idle',        // idle | loading | ready | error  (overview)
  linked: false,         // caller has at least one linked character (derived)
  sections: [],          // [{ account_id, character_name, online, available, blueprints:[{bp_id,name,piece_count,published}] }]
  caps: {                // publish + import are separate rolling-24h buckets
    publish_daily_cap: 0, published_today: 0, rename_name_max: 40,
    import_daily_cap: 0, imported_today: 0,
  },
  flags: { publish_enabled: true, import_enabled: true }, // UX gate (server hard-enforces)
  csrf: '',              // echoed for reference (writes read the cookie directly)

  market: {              // community gallery (public). page is 1-BASED (backend).
    sort: 'new', tag: '',
    page: 0, listings: [], more: false, status: 'idle',
  },

  // One listing's detail page: the card dict ITSELF plus a `status`, so a
  // component reads bases.detail.title / .share_url with no wrapper hop. The
  // card carries no `status` field of its own, which is what makes that safe.
  // `missing` is the 404 envelope: gone, hidden or never published are
  // deliberately ONE state, because telling them apart is what a probe wants.
  detail: { status: 'idle' },  // idle | loading | ready | missing | error
  // The closed filter vocabulary (server-owned). Loaded once per session.
  tagCatalog: { status: 'idle', tags: [], size_bands: [], purposes: [] },
  imports: { status: 'idle', rows: [] },      // the caller's own recent imports
  publishTarget: null,   // blueprint row whose publish/edit modal is open | null

  preview: null,         // { publishId?, bpId?, title, thumbUrl? } open 3D preview target | null
  thumbing: null,        // bp_id whose publish-time thumbnail is rendering | null
  // Admin preview backfill over listings with no thumbnail (see sweepMissingThumbnails).
  sweep: { status: 'idle', total: 0, done: 0, failed: 0 },  // idle | running | done | error
  notice: '',
  noticeTone: 'info',    // info | ok | warn | error
});

function setNotice(text, tone = 'info') {
  bases.notice = text || '';
  bases.noticeTone = tone;
}

/** Boot: pull the overview (per-char blueprint sections with per-blueprint
 *  published state already nested, caps, flags, csrf), then fan out the community
 *  market gallery. Each blueprint row carries its own
 *  `published:{publish_id,download_count}|null` from the backend — consumed as
 *  delivered. `linked` is derived from the presence of sections. */
export async function loadOverview() {
  bases.status = 'loading';
  try {
    const r = await api.bases.overview();
    const sections = Array.isArray(r?.sections) ? r.sections : [];
    bases.sections = sections;
    bases.linked = sections.length > 0;
    bases.caps = {
      publish_daily_cap: Number(r?.caps?.publish_daily_cap) || 0,
      published_today: Number(r?.caps?.published_today) || 0,
      rename_name_max: Number(r?.caps?.rename_name_max) || 40,
      import_daily_cap: Number(r?.caps?.import_daily_cap) || 0,
      imported_today: Number(r?.caps?.imported_today) || 0,
    };
    bases.flags = {
      publish_enabled: r?.flags?.publish_enabled !== false,
      import_enabled: r?.flags?.import_enabled !== false,
    };
    bases.csrf = r?.csrf || '';
    bases.status = 'ready';
    loadMarket();
  } catch (e) {
    bases.status = 'error';
    bases.sections = [];
    bases.linked = false;
  }
}

// Monotonic market token so a slow response can never overwrite a newer filter.
let marketSeq = 0;

/** Replace-load the community gallery from page 1 (applies sort/tag patch). The
 *  backend paginates 1-based; page 0 is treated as "not requested". */
export async function loadMarket(patch = {}) {
  Object.assign(bases.market, patch);
  const seq = ++marketSeq;
  bases.market.status = 'loading';
  try {
    const { sort, tag } = bases.market;
    const r = await api.bases.market({ sort, tag, page: 1 });
    if (seq !== marketSeq) return;
    bases.market.listings = Array.isArray(r?.listings) ? r.listings : [];
    bases.market.more = r?.has_more === true;
    bases.market.page = Number(r?.page) || 1;
    bases.market.status = 'ready';
  } catch (e) {
    if (seq !== marketSeq) return;
    bases.market.listings = [];
    bases.market.more = false;
    bases.market.status = 'error';
  }
}

/** Append the next gallery page (keeps existing listings). 1-based paging. */
export async function loadMoreMarket() {
  if (!bases.market.more || bases.market.status === 'loading') return;
  const seq = ++marketSeq;
  const nextPage = (bases.market.page || 1) + 1;
  bases.market.status = 'loading';
  try {
    const { sort, tag } = bases.market;
    const r = await api.bases.market({ sort, tag, page: nextPage });
    if (seq !== marketSeq) return;
    bases.market.page = Number(r?.page) || nextPage;
    bases.market.listings = [...bases.market.listings, ...(Array.isArray(r?.listings) ? r.listings : [])];
    bases.market.more = r?.has_more === true;
    bases.market.status = 'ready';
  } catch (e) {
    if (seq !== marketSeq) return;
    bases.market.status = 'ready'; // keep what we already have
  }
}

/** Mirror the applied tag into ?tag= so a filtered gallery survives a refresh
 *  and can be shared, the same link a listing's chips already point back at.
 *  replaceState, never a goto: the gallery has already reloaded from the store,
 *  and navigating would re-run the page for a filter that is applied. An empty
 *  tag removes the parameter rather than leaving ?tag= behind. */
function writeTagParam(tag) {
  try {
    const url = new URL(window.location.href);
    if (tag) url.searchParams.set('tag', tag);
    else url.searchParams.delete('tag');
    replaceState(url, {});
  } catch (e) {
    // Router not ready (SSR, or before hydration). The filter is applied either
    // way; only the address bar misses out.
  }
}

/** Apply a tag filter from a chip. Clicking the tag already applied clears it,
 *  so the same chip is both the filter and its undo. Single-select by design
 *  (the backend matches one tag), and always reloads from page 1. */
export function setTag(tag) {
  const next = String(tag || '').trim();
  const applied = next && next !== bases.market.tag ? next : '';
  loadMarket({ tag: applied });
  writeTagParam(applied);
}

// Monotonic detail token: two fast deep-links must not race each other onto the page.
let detailSeq = 0;

/** Load one listing's detail card, spread onto `bases.detail` beside a status.
 *  A 404 lands as `missing` rather than an error, because "unpublished, hidden,
 *  or never existed" is deliberately ONE answer: the route never echoes the
 *  requested id back into visible copy, so a probe learns nothing from the page
 *  it gets. */
export async function loadDetail(id) {
  const seq = ++detailSeq;
  bases.detail = { status: 'loading' };
  try {
    const r = await api.bases.detail(id);
    if (seq !== detailSeq) return;
    if (!r || r.ok === false || !r.listing) { bases.detail = { status: 'missing' }; return; }
    bases.detail = { status: 'ready', ...r.listing };
  } catch (e) {
    if (seq !== detailSeq) return;
    bases.detail = e?.status === 404 ? { status: 'missing' } : { status: 'error' };
  }
}

/** The closed filter vocabulary the chip rail and the publish modal render.
 *  Loaded once: it changes when we ship, not while a player is browsing. */
export async function loadTagCatalog() {
  if (bases.tagCatalog.status === 'loading' || bases.tagCatalog.status === 'ready') return;
  bases.tagCatalog.status = 'loading';
  try {
    const r = await api.bases.tags();
    bases.tagCatalog = {
      status: 'ready',
      tags: Array.isArray(r?.tags) ? r.tags : [],
      size_bands: Array.isArray(r?.size_bands) ? r.size_bands : [],
      purposes: Array.isArray(r?.purposes) ? r.purposes : [],
    };
  } catch (e) {
    // An unreachable catalog seals the rail rather than falling back to a
    // client-side list, which would offer filters the market cannot answer.
    bases.tagCatalog = { status: 'error', tags: [], size_bands: [], purposes: [] };
  }
}

/** The caller's own recent imports (session-scoped, newest first). */
export async function loadImports() {
  bases.imports.status = 'loading';
  try {
    const r = await api.bases.imports();
    bases.imports = { status: 'ready', rows: Array.isArray(r?.rows) ? r.rows : [] };
  } catch (e) {
    bases.imports = { status: 'error', rows: [] };
  }
}

/** Open / close the publish-or-edit modal over one of the caller's own bases.
 *  `bp` is the blueprint row itself, so the modal reads its name and published
 *  state without a second lookup. */
export function openPublish(bp) { bases.publishTarget = bp || null; }
export function closePublish() { bases.publishTarget = null; }

/** Find a blueprint row across all sections by bp_id (returns {section, bp} or null). */
function findBlueprint(bpId) {
  for (const section of bases.sections) {
    const bp = (section.blueprints || []).find((b) => String(b.bp_id) === String(bpId));
    if (bp) return { section, bp };
  }
  return null;
}

/** Render the just-published base offscreen (real meshes, textured) and upload
 *  the PNG so the gallery card is not blank. V1 parity (portal-solido.js
 *  generateThumb): best-effort and hard-capped — every failure is swallowed
 *  here, so this can never reject the publish it follows. */
// `loadBlob` (optional) replaces the own-base blob source: the admin sweep
// passes the PUBLIC listing blob because it renders other players' bases.
// Resolves true only when the snapshot was uploaded.
async function renderThumbnail(bpId, publishId, loadBlob = null) {
  const canvas = document.createElement('canvas');
  canvas.style.cssText = `position:fixed;left:-10000px;top:0;width:${THUMB_W}px;height:${THUMB_H}px;`;
  document.body.appendChild(canvas);

  let handle = null;
  let ok = false;
  // Safe to call twice: the timeout path cleans up first, and a render that
  // lands after the cap still disposes its own handle.
  function cleanup() {
    if (handle) { try { handle.dispose(); } catch (e) {} handle = null; }
    canvas.remove();
  }

  const work = (async () => {
    try {
      const [blob, cat, mod] = await Promise.all([
        loadBlob ? loadBlob() : api.bases.ownBlueprint(bpId),
        fetch(CATALOG_URL, { credentials: 'same-origin' }).then((r) => r.json()),
        import(/* @vite-ignore */ VIEWER_URL),
      ]);
      if (!blob || blob.available === false) throw new Error('no blueprint');
      handle = mod.mount(canvas, blob, cat);
      const manifest = await handle.getManifest();
      if (!manifest || !manifest.pieces) throw new Error('no manifest');
      await handle.setTextured(true);
      await handle.switchMode('real', manifest);
      // One settle tick so freshly-uploaded textures land on the GPU.
      await new Promise((r) => setTimeout(r, 300));
      const png_base64 = (handle.snapshot(THUMB_W, THUMB_H) || '').split(',')[1];
      if (!png_base64) throw new Error('empty snapshot');
      await api.bases.thumbnail(publishId, { png_base64 });
      ok = true;
    } catch (e) {
      if (window.console) console.warn('thumbnail generation failed', e);
    } finally {
      cleanup();
    }
  })();

  await Promise.race([work, new Promise((r) => setTimeout(r, THUMB_TIMEOUT_MS))]);
  cleanup();
  return ok;
}

/** Admin backfill: render a preview for every published listing that has none.
 *  Listings published before publish-time thumbnails existed have no owner
 *  action left that would render one, so an admin session renders them here
 *  from the PUBLIC blob and uploads through the admin lane of the thumbnail
 *  route (the server checks the role; the button is only a convenience gate).
 *  Sequential on purpose: one WebGL context at a time, and a failure counts
 *  instead of stopping the sweep. */
export async function sweepMissingThumbnails() {
  if (bases.sweep.status === 'running') return;
  bases.sweep = { status: 'running', total: 0, done: 0, failed: 0 };
  try {
    const targets = [];
    for (let page = 1; page <= 50; page++) {
      const r = await api.bases.market({ sort: 'new', tag: '', page });
      for (const l of (Array.isArray(r?.listings) ? r.listings : [])) {
        if (!l.thumb_url && l.publish_id != null) targets.push(l.publish_id);
      }
      if (r?.has_more !== true) break;
    }
    bases.sweep.total = targets.length;
    for (const pid of targets) {
      const ok = await renderThumbnail(null, pid, () => api.bases.blueprint(pid));
      if (ok) {
        bases.sweep.done += 1;
        const url = `/portal/solido/${pid}/thumb.png?t=${Date.now()}`;
        const row = bases.market.listings.find((l) => l.publish_id === pid);
        if (row) row.thumb_url = url;
        if (bases.detail.publish_id === pid) bases.detail.thumb_url = url;
      } else {
        bases.sweep.failed += 1;
      }
    }
    bases.sweep.status = 'done';
  } catch (e) {
    bases.sweep.status = 'error';
  }
}

/** Publish one of the caller's OWN bases to the community market, or update the
 *  listing it already has: one endpoint, one body, because `create_or_update`
 *  updates in place on (account, blueprint) and keeps the download counter. The
 *  server re-exports the blueprint (never trusts a client blob); title,
 *  description and purpose tags are the player's and are sent as given.
 *  Optimistically flags the row published; reconciles from the response and
 *  rolls back on any throw. The gallery thumbnail is rendered after the listing
 *  exists, inside its own try/catch so a failed render can never trigger the
 *  rollback -- and it is re-rendered on an update too, because the update
 *  re-exported the base and the old shot may no longer be the base. */
export async function publishListing({ game_bp_id, title = '', description = '', tags = [] } = {}) {
  const hit = findBlueprint(game_bp_id);
  if (!hit) return false;
  const { bp } = hit;
  const prev = bp.published;
  const editing = !!prev;
  bp.published = {
    publish_id: prev?.publish_id ?? null,
    download_count: prev?.download_count ?? 0,
    pending: true,
  };
  try {
    const r = await api.bases.publish({
      game_bp_id, title, description, tags, client_uuid: uuidv4(),
    });
    bp.published = {
      publish_id: r?.publish_id ?? prev?.publish_id ?? null,
      download_count: Number(r?.download_count) || 0,
      // What we just wrote, so re-opening the modal prefills from the row
      // instead of refetching the overview to learn its own last write.
      title, description, user_tags: tags,
    };
    setNotice(editing
      ? `Updated ${bp.name || 'base'} on the market.`
      : `Published ${bp.name || 'base'} to the market.`, 'ok');
    const pid = bp.published.publish_id;
    if (pid != null) {
      bases.thumbing = game_bp_id;
      try {
        await renderThumbnail(game_bp_id, pid);
      } catch (e) {
        // Best-effort: the listing is already live, a missing thumb is cosmetic.
      } finally {
        bases.thumbing = null;
      }
    }
    return true;
  } catch (e) {
    bp.published = prev; // rollback
    setNotice(friendly(e, editing
      ? 'Could not update that listing. Nothing changed.'
      : 'Could not publish that base. Nothing changed.'), 'error');
    return false;
  }
}


/** Unpublish one of the caller's listings. Optimistically clears the published
 *  flag; rolls back on any throw. */
export async function unpublish(bpId, publishId) {
  const hit = findBlueprint(bpId);
  if (!hit) return false;
  const { bp } = hit;
  const prev = bp.published;
  const pid = publishId ?? bp.published?.publish_id;
  bp.published = null;
  try {
    await api.bases.unpublish({ publish_id: pid });
    setNotice('Listing removed from the market.', 'ok');
    return true;
  } catch (e) {
    bp.published = prev; // rollback
    setNotice(friendly(e, 'Could not unpublish that listing. Nothing changed.'), 'error');
    return false;
  }
}

/** Rename one of the caller's OWN bases (OFFLINE-gated server-side). Optimistic
 *  on the display name; a player_online refusal reverts and warns (soft gate). */
export async function rename(bpId, name) {
  const clean = String(name || '').replace(/\s+/g, ' ').trim();
  if (!clean) { setNotice('Enter a name.', 'warn'); return false; }
  const hit = findBlueprint(bpId);
  if (!hit) return false;
  const { bp } = hit;
  const prev = bp.name;
  if (clean === prev) return false;
  bp.name = clean;
  try {
    const r = await api.bases.rename({ bp_id: bpId, name: clean, client_uuid: uuidv4() });
    bp.name = r?.name || clean;
    setNotice('Base renamed.', 'ok');
    return true;
  } catch (e) {
    bp.name = prev; // rollback
    // 409 player_online is an honest offline gate (soft warn), not a failure.
    const soft = e?.status === 409 || (e && e.message) === 'player_online';
    setNotice(renameError(e), soft ? 'warn' : 'error');
    return false;
  }
}

/** Import a public link or private listing to a linked character. Not optimistic
 *  (no local list to mutate); returns the server envelope for the dialog to
 *  surface. The dialog mints client_uuid ONCE per logical send and reuses it on
 *  retry (idempotency), so this passes the body straight through. Throws propagate
 *  as a caught error the dialog renders (409 online+backpack, 429 cap). */
export async function importBlueprint(body) {
  return api.bases.import(body);
}

/** Open / close the 3D preview. `target` = {publishId} (market listing) or
 *  {bpId} (own base) + a title for the dialog header. Extra keys ride through
 *  untouched (the viewer reads `thumbUrl` for its tap-to-load plate). */
export function openPreview(target) { bases.preview = target || null; }
export function closePreview() { bases.preview = null; }

/** Map a rename error (status code first, then token) to friendly copy. Backend
 *  rename refusals: 409 player_online, 404 not_owned, 502 relay_error. */
function renameError(e) {
  const t = (e && e.message) || '';
  const status = e && e.status;
  if (status === 409 || t === 'player_online') return 'Log out of the game first. Base names only update while you are offline. Nothing changed.';
  if (status === 404 || t === 'not_owned') return 'That base does not belong to your linked characters.';
  if (status === 502 || t === 'relay_error') return 'The game server did not respond. Try again shortly.';
  const map = {
    no_player: 'We could not find your in-game character right now. Try again shortly.',
    no_struct: "That base can't be renamed (unexpected item data).",
    empty_name: 'Enter a name.',
    name_too_long: 'That name is too long. Shorten it and try again.',
  };
  return map[t] || 'The rename did not complete. Nothing changed.';
}

/** Map a generic write error (status code first, then token) to friendly copy. */
function friendly(e, fallback) {
  const t = (e && e.message) || '';
  const status = e && e.status;
  if (status === 429 || t === 'rate_limited') return 'Daily publish limit reached. Try again tomorrow.';
  if (status === 413) return 'That base is too large to publish.';
  if (status === 404) return 'Blueprint not found for your linked characters.';
  if (status === 409 || t === 'player_online') return 'Log out of the game first. Nothing changed.';
  return fallback;
}

/** Load everything (called once auth resolves to authed). */
export function loadAll() {
  loadOverview();
}
