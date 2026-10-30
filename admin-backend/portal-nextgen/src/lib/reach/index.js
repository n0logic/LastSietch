export const MAPS = [
  { label: 'Habbanya', detail: 'Hagga Basin · PvE', href: '/maps/hagga?inst=habbanya', plate: 'habbanya' },
  { label: 'Amtal', detail: 'Full PvP · untracked', href: '/maps/hagga?inst=amtal', plate: 'amtal' },
  { label: 'Deep Desert · PvE', detail: 'Spice, storms and the survey grid', href: '/maps/deep-desert?inst=pve', plate: 'dd-pve' },
  { label: 'Deep Desert · PvP', detail: 'The contested sand', href: '/maps/deep-desert?inst=pvp', plate: 'dd-pvp' },
  { label: 'Arrakeen', detail: 'Social hub', href: '/maps/arrakeen' },
  { label: 'Harko Village', detail: 'Social hub', href: '/maps/harko-village' },
].map((map) => ({ ...map, kind: 'Map' }));

// Sand Table stones the Reach can jump to by name (ids = overmap-places.json locations).
export const STONES = [
  ['hagga-basin', 'Hagga Basin'], ['deep-desert', 'Deep Desert'], ['arrakeen', 'Arrakeen'], ['harko-village', 'Harko Village'],
  ['arrakeen-spaceport', 'Arrakeen Spaceport'], ['fallen-light', 'Fallen Light'], ['neo-carthag-arena', 'Neo Carthag Arena'],
  ['ruins-of-old-carthag', 'Ruins of Old Carthag'], ['water-shippers-mansion', 'Water Shipper\'s Mansion'],
  ['wreck-of-the-hephaestus', 'Wreck of the Hephaestus'], ['arsunt-garrison', 'Arsunt Garrison'], ['blushing-cavern', 'Blushing Cavern'],
  ['ruins-of-tsimpo', 'Ruins of Tsimpo'], ['smugglers-run', 'Smuggler\'s Run'], ['the-broodworks', 'The Broodworks'],
  ['wind-pass', 'Wind Pass'], ['wreck-of-the-tyche', 'Wreck of the Tyche'], ['testing-station-no-24', 'Testing Station No. 24'],
  ['testing-station-no-89', 'Testing Station No. 89'], ['testing-station-no-136', 'Testing Station No. 136'],
  ['testing-station-no-152', 'Testing Station No. 152'], ['testing-station-no-195', 'Testing Station No. 195'], ['the-old-quarry', 'The Old Quarry'],
].map(([id, label]) => ({ label, detail: 'Sand Table', href: `/world?stone=${id}`, kind: 'Map' }));

export function routeIndex(sections) {
  const seen = new Set();
  return sections.flatMap((section) => [
    ...(section.hub ? [{ label: section.group, href: section.hub }] : []),
    ...section.items,
  ].map((item) => ({ ...item, kind: 'Page', detail: section.group })))
    .concat([
      { label: 'Chat', href: '/chat', kind: 'Page', detail: 'Sietch, guild, house and map rooms' },
      { label: 'Settings', href: '/settings', kind: 'Page', detail: 'Characters, appearance and privacy' },
      { label: 'Mailbox', href: '/mailbox', kind: 'Page', detail: 'Messages and deliveries' },
      { label: 'Help', href: '/help', kind: 'Page', detail: 'Guides and changelog' },
      { label: 'Server rules', href: '/rules', kind: 'Page', detail: 'Community rules' },
      ...MAPS,
      ...STONES,
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
