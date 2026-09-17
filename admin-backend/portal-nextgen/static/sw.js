// Last Sietch Companion V2 service worker. Scope: / (the app is the whole root
// of portal.lastsietch.com).
// Content-hashed build assets (/_app/immutable/) are cache-first forever
// (filenames change on rebuild, so this self-heals). Navigations are
// network-first with an offline fallback to the cached shell, and the app's own
// static files are network-first with a cache fallback.
// Minimal + self-hosted (CSP script-src 'self'); not a full offline app.
const CACHE = 'ls-v2-shell-v128';
const SHELL_URL = '/';

// Paths this worker must NEVER answer for. Under the old /portal/v2 scope the
// browser excluded them by construction; at the root nothing does, so the rule
// is written out instead. The portal pages and their OAuth flow, the JSON API,
// the admin app, the shared static mount and the site assets all belong to the
// server, and a replayed or cached response to any of them would hand a player
// a stale session or print yesterday's numbers as live data.
const NEVER = ['/portal', '/api', '/admin', '/static', '/assets'];

// A prefix has to match the BARE path as well as the path plus a slash. /portal
// with no trailing slash is the Classic landing page a player reaches from the
// Classic link, and a startsWith('/portal/') test would sail straight past it
// and hand it to the worker. Matching on the segment boundary rather than on the
// raw prefix also keeps a path that merely starts with the same letters (an
// /apidocs, say) out of the list.
const isServerPath = (path) => NEVER.some((p) => path === p || path.startsWith(p + '/'));

// The app's own static files, served at the root by the same rewrite that serves
// the shell. Prefixes, so /icon-192.png and /icon-512-maskable.png are one entry.
const APP_STATIC = [
  '/fonts/', '/img/', '/glb/', '/terrain/',
  '/icon-', '/favicon.png', '/og-fremkit.jpg', '/manifest.webmanifest',
];

// Module static assets worth precaching so the shell serves them offline and they
// cache-bust on activation. Item/container icons and the Solari coin crest are
// served from the shared /admin static mount, which NEVER lists, so they are not
// precached here; the browser HTTP cache covers those. The GLB hero is
// intentionally NOT precached (it is large + lazy, high-tier only); it
// runtime-caches on demand.
const PRECACHE = [
  '/img/dune-icons/T_UI_IconChoam_StorageContainer_Medium_D.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(PRECACHE)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  if (isServerPath(url.pathname)) return;

  // Immutable hashed assets: cache-first.
  if (url.pathname.startsWith('/_app/immutable/')) {
    e.respondWith(
      caches.open(CACHE).then((c) =>
        c.match(req).then((hit) =>
          hit ||
          fetch(req).then((res) => {
            if (res && res.status === 200) c.put(req, res.clone());
            return res;
          })
        )
      )
    );
    return;
  }

  // Navigations: network-first, cache the shell for offline.
  if (req.mode === 'navigate') {
    e.respondWith(
      fetch(req)
        .then((res) => {
          // Only a real HTML page may become the shell. A navigation that came
          // back as JSON or as an image would otherwise be served as the app
          // itself on the one request nobody can retry: the first load with no
          // network.
          const type = res && res.headers ? res.headers.get('content-type') || '' : '';
          if (res && res.status === 200 && type.includes('text/html')) {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put(SHELL_URL, copy));
          }
          return res;
        })
        .catch(() => caches.match(req).then((hit) => hit || caches.match(SHELL_URL)))
    );
    return;
  }

  // The app's own static files: network-first, cache fallback. A miss resolves to
  // an error response rather than to undefined, which is the same failure a plain
  // offline fetch gives without the console noise of an empty respondWith.
  if (APP_STATIC.some((p) => url.pathname.startsWith(p))) {
    e.respondWith(
      fetch(req)
        .then((res) => {
          if (res && res.status === 200) {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put(req, copy));
          }
          return res;
        })
        .catch(() => caches.match(req).then((hit) => hit || Response.error()))
    );
    return;
  }

  // Anything else on this origin belongs to the server: not intercepted.
});
