// Fetch for the Seven Nights door layer (sietchSpots.js). Reads the signed-in
// player's /portal/sietch/state, the only source of door pins; never the public
// names-only route. Outside the event window it makes no request.
import { getJSON } from '../api.js';

// The event window (copy of record: data/sietch_nights_copy.json).
const EVENT_START = Date.parse('2026-10-26T00:00:00Z');
const EVENT_END = Date.parse('2026-11-02T00:00:00Z');

// Tonight's spots_public rows, or [] when signed out, outside the event, with the
// gate off, or on any failure: the layer is decoration and never errors the map.
export async function fetchDoorPins(authed, now = Date.now()) {
  if (!authed || now < EVENT_START || now >= EVENT_END) return [];
  try {
    const d = await getJSON('/portal/sietch/state');
    const rows = d && d.night && d.night.spots_public;
    return Array.isArray(rows) ? rows : [];
  } catch (e) {
    return [];
  }
}
