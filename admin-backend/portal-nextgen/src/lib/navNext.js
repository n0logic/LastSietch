// P3 navigation for the redesigned shell (shell.next): five destinations on the
// rail, utilities in the topbar, and each section's pages as tabs.
//
// PURE. No store, no fetch, no window: the shell hands in the path, the query,
// the public feature map and the clock, and this decides. The suite runs it under
// node.
//
// Owner rules (2026-10-30): no dropdowns and no fly-outs. A rail item goes to its
// section's landing tab; the section's other pages are tabs on that page. The
// active tab lives in the URL, so a deep link and back/forward land on the right
// tab. A tab may carry views (a second, smaller strip) where the parity audit
// folds an old page into another one (Landsraad > Standings, Bases > Preview).
//
// Paths here are app paths without the deploy base; the components prefix it.
//
// A leaf (tab or view) matches:
//   * its own href's path, and, when the href carries ?tab=, only that tab value;
//   * any of `paths` (prefix rule), whatever the query says.
// The best match wins: a ?tab= match beats a bare path, and a longer path beats a
// shorter one, so /storage/workshop is Workshop and not Storage. An unknown or
// gated ?tab= matches nothing extra, so it falls back to the landing tab.

export const SECTIONS = [
  { id: 'home', label: 'Home', rail: true, tabs: [
    { id: 'home', label: 'Home', href: '/', exact: true },
  ] },
  { id: 'desert', label: 'Desert', rail: true, tabs: [
    { id: 'maps', label: 'Maps', href: '/maps', paths: ['/desert'] },
    { id: 'world', label: 'Sand Table', href: '/world' },
  ] },
  { id: 'trade', label: 'Trade', rail: true, badge: 'alerts', tabs: [
    { id: 'exchange', label: 'Exchange', href: '/exchange' },
    { id: 'karum', label: 'Karum', href: '/karum' },
    { id: 'orders', label: 'My orders', href: '/exchange?tab=orders', paths: ['/economy'], badge: 'alerts', render: 'orders' },
  ] },
  { id: 'sietch', label: 'Sietch', rail: true, tabs: [
    { id: 'events', label: 'Events', href: '/events', views: [
      { id: 'calendar', label: 'Calendar', href: '/events' },
      // Seven Nights stays reachable while it runs; the Events page also carries
      // its feature card. After the Stall closes /sietch redirects to /events.
      { id: 'seven-nights', label: 'Seven Nights', href: '/sietch', feature: 'sietch', until: '2026-11-04T00:00:00Z' },
      { id: 'almanac', label: 'Almanac', href: '/reports', feature: 'reports' },
    ] },
    { id: 'sietches', label: 'Sietches', href: '/sietches', views: [
      { id: 'habbanya', label: 'Habbanya', href: '/sietches' },
      { id: 'amtal', label: 'Amtal', href: '/sietches?tab=amtal', paths: ['/amtal'], feature: 'amtal' },
    ] },
    { id: 'guilds', label: 'Guilds', href: '/guilds' },
    { id: 'landsraad', label: 'Landsraad', href: '/landsraad', views: [
      { id: 'board', label: 'Term board', href: '/landsraad' },
      { id: 'standings', label: 'Standings', href: '/landsraad?tab=standings', paths: ['/standings'], render: 'standings' },
    ] },
  ] },
  { id: 'you', label: 'You', rail: true, tabs: [
    { id: 'character', label: 'Character', href: '/character' },
    { id: 'storage', label: 'Storage', href: '/storage' },
    { id: 'workshop', label: 'Workshop', href: '/storage/workshop', paths: ['/storage/augments'] },
    { id: 'bases', label: 'Bases', href: '/bases', views: [
      { id: 'blueprints', label: 'Blueprints', href: '/bases' },
      { id: 'preview', label: 'Preview', href: '/bases?tab=preview', paths: ['/solido'], render: 'preview' },
    ] },
    { id: 'rewards', label: 'Rewards', href: '/rewards' },
    { id: 'settings', label: 'Settings', href: '/settings' },
  ] },
  // Utilities: topbar buttons, not rail destinations. Inbox and Help still get a
  // tab strip on their own pages; Chat is one page.
  { id: 'chat', label: 'Chat', utility: true, tabs: [
    { id: 'chat', label: 'Chat', href: '/chat' },
  ] },
  { id: 'inbox', label: 'Inbox', utility: true, tabs: [
    { id: 'mail', label: 'Mail', href: '/mailbox' },
    { id: 'deliveries', label: 'Deliveries', href: '/mailbox?tab=deliveries', render: 'deliveries' },
    { id: 'activity', label: 'Activity', href: '/mailbox?tab=activity', paths: ['/activity'], render: 'activity' },
  ] },
  { id: 'help', label: 'Help', utility: true, tabs: [
    { id: 'help', label: 'Help', href: '/help' },
    { id: 'rules', label: 'Rules', href: '/rules' },
    { id: 'start', label: 'First session', href: '/start' },
  ] },
];

// Old URLs the redesign retires. Only the new shell follows these; the shipped
// shell keeps every page where it was. `after` holds a redirect until a moment.
export const REDIRECTS = [
  { from: '/desert', to: '/maps' },
  { from: '/economy', to: '/exchange?tab=orders' },
  { from: '/standings', to: '/landsraad?tab=standings' },
  { from: '/solido', to: '/bases?tab=preview' },
  { from: '/activity', to: '/mailbox?tab=activity' },
  { from: '/amtal', to: '/sietches?tab=amtal' },
  { from: '/sietch', to: '/events', after: '2026-11-04T00:00:00Z' },
];

function splitHref(href) {
  const at = href.indexOf('?');
  if (at < 0) return { path: href, tab: null };
  return { path: href.slice(0, at), tab: new URLSearchParams(href.slice(at + 1)).get('tab') };
}

function normalPath(path) {
  if (typeof path !== 'string' || !path) return '/';
  return path.length > 1 && path.endsWith('/') ? path.replace(/\/+$/, '') || '/' : path;
}

function pathMatches(path, p, exact) {
  if (exact || p === '/') return path === p;
  return path === p || path.startsWith(p + '/');
}

/** True when a leaf is on for this feature map at this moment. Fails closed on
 *  an unread map, like the shipped nav. */
export function leafVisible(leaf, features, now) {
  if (leaf.feature && !(features && features[leaf.feature] === true)) return false;
  if (leaf.until && !(now < Date.parse(leaf.until))) return false;
  return true;
}

export function visibleViews(tab, features, now) {
  return (tab.views || []).filter((v) => leafVisible(v, features, now));
}

export function visibleTabs(section, features, now) {
  return section.tabs.filter((t) => leafVisible(t, features, now));
}

function score(leaf, path, tab) {
  let best = 0;
  const own = splitHref(leaf.href);
  if (pathMatches(path, own.path, leaf.exact)) {
    if (own.tab) { if (tab === own.tab) best = 3000 + own.path.length; }
    else best = 1000 + own.path.length;
  }
  for (const p of leaf.paths || []) {
    if (pathMatches(path, p, false)) best = Math.max(best, 1000 + p.length);
  }
  return best;
}

/** {section, tab, view} for a location, or all null when nothing in the nav
 *  owns it (the login page, staff tools). `view` is null for a tab without views. */
export function locate(path, search, features, now) {
  const p = normalPath(path);
  const tabParam = new URLSearchParams(search || '').get('tab');
  let found = { section: null, tab: null, view: null };
  let best = 0;
  for (const section of SECTIONS) {
    for (const tab of visibleTabs(section, features, now)) {
      const views = visibleViews(tab, features, now);
      const leaves = tab.views ? views : [tab];
      for (const leaf of leaves) {
        const s = score(leaf, p, tabParam);
        if (s > best) {
          best = s;
          found = { section, tab, view: tab.views ? leaf : null };
        }
      }
    }
  }
  return found;
}

/** The page that stands in for the route at this location, or null when the
 *  route renders itself. Only a ?tab= view of another page has one. */
export function renderFor(loc) {
  const leaf = loc.view || loc.tab;
  return leaf && leaf.render ? leaf.render : null;
}

/** Where a rail item goes: the section's landing tab, at its landing view. */
export function landingHref(section) {
  return section.tabs[0].href;
}

export function railSections() {
  return SECTIONS.filter((s) => s.rail);
}

/** The redirect target for a retired URL, query kept, or null. The target's own
 *  ?tab= wins over one the old link carried. */
export function redirectFor(path, search, now) {
  const p = normalPath(path);
  const rule = REDIRECTS.find((r) => r.from === p && (!r.after || now >= Date.parse(r.after)));
  if (!rule) return null;
  const { path: to, tab } = splitHref(rule.to);
  const params = new URLSearchParams(search || '');
  if (tab) params.set('tab', tab);
  const q = params.toString();
  return q ? `${to}?${q}` : to;
}

/** The nav in the shape the Reach search indexes ({group, items}). Every tab
 *  and view a player can reach is searchable. */
export function searchSections(features, now) {
  return SECTIONS.map((section) => ({
    group: section.label,
    items: visibleTabs(section, features, now).flatMap((tab) => {
      const views = visibleViews(tab, features, now);
      return tab.views ? views.map((v, i) => ({ label: i === 0 ? tab.label : v.label, href: v.href, v2: true }))
        : [{ label: tab.label, href: tab.href, v2: true }];
    }),
  }));
}
