// Live other-player + world-vehicle layer (Hagga only). Feeds off the public
// /api/dune/positions + /api/dune/vehicles feeds (coords + partition/type only,
// PII-safe: no names, no account ids), the same data the public lastsietch.com
// board shows. Each entity is filtered to the drawn MAP and then to the selected
// sietch by partition (1=Habbanya / 32=Kulon / 33=Amtal), projected world ->
// normalized here, and drawn as a sprite token (icons.js): a vehicle by its
// class `t` and subtype `st` (world ids, not identity), a player as the
// anonymous badge. Unknown vehicle classes keep the old dot. No per-marker
// hover (the crowd is anonymous and can be dense).

import { worldToNormalized } from './transform.js';
import { ensureIconDefs, iconUse, vehicleIcon } from './icons.js';

const SVGNS = 'http://www.w3.org/2000/svg';

// dune.actors.map, the tag the live feeds carry. Keyed by map registry key so
// this stays in step with map_model without widening the public /data payload.
const ENGINE_MAP = {
  hagga: 'HaggaBasin',
  'deep-desert': 'DeepDesert',
  arrakeen: 'Arrakeen',
  'harko-village': 'HarkoVillage',
};

// Since 2026-07-27 both feeds cover EVERY map (they were hardcoded to
// HaggaBasin, which hid everyone elsewhere). Partition alone must NOT be
// trusted to keep maps apart: it worked only because Hagga's partitions
// (1, 32, 33) happen not to collide with Deep Desert's (8, 31), and partMatch
// below returns true whenever the instance has no `part` at all, which is
// every non-Hagga map in the registry. Without this gate, turning the feed on
// for those pages would draw the entire world on each of them.
// A row with no `m` is treated as Hagga: that is what the feeds returned
// exclusively before the change, so an older backend still renders.
function mapMatch(row, mapKey) {
  const want = ENGINE_MAP[mapKey];
  if (!want) return true;
  return (row && row.m ? row.m : 'HaggaBasin') === want;
}

function partMatch(entityPart, selPart) {
  if (selPart === null || selPart === undefined) return true;
  if (entityPart === null || entityPart === undefined) return false;
  return Number(entityPart) === Number(selPart);
}

// p: { players, vehicles, cal, viewUnits, part, mapKey, showPlayers,
// showVehicles }. players/vehicles are the raw public rows ({x,y,p,m,d} /
// {x,y,p,t,st,m,d}; st is absent on an older backend and falls to the light
// ornithopter). Returns the count actually drawn for the selected sietch, so
// chrome can show a readout.
export function drawOtherPlayers(svg, p) {
  ensureIconDefs(svg);
  const old = svg.querySelector('.map-others-layer');
  if (old) svg.removeChild(old);
  const layer = document.createElementNS(SVGNS, 'g');
  layer.setAttribute('class', 'map-others-layer');
  const unit = p.viewUnits / 1000;
  let playerCount = 0;

  if (p.showVehicles) {
    (p.vehicles || []).forEach(function (v) {
      if (!mapMatch(v, p.mapKey)) return;
      if (!partMatch(v.p, p.part)) return;
      const n = worldToNormalized(v.x, v.y, p.cal, p.viewUnits);
      if (!n) return;
      const name = vehicleIcon(v.t, v.st);
      if (!name || !iconUse(layer, name, n.nx, n.ny, 14 * unit, 'map-other-vehicle map-icon')) {
        dot(layer, n.nx, n.ny, 3.4 * unit, 'map-other-vehicle');
      }
    });
  }

  if (p.showPlayers) {
    (p.players || []).forEach(function (pl) {
      if (!mapMatch(pl, p.mapKey)) return;
      if (!partMatch(pl.p, p.part)) return;
      const n = worldToNormalized(pl.x, pl.y, p.cal, p.viewUnits);
      if (!n) return;
      if (!iconUse(layer, 'player-anon', n.nx, n.ny, 12 * unit, 'map-other-player map-icon')) {
        dot(layer, n.nx, n.ny, 4.2 * unit, 'map-other-player');
      }
      playerCount++;
    });
  }

  // Prepend so the crowd always renders beneath the me-pin / waypoints /
  // overlays (which append to the end), regardless of which feed polled last.
  svg.insertBefore(layer, svg.firstChild);
  return playerCount;
}

// Count the players in the selected sietch without drawing (for the readout
// when the layer is toggled off). Applies the SAME map gate as the draw, or the
// readout would report the whole world while the map showed one sietch.
export function countOtherPlayers(players, part, mapKey) {
  let n = 0;
  (players || []).forEach(function (pl) {
    if (mapMatch(pl, mapKey) && partMatch(pl.p, part)) n++;
  });
  return n;
}

function dot(parent, x, y, r, cls) {
  const c = document.createElementNS(SVGNS, 'circle');
  c.setAttribute('cx', x);
  c.setAttribute('cy', y);
  c.setAttribute('r', r);
  c.setAttribute('class', cls);
  parent.appendChild(c);
}
