// The Home verdict ladder, as pure functions over plain data.
//
// Plain .js on purpose: no runes, no imports, no browser. The ladder is the one
// piece of Home that decides what a player is told first, so it has to be
// exercisable with fixtures from a bare `node` process rather than only through
// a rendered page. home.svelte.js owns the reads and the timers and calls in
// here; nothing in here knows what a fetch is.
//
// Two rules run through all of it:
//
//   * A NULL SIGNAL IS ABSENT, NEVER 0. `claimable_total: null` means the
//     rewards loader could not answer, and printing "0 Solari waiting" for it is
//     a fabricated statement about somebody's own account. There is no `?? 0`
//     and no `|| 0` in this file for that reason: every comparison is written so
//     that null falls through instead of landing on a number.
//   * THE H1 IS NEVER EMPTY. Rank 10 is a fallthrough that always produces a
//     sentence, so every path through deriveVerdict returns text.
//
// Character online/offline is a MODIFIER on copy and never a verdict of its own:
// a player being logged out changes which sentence we write about the desert,
// not which rank wins.

// Friendly map name (what /portal/home/v2 reports as current_map, straight out
// of MAP_DISPLAY_NAMES) -> the maps key its live overlay is served under. The
// social hubs and the Overmap have no live board, so they resolve to null and
// the reading falls back to the stored Deep Desert instance.
const MAP_KEY_BY_NAME = {
  'Deep Desert': 'deep-desert',
  'Hagga Basin': 'hagga',
};

export const DEFAULT_MAP_KEY = 'deep-desert';
export const DEFAULT_MAP_LABEL = 'Deep Desert';

/** 'Hagga Basin' -> 'hagga'. null for a map with no live board of its own. */
export function mapKeyFor(currentMap) {
  if (!currentMap) return null;
  return MAP_KEY_BY_NAME[currentMap] || null;
}

/** The dimension a partition sits in, read off the board's OWN instance table
 *  (/data instances: {key, label, dim, part}). Hagga's three sietches share the
 *  terrain and are told apart ONLY by partition_id, so the sietch is never
 *  guessed from the friendly map name: 'Hagga Basin' names the terrain, not
 *  which of Habbanya, Kulon or Amtal a player is standing in.
 *
 *  Returns null when the partition is unknown, the board has not been read yet,
 *  or the board's instances carry no `part` at all (the Deep Desert, whose two
 *  instances are a player preference rather than a placement). A null here means
 *  the caller keeps the stored instance, which is what Home has always done. */
export function dimForPartition(instances, partition) {
  if (partition == null || !Array.isArray(instances)) return null;
  const hit = instances.find((i) => i && i.part != null && i.part === partition);
  return hit && hit.dim != null ? hit.dim : null;
}

/** Friendly label for a maps key, for the copy that names the board. */
export function mapLabelFor(key) {
  for (const name of Object.keys(MAP_KEY_BY_NAME)) {
    if (MAP_KEY_BY_NAME[name] === key) return name;
  }
  return DEFAULT_MAP_LABEL;
}

// A storm counts as on the map while its position reader is fresh: the reader
// tracks the moving storm live, so scan freshness is the active signal (the
// 3-minute post-spawn heuristic missed the long sweep).
export const STORM_SCAN_FRESH_MS = 12 * 60_000;
// The Landsraad term is "closing" once it is inside a day.
export const TERM_SOON_MS = 24 * 60 * 60_000;

function ms(utc) {
  if (!utc) return null;
  const t = new Date(utc).getTime();
  return Number.isFinite(t) ? t : null;
}

/** Storm heading -> 8-point compass. World +x = East, +y = South, and the
 *  reader's heading_yaw matches the map engine's (cos,sin) screen convention,
 *  so bearing = (yaw + 90) degrees clockwise from North. */
export function compass(yaw) {
  if (yaw == null || !isFinite(yaw)) return null;
  const b = (((yaw + 90) % 360) + 360) % 360;
  return ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][Math.round(b / 45) % 8];
}

function plural(n, one, many) {
  return n === 1 ? one : many;
}

/** Flatten every read into the facts the ladder and the instruments consume.
 *  raw = {data, live, overview, standings, announcement, home}; ctx carries the
 *  things the reads do not know about themselves (our clock, the selected
 *  instance, the map board in play, whether identity has resolved). */
export function readSignals(raw, ctx) {
  const r = raw || {};
  const c = ctx || {};
  const now = c.now != null ? c.now : Date.now();
  const dimKey = String(c.dim != null ? c.dim : 0);

  const board = r.data || null;
  const live = r.live || null;
  const spice = (live && live.spice && live.spice.dimensions
    && live.spice.dimensions[dimKey]) || null;
  const stormRaw = (live && live.sandstorm && live.sandstorm.dimensions
    && live.sandstorm.dimensions[dimKey]) || null;
  const wormList = (live && live.worms && live.worms.dimensions
    && live.worms.dimensions[dimKey] && live.worms.dimensions[dimKey].worms) || [];

  const scanned = ms(stormRaw && stormRaw.storm_scanned_utc);
  const stormSector = (stormRaw && stormRaw.storm_sector) || null;
  const stormActive = !!(stormSector && scanned != null
    && now - scanned >= 0 && now - scanned < STORM_SCAN_FRESH_MS);

  const status = (r.overview && r.overview.status) || null;
  const ann = (r.announcement && r.announcement.active) ? r.announcement : null;
  const standings = r.standings || null;

  const h = r.home || {};
  const character = h.character || null;
  const rewards = h.rewards || null;
  const guild = h.guild || null;
  const wallet = h.wallet || null;
  const orders = h.orders || null;
  const mailbox = h.mailbox || null;
  const landsraad = h.landsraad || null;
  const deliveries = h.deliveries || null;

  const mapKey = c.mapKey || DEFAULT_MAP_KEY;
  const charMap = character ? (character.current_map || null) : null;
  const charOnline = character ? character.online === true : null;
  // Absent whenever the roster cannot say, which is not the same as partition 0.
  const charPartition = character && character.current_partition != null
    ? character.current_partition : null;

  return {
    booting: c.booting === true,
    now,
    authed: c.authed === true,
    linkCount: c.linkCount != null ? c.linkCount : 0,
    // Linked is what every personal rank gates on: a connected Discord with no
    // character is rank 1, not a player with an empty console.
    linked: c.authed === true && (c.linkCount != null ? c.linkCount : 0) > 0,

    mapKey,
    mapLabel: mapLabelFor(mapKey),
    dim: c.dim != null ? c.dim : 0,
    instLabel: c.instLabel || null,
    // Spice, worms and storms are SEPARATE axes and the board says which it
    // carries: Hagga has storms and no spice, and gating the storm card on
    // hasSpice hid the live conditions from every player standing on it. Until
    // /data lands, the Deep Desert is the only board we can answer for.
    hasSpice: board ? board.has_spice === true : mapKey === DEFAULT_MAP_KEY,
    hasStorms: board ? board.has_storms === true : mapKey === DEFAULT_MAP_KEY,

    // A status object that never carried `up` is an UNREAD server, not a down
    // one. Testing `status.up === true` alone turns a half-answer into rank 2.
    serverUp: status && status.up != null ? status.up === true : null,
    onlinePlayers: status && status.online_players != null ? status.online_players : null,
    mapPlayers: mapPlayersFrom(status, mapLabelFor(mapKey)),

    stormActive,
    stormSector,
    stormHeading: compass(stormRaw && stormRaw.heading_yaw),
    stormNextEtaUtc: (stormRaw && stormRaw.next_eta_utc) || null,
    storm: stormRaw,

    spiceActive: !!(spice && spice.large_active),
    spiceSector: (spice && (spice.sector || spice.ram_sector)) || null,

    wormCount: wormList.length,
    enraged: wormList.filter((w) => w.enraged).length,
    breaching: wormList.filter((w) => w.threat === 'breaching').length,

    announcement: ann,
    termEndUtc: (standings && standings.term && standings.term.end_utc) || null,
    standings,

    charName: character ? (character.name || null) : null,
    charOnline,
    charMap,
    charPartition,

    rewardsEnabled: rewards ? rewards.enabled !== false : null,
    claimableTotal: rewards && rewards.claimable_total != null ? rewards.claimable_total : null,
    claimableDays: rewards && rewards.claimable_days != null ? rewards.claimable_days : null,
    nextClaimUtc: (rewards && rewards.next_claim_utc) || null,
    weeklyClaimable: rewards ? rewards.weekly_claimable === true : null,
    monthlyClaimable: rewards ? rewards.monthly_claimable === true : null,
    rewardsCycle: (rewards && rewards.cycle) || null,

    // Whether the guild loader ANSWERED, which is a different question from
    // whether the player is in a sietch: the endpoint sends the object with a
    // null name for a player in none, and no object at all when it failed.
    guildRead: guild != null,
    guildName: (guild && guild.name) || null,
    guildMembers: (guild && guild.members) || null,
    guildOnline: guild && guild.online_members != null ? guild.online_members : null,
    inviteCount: guild && guild.invite_count != null ? guild.invite_count : null,

    // The bell in the shell is the authoritative unread count when it has read
    // one: two different numbers for the same inbox on the same screen is worse
    // than one that is a poll behind.
    myContribution: landsraad && landsraad.my_contribution != null
      ? landsraad.my_contribution : null,

    unread: c.unread != null ? c.unread
      : (mailbox && mailbox.unread != null ? mailbox.unread : null),
    bankSolari: wallet && wallet.bank_solari != null ? wallet.bank_solari : null,
    marketOpen: orders && orders.market_open != null ? orders.market_open : null,
    marketFilled: orders && orders.market_filled_today != null ? orders.market_filled_today : null,
    karumOpen: orders && orders.karum_open != null ? orders.karum_open : null,

    // What the server has sent this player (the welcome package, a return
    // package). A null sub-object is the game host not answering and every one
    // of these stays null for it: "0 packages" would tell a player nothing was
    // ever sent to them, which is a different claim from not being able to look.
    deliveriesCount: deliveries && deliveries.count != null ? deliveries.count : null,
    deliveriesPending: deliveries && deliveries.pending_count != null
      ? deliveries.pending_count : null,
    deliveriesLatest: (deliveries && deliveries.latest) || null,
  };
}

function mapPlayersFrom(status, label) {
  if (!status || !Array.isArray(status.maps)) return null;
  const row = status.maps.find((m) => m && m.name === label);
  if (!row || row.players == null) return null;
  return row.players;
}

/** The ladder. Rank 1 wins; every rank below it is only reached because the ones
 *  above did not fire. `gate` is the useAuthGate shape ({authed, anon, loading}),
 *  read for the two ranks that are about identity rather than the desert. */
export function deriveVerdict(signals, gate) {
  const s = signals || {};
  const g = gate || {};
  const linked = s.linked === true;

  // 1. Discord connected, no character linked. Above the boot line: it is true
  //    the moment identity resolves and does not wait on a desert read.
  if (g.authed === true && s.linkCount === 0) {
    return v(1, 'Your Discord is connected. Link a character to make this yours.', 'warn', null);
  }

  // The desert has not answered once yet. Not a rank: a holding sentence, so the
  // h1 is never empty on the first paint.
  if (s.booting) return v(0, 'Reading the desert...', 'idle', null);

  // 2. Server down. Only on a read that actually said so; a null status is an
  //    unread server, not a down one.
  if (s.serverUp === false) {
    return v(2, 'The station is down. Nothing is standing on the sand right now.', 'danger', null);
  }

  // 3. Storm on the map the player is actually on (Ibad).
  if (s.stormActive) {
    const here = s.charOnline === true && s.charMap ? ` on ${s.charMap}` : '';
    if (s.stormSector && s.stormHeading) {
      return v(3, `Sandstorm over ${s.stormSector}${here}, heading ${s.stormHeading}. Make for rock.`, 'danger', null);
    }
    if (s.stormSector) {
      return v(3, `Sandstorm over ${s.stormSector}${here}. Make for rock.`, 'danger', null);
    }
    return v(3, `Sandstorm sweeping ${s.mapLabel}. Make for rock.`, 'danger', null);
  }

  // 4. Operator alert or planned maintenance.
  const ann = s.announcement;
  if (ann && (ann.type === 'alert' || ann.type === 'maintenance')) {
    const when = ann.starts_utc || ann.ends_utc || null;
    if (ann.type === 'maintenance') {
      return v(4, ann.title ? `Planned maintenance: ${ann.title}` : 'Planned maintenance is scheduled.', 'warn', when);
    }
    return v(4, ann.title ? `Alert: ${ann.title}` : 'The operators have posted an alert.', 'warn', when);
  }

  // 5. Unclaimed rewards. Hidden entirely while the system is off, and never
  //    invented from a null total.
  if (linked && s.rewardsEnabled === true) {
    if (s.claimableTotal != null && s.claimableTotal > 0) {
      return v(5, `${s.claimableTotal.toLocaleString()} Solari waiting on your rewards track.`, 'live', s.nextClaimUtc);
    }
    if (s.weeklyClaimable === true || s.monthlyClaimable === true) {
      return v(5, 'A reward is waiting on your track.', 'live', s.nextClaimUtc);
    }
    if (s.claimableDays != null && s.claimableDays > 0) {
      return v(5, `${s.claimableDays} ${plural(s.claimableDays, 'day', 'days')} of rewards waiting to be claimed.`, 'live', s.nextClaimUtc);
    }
  }

  // 6. Pending guild invites.
  if (linked && s.inviteCount != null && s.inviteCount > 0) {
    return v(6, `${s.inviteCount} guild ${plural(s.inviteCount, 'invite', 'invites')} waiting on your answer.`, 'idle', null);
  }

  // 7. Unread mail.
  if (linked && s.unread != null && s.unread > 0) {
    return v(7, `${s.unread} unread ${plural(s.unread, 'message', 'messages')} in your mailbox.`, 'idle', null);
  }

  // 8. The Landsraad term closes inside a day.
  const termMs = ms(s.termEndUtc);
  if (termMs != null && termMs - s.now > 0 && termMs - s.now < TERM_SOON_MS) {
    return v(8, 'The Landsraad term closes within the day.', 'idle', s.termEndUtc);
  }

  // 9. Spice blowing (Ibad).
  if (s.spiceActive) {
    return v(9, s.spiceSector ? `Spice blowing at ${s.spiceSector}.` : 'Spice blowing, sector locating.', 'live', null);
  }

  // 10. Fallthrough. Always a sentence.
  if (s.enraged > 0) {
    return v(10, `${s.enraged} Shai-Hulud enraged on the ${s.mapLabel} sand.`, 'idle', null);
  }
  if (s.wormCount > 0) {
    return v(10, `${s.wormCount} Shai-Hulud roaming. Watch the sand.`, 'idle', null);
  }
  return v(10, 'Conditions clear.', 'idle', null);
}

function v(rank, text, tone, timerUtc) {
  return { rank, text, tone, timerUtc: timerUtc || null };
}

/** Up to three supporting lines under the h1. Whichever signal produced the
 *  verdict is skipped: the second line must add a fact, not restate one. */
export function deriveSubLines(signals, gate, verdict) {
  const s = signals || {};
  const rank = verdict ? verdict.rank : 0;
  const linked = s.linked === true;
  const out = [];

  if (linked && s.charName) {
    if (s.charOnline === true) {
      out.push(s.charMap
        ? `${s.charName} is standing in ${s.charMap}.`
        : `${s.charName} is online.`);
    } else if (s.charOnline === false) {
      out.push(`${s.charName} is offline. This is the public reading.`);
    }
  }

  if (rank !== 2 && s.serverUp === false) {
    out.push('The station is not answering.');
  }
  if (rank !== 3 && s.stormActive && s.stormSector) {
    out.push(`Sandstorm over ${s.stormSector}. Make for rock.`);
  }
  if (rank !== 9 && s.spiceActive) {
    out.push(s.spiceSector ? `Spice blowing at ${s.spiceSector}.` : 'Spice blowing, sector locating.');
  }
  if (rank !== 5 && linked && s.rewardsEnabled === true
      && s.claimableTotal != null && s.claimableTotal > 0) {
    out.push(`${s.claimableTotal.toLocaleString()} Solari waiting on your rewards track.`);
  }
  if (rank !== 6 && linked && s.inviteCount != null && s.inviteCount > 0) {
    out.push(`${s.inviteCount} guild ${plural(s.inviteCount, 'invite', 'invites')} waiting on your answer.`);
  }
  if (rank !== 7 && linked && s.unread != null && s.unread > 0) {
    out.push(`${s.unread} unread ${plural(s.unread, 'message', 'messages')} in your mailbox.`);
  }
  if (rank !== 10 && s.enraged > 0) {
    out.push(`${s.enraged} Shai-Hulud enraged on the sand.`);
  }

  return out.slice(0, 3);
}
