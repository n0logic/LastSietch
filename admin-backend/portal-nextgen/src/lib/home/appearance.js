const NAMES = { habbanya: 'Habbanya', amtal: 'Amtal', 'dd-pve': 'Deep Desert', 'dd-pvp': 'Deep Desert' };

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
    // Hagga dims: 0 Habbanya, 2 Amtal. Dim 1 was Kulon (closed 2026-10-28); a
    // stale 1 falls back to Habbanya like any other unknown dim.
    ? { 0: 'habbanya', 2: 'amtal' }[s.dim] || 'habbanya'
    : s.dim === 1 ? 'dd-pvp' : 'dd-pve');
  const inst = map === 'amtal' ? 'Full PvP' : map === 'dd-pvp' ? 'PvP' : 'PvE';
  return {
    map, label: NAMES[map], mode: inst, band: timeBand(clock?.local_hour),
    weather: s.stormActive ? 'storm' : 'clear', untracked: map === 'amtal',
    href: '/world',
  };
}
