// Data transport (V1 cadences, NEVER faster) + console-model derivation.
// Every payload funnels through the engine's single applyFix ingestion point
// so the M-LIVE SSE transport can swap in later without touching the renderer
// or reckon.js.

// Relative import (not $lib) so the node smoke can load this module without
// the SvelteKit alias; api.js itself only touches browser APIs inside fns.
import { api, getJSON } from '../api.js';
import { headingToCompass } from './overlays.js';

export const LIVE_INTERVAL_MS = 10000;       // /live cadence floor (V1)
export const PLAYERS_INTERVAL_MS = 45000;    // /players cadence (V1)
export const ME_INTERVAL_MS = 10000;         // /me cadence (V1)
// Live other-player layer (Hagga only). Cadences sit just above the backend
// caches (positions 5s, vehicles 15s) so a fast client never out-runs them.
export const POSITIONS_INTERVAL_MS = 6000;
export const WORLD_VEHICLES_INTERVAL_MS = 16000;
// Shared /me staleness threshold (2.5x cadence): the chip clock and the
// engine's quiet-feed degrade must agree on the same number.
export const ME_STALE_MS = 25000;

// Assumed sweep window for a recurring DD sandstorm. The pod logs emit only a
// spawn timestamp with no duration, so "active" is derived (V1 calibration
// assumption, tune if it reads early/late in-game).
export const STORM_ACTIVE_SECONDS = 180;
const STORM_LOCATION_FRESH_MS = 20 * 60 * 1000;

export function createPollers(key, applyFix, getDim) {
  let liveTimer = null, playersTimer = null, meTimer = null;
  let posTimer = null, vehTimer = null;
  let spice = false, started = false, paused = false;
  // The live other-player layer only exists on Hagga (the positions feed is
  // Hagga-scoped; Deep Desert deliberately has none).
  const hasOthers = key === 'hagga';

  function pollLive() {
    api.maps.live(key)
      .then(function (d) { applyFix('live', d); })
      .catch(function () {});
  }

  function pollPlayers() {
    // Per-instance count: without the dim the backend returns the whole-map
    // total, so every instance card showed the same number (DD "2 here" on
    // both PvE and PvP while only dim 0 held them; Hagga showed dim0+1+2 on
    // all three cards). 2026-08-29.
    api.maps.players(key, typeof getDim === 'function' ? getDim() : null)
      .then(function (d) { applyFix('players', d); })
      .catch(function () {});
  }

  function pollPositions() {
    api.maps.dunePositions()
      .then(function (d) { applyFix('positions', d); })
      .catch(function () {});
  }

  function pollWorldVehicles() {
    api.maps.duneVehicles()
      .then(function (d) { applyFix('worldVehicles', d); })
      .catch(function () {});
  }

  function pollMe() {
    getJSON('/portal/maps/' + key + '/me')
      .then(function (d) { applyFix('me', d); })
      .catch(function (err) {
        // Session expired mid-run: tell the engine so the layer clears
        // honestly instead of freezing on the last authed fix.
        if (err && err.status === 401) applyFix('me', { authenticated: false });
        // Any other failure: no new receipt. Signal the quiet so the engine
        // re-evaluates meAtMs and degrades the board pin once past the stale
        // threshold — otherwise a dead feed never triggers a redraw and the
        // breathing Ibad ring would persist forever.
        else applyFix('me-quiet', {});
      });
  }

  function startPublic() {
    if (spice && !liveTimer) {
      pollLive();
      liveTimer = setInterval(pollLive, LIVE_INTERVAL_MS);
    }
    if (!playersTimer) {
      pollPlayers();
      playersTimer = setInterval(pollPlayers, PLAYERS_INTERVAL_MS);
    }
    if (hasOthers && !posTimer) {
      pollPositions();
      posTimer = setInterval(pollPositions, POSITIONS_INTERVAL_MS);
    }
    if (hasOthers && !vehTimer) {
      pollWorldVehicles();
      vehTimer = setInterval(pollWorldVehicles, WORLD_VEHICLES_INTERVAL_MS);
    }
  }

  function stopPublic() {
    if (liveTimer) clearInterval(liveTimer);
    if (playersTimer) clearInterval(playersTimer);
    if (posTimer) clearInterval(posTimer);
    if (vehTimer) clearInterval(vehTimer);
    liveTimer = playersTimer = posTimer = vehTimer = null;
  }

  return {
    loadData: function () {
      return api.maps.data(key).then(function (d) {
        applyFix('data', d);
        return d;
      });
    },
    start: function (hasSpice) {
      spice = !!hasSpice;
      started = true;
      if (!paused) startPublic();
    },
    // SSE transport swap: pause suspends ONLY the public live/players pollers
    // (the me poller is session-gated and never rides the public stream).
    pause: function () {
      paused = true;
      stopPublic();
    },
    resume: function () {
      if (!paused) return;
      paused = false;
      if (started) startPublic();
    },
    // /me poller, active only while the chrome signals an authed session.
    // One-shot players refetch. The count is per-dimension now, so switching
    // instance must refetch immediately instead of waiting out the 45s cadence
    // (which would leave the previous instance's number on screen).
    refreshPlayers: function () { pollPlayers(); },
    startMe: function () {
      if (meTimer) return;
      pollMe();
      meTimer = setInterval(pollMe, ME_INTERVAL_MS);
    },
    stopMe: function () {
      if (meTimer) clearInterval(meTimer);
      meTimer = null;
    },
    stop: function () {
      stopPublic();
      if (meTimer) clearInterval(meTimer);
      meTimer = null;
    },
  };
}

// ---- console model ----------------------------------------------------------
// Pure derivation of the SpiceHudConsole feed from engine state (the V1 banner
// logic, minus all DOM). Scoped to the SELECTED instance's dimension; the PvE
// tab must never show the PvP data and vice versa.
// spice/worms carry asOfMs (transport receipt time): a dead poller fails
// silently, so chrome must degrade Ibad to dim amber when
// now - asOfMs > ~2.5x the 10s cadence, not trust `fresh` alone.
// src: { meta, spice, sandstorm, worms, instance, spiceAtMs?, wormsAtMs?, now? }
export function deriveConsole(src) {
  const dim = src.instance ? src.instance.dim : null;
  const dimKey = String(dim);
  const now = src.now != null ? src.now : Date.now();
  const meta = src.meta || {};

  const mediums = meta.spice_mediums || [];
  const mediumActive = mediums.filter(function (m) { return m && m[3] === true; }).length;
  const spiceInfo = ((src.spice && src.spice.dimensions) || {})[dimKey];
  let blows = [];
  if (spiceInfo && spiceInfo.large_active) {
    const fields = (spiceInfo.ram_active_fields && spiceInfo.ram_active_fields.length)
      ? spiceInfo.ram_active_fields
      : [{ sector: spiceInfo.ram_sector }];
    blows = fields
      .map(function (f) { return { sector: (f.sector || '').toUpperCase() }; })
      .filter(function (b) { return b.sector; });
    // active with blows empty = a Large is up at one of the candidate sites
    // (chrome copy decision).
  }

  const wormsInfo = ((src.worms && src.worms.dimensions) || {})[dimKey];
  const list = (wormsInfo && wormsInfo.worms) || [];
  const roaming = list.filter(function (w) { return w.threat && w.threat !== 'submerged'; });
  const enraged = list.filter(function (w) { return w.threat === 'enraged' || w.threat === 'breaching'; });
  const breaching = list.filter(function (w) { return w.threat === 'breaching'; });
  const sectors = roaming.map(function (w) { return w.sector; })
    .filter(Boolean)
    .filter(function (v, i, a) { return a.indexOf(v) === i; });

  return {
    spice: {
      active: !!(spiceInfo && spiceInfo.large_active),
      blows: blows,
      mediumTotal: mediums.length,
      mediumActive: mediumActive,
      fresh: !!src.spice,
      asOfMs: src.spiceAtMs || null,
    },
    storm: deriveStorm(src.sandstorm, dimKey, now),
    coriolis: {
      nextCycleUtc: (meta.coriolis && meta.coriolis.next_cycle_utc) || null,
    },
    worms: {
      total: list.length,
      roaming: roaming.length,
      enraged: enraged.length,
      breaching: breaching.length,
      sectors: sectors,
      danger: enraged.length > 0,
      fresh: !!src.worms,
      asOfMs: src.wormsAtMs || null,
    },
  };
}

function deriveStorm(sandstorm, dimKey, now) {
  const sealed = { state: 'sealed', fresh: false, label: '' };
  if (!sandstorm || sandstorm.available === false) return sealed;
  const info = (sandstorm.dimensions || {})[dimKey];
  // Selected dim absent from the feed: sealed, NEVER borrow the other dim.
  if (!info) return sealed;
  const scanned = info.storm_scanned_utc;
  const locFresh = !!(scanned &&
    now - new Date(scanned).getTime() < STORM_LOCATION_FRESH_MS);
  if (info.storm_sector && locFresh) {
    const heading = headingToCompass(info.heading_yaw);
    return {
      state: 'centered',
      sector: info.storm_sector,
      heading: heading,
      // Compass sign VERIFIED 2026-07-04 vs a live sweep (drift bearing 34.47
      // deg vs heading_yaw 34.5; samples in ops/storm-heading-verify/).
      headingProvisional: false,
      fresh: true,
      label: 'centered ' + info.storm_sector +
             (heading ? ', heading ' + heading : ''),
    };
  }
  if (info.last_spawn_utc) {
    const since = now - new Date(info.last_spawn_utc).getTime();
    if (since >= 0 && since < STORM_ACTIVE_SECONDS * 1000) {
      return { state: 'sweeping', fresh: true, label: 'sweeping now' };
    }
  }
  return { state: 'eta', etaUtc: info.next_eta_utc || null, fresh: false, label: '' };
}
