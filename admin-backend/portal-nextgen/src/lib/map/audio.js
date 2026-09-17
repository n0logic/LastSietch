// Worm danger audio cue (V1 _pingAudio port). Muted by default; the engine's
// setAudioEnabled(true) must be called from a user gesture (chrome's toggle
// click) so the AudioContext is created/resumed inside the gesture grant.
// Trigger semantics: one ping per danger TRANSITION (false -> true), not V1's
// re-ping on every poll while danger persists; enabling audio while already in
// danger pings once (audible confirmation + alert, matches V1's <=10s ping).

export function createWormAudio(opts) {
  const factory = (opts && opts.contextFactory) || defaultContextFactory;
  let enabled = false;
  let prevDanger = false;
  let ctx = null;

  function ensureCtx() {
    if (!ctx) ctx = factory();
    if (ctx && ctx.state === 'suspended' && ctx.resume) ctx.resume();
    return ctx;
  }

  // V1 envelope verbatim: 440Hz sine falling to 220Hz over 0.18s, 0.22s decay.
  function ping() {
    try {
      const c = ensureCtx();
      if (!c) return;
      const osc = c.createOscillator();
      const gain = c.createGain();
      osc.connect(gain);
      gain.connect(c.destination);
      osc.type = 'sine';
      osc.frequency.setValueAtTime(440, c.currentTime);
      osc.frequency.exponentialRampToValueAtTime(220, c.currentTime + 0.18);
      gain.gain.setValueAtTime(0.18, c.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, c.currentTime + 0.22);
      osc.start(c.currentTime);
      osc.stop(c.currentTime + 0.22);
    } catch (e) { /* audio is best-effort, never break the map */ }
  }

  return {
    setEnabled: function (on) {
      const next = !!on;
      if (next && !enabled) {
        ensureCtx();               // inside the user gesture
        if (prevDanger) ping();
      }
      enabled = next;
    },
    isEnabled: function () { return enabled; },
    notifyDanger: function (danger) {
      const d = !!danger;
      if (d && !prevDanger && enabled) ping();
      prevDanger = d;
    },
    destroy: function () {
      if (ctx && ctx.close) { try { ctx.close(); } catch (e) {} }
      ctx = null;
    },
  };
}

function defaultContextFactory() {
  const Ctor = (typeof window !== 'undefined') &&
    (window.AudioContext || window.webkitAudioContext);
  return Ctor ? new Ctor() : null;
}
