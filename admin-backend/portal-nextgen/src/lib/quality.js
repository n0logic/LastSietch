// Mobile-first quality-tier detector (Bruno Simon's rule: cut work on weak GPUs).
// Surfaces drive 3D/particle/FBO budgets off `tier`; every heavy effect also
// honors reducedMotion + saveData independently. Browser-only; SSR-safe defaults.

function detectWebGL() {
  if (typeof document === 'undefined') return false;
  try {
    const c = document.createElement('canvas');
    return !!(c.getContext('webgl2') || c.getContext('webgl'));
  } catch {
    return false;
  }
}

export function detectQuality() {
  if (typeof window === 'undefined') {
    return { tier: 'mid', reducedMotion: true, saveData: false, dpr: 1, cores: 4, memory: 4, webgl: false };
  }
  const dpr = Math.min(window.devicePixelRatio || 1, 3);
  const cores = navigator.hardwareConcurrency || 4;
  const memory = navigator.deviceMemory || 4;
  const conn = navigator.connection || {};
  const saveData = !!conn.saveData;
  const reducedMotion = !!(window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  const webgl = detectWebGL();
  const coarse = !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);

  // Heuristic tiers. Conservative on coarse-pointer (mobile) + low memory/cores.
  let tier = 'mid';
  if (!webgl || saveData || memory <= 2 || cores <= 2) tier = 'low';
  else if (!coarse && memory >= 8 && cores >= 8 && dpr <= 2) tier = 'high';
  if (reducedMotion && tier === 'high') tier = 'mid';

  return { tier, reducedMotion, saveData, dpr, cores, memory, webgl, coarse };
}
