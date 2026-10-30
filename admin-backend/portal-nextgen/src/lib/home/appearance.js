import { instanceFor } from '../mapInstances.js';

export function timeBand(hour) {
  if (!Number.isInteger(hour) || hour < 0 || hour > 23) return 'dusk';
  if (hour >= 5 && hour < 8) return 'dawn';
  if (hour >= 8 && hour < 16) return 'noon';
  if (hour >= 16 && hour < 20) return 'dusk';
  return 'night';
}

export function homeAppearance(signals, clock, instances) {
  const s = signals || {};
  // The board's instance at the selected dim; an unknown or closed dim falls back
  // to that map's first open instance, never a dead plate. No instances read yet
  // (or none configured) leaves the stand blank.
  const inst = instanceFor(instances, s.mapKey === 'hagga' ? 'hagga' : 'deep-desert', s.dim);
  return {
    map: inst ? inst.key : null, label: inst ? inst.name : '', mode: inst ? inst.mode : '',
    band: timeBand(clock?.local_hour),
    weather: s.stormActive ? 'storm' : 'clear', untracked: inst ? !inst.tracked : false,
    href: '/world',
  };
}
