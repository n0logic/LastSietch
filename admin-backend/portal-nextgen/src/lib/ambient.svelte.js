export const motion = $state({ preference: 'auto' });

export function loadMotion() {
  try {
    const saved = localStorage.getItem('ls-motion');
    if (['auto', 'still', 'full'].includes(saved)) motion.preference = saved;
  } catch (e) {}
}

export function setMotion(value) {
  if (!['auto', 'still', 'full'].includes(value)) return;
  motion.preference = value;
  try { localStorage.setItem('ls-motion', value); } catch (e) {}
}
