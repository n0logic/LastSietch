// The map-instance list, read once per page view off GET /portal/features (the
// server caches it for a minute). The shell switch (shellNext.svelte.js) reads the
// same payload through readFeatures(), so a page that needs both makes one call.
import { api } from './api.js';
import { parseInstances } from './mapInstances.js';

export const mapInstances = $state({
  list: [],
  status: 'loading', // loading | ready
});

let inflight = null;

/** One shared /portal/features read. Rejects on failure; the list stays empty. */
export function readFeatures() {
  if (!inflight) {
    inflight = api.server.features().then((data) => {
      mapInstances.list = parseInstances(data?.instances);
      mapInstances.status = 'ready';
      return data;
    }, (e) => {
      mapInstances.status = 'ready';
      throw e;
    }).finally(() => { inflight = null; });
  }
  return inflight;
}

/** Resolve the list. Never throws; read at most once per page view. */
export function loadMapInstances() {
  if (mapInstances.status === 'ready') return Promise.resolve();
  return readFeatures().then(() => {}, () => {});
}
