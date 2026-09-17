const NAMES = { habbanya: 'Habbanya', kulon: 'Kulon', amtal: 'Amtal', 'dd-pve': 'Deep Desert', 'dd-pvp': 'Deep Desert' };

export function timeBand(hour) {
  if (!Number.isInteger(hour) || hour < 0 || hour > 23) return 'dusk';
  if (hour >= 5 && hour < 8) return 'dawn';
  if (hour >= 8 && hour < 16) return 'noon';
  if (hour >= 16 && hour < 20) return 'dusk';
  return 'night';
}

export function homeAppearance(signals, clock) {
  const s = signals || {};
  const map = (s.mapKey === 'hagga'
    ? ['habbanya', 'kulon', 'amtal'][s.dim] || 'habbanya'
    : s.dim === 1 ? 'dd-pvp' : 'dd-pve');
  const inst = map === 'amtal' ? 'Full PvP' : map === 'kulon' || map === 'dd-pvp' ? 'PvP' : 'PvE';
  return {
    map, label: NAMES[map], mode: inst, band: timeBand(clock?.local_hour),
    weather: s.stormActive ? 'storm' : 'clear', untracked: map === 'amtal',
    href: map.startsWith('dd-') ? `/maps/deep-desert?inst=${map.slice(3)}` : `/maps/hagga?inst=${map}`,
  };
}
