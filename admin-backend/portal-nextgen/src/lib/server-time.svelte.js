import { parseTimeReading } from './server-time.js';

export const serverTime = $state({ nowMs: null, nextResetMs: null, timeZone: 'America/New_York', status: 'loading' });
let subscribers = 0;
let generation = 0;
let tickTimer;
let syncTimer;
let controller;
let pending;
let baseEpoch = null;
let basePerformance = 0;
let lastAttempt = -Infinity;

function tick() {
  if (document.hidden || baseEpoch === null) return;
  serverTime.nowMs = baseEpoch + Math.max(0, performance.now() - basePerformance);
  if (serverTime.nextResetMs !== null && serverTime.nowMs >= serverTime.nextResetMs
      && performance.now() - lastAttempt > 30000) sync();
}

async function sync() {
  if (pending || document.hidden) return pending;
  const current = generation;
  const requestController = new AbortController();
  controller = requestController;
  const signal = requestController.signal;
  const started = performance.now();
  lastAttempt = started;
  const timeout = setTimeout(() => requestController.abort(), 10000);
  pending = (async () => {
    try {
      const response = await fetch('/portal/server/time', {
        credentials: 'same-origin', cache: 'no-store', headers: { accept: 'application/json' }, signal,
      });
      if (!response.ok) throw new Error('Time read failed');
      const reading = parseTimeReading(await response.json());
      if (!reading) throw new Error('Time reading unavailable');
      if (current !== generation) return;
      basePerformance = performance.now();
      baseEpoch = reading.epochMs + Math.min((basePerformance - started) / 2, 5000);
      serverTime.timeZone = reading.timeZone;
      serverTime.nextResetMs = reading.nextResetMs;
      serverTime.status = 'synced';
      tick();
    } catch {
      if (current === generation) serverTime.status = baseEpoch === null ? 'unavailable' : 'stale';
    } finally {
      clearTimeout(timeout);
      if (current === generation) { pending = null; controller = null; }
    }
  })();
  return pending;
}

function resume() {
  if (!document.hidden) { tick(); sync(); }
}

export function subscribeServerTime() {
  if (typeof document === 'undefined') return () => {};
  subscribers += 1;
  if (subscribers === 1) {
    generation += 1;
    sync();
    tickTimer = setInterval(tick, 1000);
    syncTimer = setInterval(sync, 300000);
    document.addEventListener('visibilitychange', resume);
    window.addEventListener('focus', resume);
  }
  return () => {
    subscribers -= 1;
    if (subscribers !== 0) return;
    generation += 1;
    clearInterval(tickTimer);
    clearInterval(syncTimer);
    document.removeEventListener('visibilitychange', resume);
    window.removeEventListener('focus', resume);
    controller?.abort();
    controller = null;
    pending = null;
  };
}
