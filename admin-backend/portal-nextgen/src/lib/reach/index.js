export const MAPS = [
  { label: 'Habbanya', detail: 'Hagga Basin · PvE', href: '/maps/hagga?inst=habbanya', plate: 'habbanya' },
  { label: 'Kulon', detail: 'Hagga Basin · PvP', href: '/maps/hagga?inst=kulon', plate: 'kulon' },
  { label: 'Amtal', detail: 'Full PvP · untracked', href: '/maps/hagga?inst=amtal', plate: 'amtal' },
  { label: 'Deep Desert · PvE', detail: 'Spice, storms and the survey grid', href: '/maps/deep-desert?inst=pve', plate: 'dd-pve' },
  { label: 'Deep Desert · PvP', detail: 'The contested sand', href: '/maps/deep-desert?inst=pvp', plate: 'dd-pvp' },
  { label: 'Arrakeen', detail: 'Social hub', href: '/maps/arrakeen' },
  { label: 'Harko Village', detail: 'Social hub', href: '/maps/harko-village' },
].map((map) => ({ ...map, kind: 'Map' }));

export function routeIndex(sections) {
  const seen = new Set();
  return sections.flatMap((section) => [
    ...(section.hub ? [{ label: section.group, href: section.hub }] : []),
    ...section.items,
  ].map((item) => ({ ...item, kind: 'Page', detail: section.group })))
    .concat([
      { label: 'Settings', href: '/settings', kind: 'Page', detail: 'Characters, appearance and privacy' },
      { label: 'Mailbox', href: '/mailbox', kind: 'Page', detail: 'Messages and deliveries' },
      { label: 'Help', href: '/help', kind: 'Page', detail: 'Guides and changelog' },
      { label: 'Server rules', href: '/rules', kind: 'Page', detail: 'Community rules' },
      ...MAPS,
    ]).filter((item) => !seen.has(item.href) && seen.add(item.href));
}

export function searchIndex(items, query) {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  return items.filter((item) => words.every((word) =>
    `${item.label} ${item.detail || ''} ${item.kind}`.toLowerCase().includes(word)))
    .sort((a, b) => Number(b.label.toLowerCase().startsWith(query.toLowerCase()))
      - Number(a.label.toLowerCase().startsWith(query.toLowerCase())));
}

export function rememberPath(paths, href) {
  return [href, ...(Array.isArray(paths) ? paths : []).filter((path) => path !== href)].slice(0, 12);
}
