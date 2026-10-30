// Pure helpers for the redesigned Home (P1). No Svelte, no store, no fetch: the
// components hand in plain values and the suite runs these under node.

/** What the hero says about a weekend slot from $lib/weekend.js describeSlot().
 *  Only a slot whose effect the runner read back (effect_state 'running') is
 *  verified live. A missed or failed effect is NEVER live, whatever the clock says. */
export function heroState(d) {
  if (!d) return { kind: 'none', verified: false, kicker: 'This weekend', countdown: null };
  if (!d.info) return { kind: 'open', verified: false, kicker: 'This weekend', countdown: null };
  if (!d.open) {
    return {
      kind: 'upcoming', verified: false, countdown: 'start',
      kicker: d.planned ? 'Next weekend, planned' : 'Next weekend',
    };
  }
  if (d.state === 'failed' || d.state === 'missed') {
    return { kind: 'notlive', verified: false, kicker: 'This weekend, bonus not confirmed', countdown: null };
  }
  if (d.state === 'done') return { kind: 'done', verified: false, kicker: 'This weekend, finished', countdown: null };
  if (d.state === 'armed') return { kind: 'pending', verified: false, kicker: 'This weekend, not live yet', countdown: 'end' };
  if (d.state === 'running') return { kind: 'live', verified: true, kicker: 'Live now', countdown: 'end' };
  // Open, and nothing measured it: a published title-derived event is "on the
  // board"; a rotation plan with no event is only planned.
  if (d.planned) return { kind: 'planned', verified: false, kicker: 'This weekend, planned', countdown: 'end' };
  return { kind: 'live', verified: false, kicker: 'On the board now', countdown: 'end' };
}

/** The one place the weekend sends a player to act. */
export function weekendAction(effect, base = '') {
  switch (effect) {
    case 'spec_xp': return { label: 'Open the Landsraad', href: `${base}/landsraad` };
    case 'harvest': return { label: 'Open the maps', href: `${base}/maps` };
    case 'crafting': return { label: 'Open the workshop', href: `${base}/storage/workshop` };
    case 'xp': return { label: 'See your character', href: `${base}/character` };
    default: return { label: 'Open events', href: `${base}/events` };
  }
}

// The weekend hero art per effect (art pass B1). Each name is a file stem under
// /img/v3/weekend/: `<name>-hero` (21:9) and `<name>-mobile` (4:5), each as avif,
// webp and jpg. The same names are the event banner slugs. Bump WEEKEND_ART_VERSION
// whenever these files are re-cut; new art gets new names, never a reused one.
export const WEEKEND_ART = {
  xp: 'weekend-xp',
  spec_xp: 'weekend-landsraad',
  crafting: 'weekend-crafting',
  harvest: 'weekend-harvest',
};
export const WEEKEND_ART_VERSION = '20261030a';

/** The hero art stem for an effect, or null (an open weekend keeps the map plate). */
export function weekendArt(effect) {
  return Object.hasOwn(WEEKEND_ART, effect) ? `/img/v3/weekend/${WEEKEND_ART[effect]}` : null;
}

// Map NAME only. Never coordinates, region or partition: the owner ruling for
// entity rows. Anything we do not recognise is omitted rather than passed through.
// The names are the configured instance names (GET /portal/features).
export function mapNameOnly(name, instances) {
  const s = String(name || '').toLowerCase();
  const hit = (instances || []).find((i) => s.includes(i.name.toLowerCase()));
  return hit ? hit.name : null;
}

export function initials(name) {
  const words = String(name || '').trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return '?';
  const first = Array.from(words[0])[0] || '?';
  const second = words.length > 1 ? (Array.from(words[words.length - 1])[0] || '') : '';
  return (first + second).toUpperCase();
}

/** "Needs you": up to three real things waiting, in priority order. A null signal
 *  is absent, never zero. */
export function needsYou(s, base = '') {
  const out = [];
  if (!s || s.linked !== true) return out;
  const reward = s.rewardsEnabled !== false
    && ((s.claimableTotal || 0) > 0 || s.weeklyClaimable === true || s.monthlyClaimable === true);
  if (reward) out.push({ key: 'rewards', title: 'A reward is ready to claim', note: 'Open Rewards to claim it.', cta: 'Claim', href: `${base}/rewards` });
  if ((s.marketFilled || 0) > 0) {
    const n = s.marketFilled;
    out.push({ key: 'orders', title: `${n} Exchange ${n === 1 ? 'order' : 'orders'} filled today`, note: 'Collect what you earned.', cta: 'Collect', href: `${base}/exchange` });
  }
  if ((s.deliveriesPending || 0) > 0) {
    const n = s.deliveriesPending;
    out.push({ key: 'deliveries', title: `${n} ${n === 1 ? 'package' : 'packages'} still landing`, note: 'They arrive in game soon.', cta: 'View', href: `${base}/mailbox#deliveries` });
  }
  if ((s.inviteCount || 0) > 0) {
    const n = s.inviteCount;
    out.push({ key: 'invites', title: `${n} sietch ${n === 1 ? 'invite' : 'invites'} waiting`, note: 'Answer from Guilds.', cta: 'Open', href: `${base}/guilds` });
  }
  if ((s.unread || 0) > 0) {
    const n = s.unread;
    out.push({ key: 'mail', title: `${n} unread ${n === 1 ? 'message' : 'messages'}`, note: 'In your mailbox.', cta: 'Read', href: `${base}/mailbox` });
  }
  return out.slice(0, 3);
}

/** The desert's reading as short lines from the Home signals. A storm or spice line
 *  only appears when that read said so; nothing is invented for a read that failed. */
export function desertLines(s) {
  const out = [];
  if (!s) return out;
  if (s.storm && s.stormActive === true) {
    const where = s.stormSector ? ` over ${s.stormSector}` : '';
    const way = s.stormHeading ? `, heading ${s.stormHeading}` : '';
    out.push({ text: `Sandstorm${where}${way}. Make for rock.`, alert: true });
  } else if (s.storm) {
    out.push({ text: 'No sandstorm on the board.', alert: false });
  }
  if (s.spiceActive === true) {
    out.push({ text: s.spiceSector ? `Spice blowing at ${s.spiceSector}.` : 'Spice is blowing.', alert: false });
  }
  if ((s.wormCount || 0) > 0) {
    out.push({ text: `${s.wormCount} Shai-Hulud on the sand${s.enraged > 0 ? `, ${s.enraged} enraged` : ''}.`, alert: false });
  }
  if (out.length === 0) out.push({ text: 'Reading the desert.', alert: false });
  return out;
}
