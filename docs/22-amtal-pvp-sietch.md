# 22 - Running a second sietch as full PvP

Amtal is our second Hagga Basin sietch, run as a full PvP map: PvP everywhere in the open world and at the
tradeposts, safe only inside two faction settlements. This doc covers what makes that work and the jobs that run
around it. [16-pvp-maps.md](16-pvp-maps.md) covers adding the partition and the per-partition PvP opt-in; read it
first.

Partition `33` below is ours. Since v0.8.1, map instance names and partition ids are configuration, not code: set
yours in the map instance config ([20-player-portal.md](20-player-portal.md#map-instances)). The Amtal jobs under
`ops/amtal/` read their partition and pod from the entry named by `LASTSIETCH_MAP_INSTANCE_KEY` (default `amtal`) and
fall back to ours when the file is missing, so install the file before the jobs.

## The ruleset we run

| Rule | Value |
|---|---|
| PvP | Everywhere, including tradeposts |
| Safe areas | Inside the Riftwatch and Helius Gate settlements only, with a CHOAM exchange terminal in each |
| Death to another player | Drops all loose backpack loot (resources, fuel, unlearned schematics). Equipped gear, weapons, tools, ammo, uniques, quest items and base blueprints stay |
| Harvest | 1.5x base, 3.75x in the open with Funcom's 2.5x PvP-zone bonus |
| Backpack | 4x volume, 100 slots |
| Arrival from another map | Placed at the player's own base on the map, or at their faction's safe area |
| Base Reconstruction Tool | Allowed, so players can pack a base and take a break |
| Exploits | Glitching through walls, terrain or shields is a ban |

Every value went to the community as a poll or an owner decision first and is kept in the rules register
([21](21-custom-settings-and-rates.md#the-rules-and-rates-register)).

## Configuration

### Per-pod arguments

Everything specific to the PvP map is an argument on that partition's podSpec, so the shared settings files stay the
same for every map ([21](21-custom-settings-and-rates.md#per-map-rates)). Ours, at build `2168446`:

| Argument | Purpose |
|---|---|
| `Bgd.ServerDisplayName=<name>` | The name in the server browser and sietch picker ([05](05-display-name.md)) |
| `-ini:game:[/Script/DuneSandbox.DataTablesSubsystem]:m_SecurityZonesDataTable=/Game/Dune/Systems/SecurityZones/DT_SecurityZones_PvPOverride.DT_SecurityZones_PvPOverride` | Funcom's PvP security-zone table, on this map only |
| `-ini:game:[/Script/DuneSandbox.DuneSandboxGameModeBase]:m_DropAmountOnDefeat=1.0` | A PvP death drops the whole loose backpack |
| `-ini:ServerCustomSettings:[/Script/DuneSandbox.UserServerCustomSettings]:GatheringAmount=1.5` | The map's harvest rate |
| Inventory volume and starting slots | The larger backpack |
| `Map.Markers.GainXPForMarkerDiscovery=0` | No XP for map marker discovery on this map |

Keep the shared `UserGame.ini` free of a global security-zones key: a key in the shared file beat our per-pod
argument. Keep `m_bShouldForceEnablePvpOnAllPartitions=False` so the PvE sietch stays PvE.

An update through Funcom's battlegroup update command dropped a per-pod argument on our host. Read the arguments
back from the CR after every update ([08](08-updates.md#update-day-regression-checklist)).

### The runtime PvP flag

The table argument alone was not enough. Players' clients also need the runtime cvar
`SecurityZones.UsePvPOverrideTable 1`, and a cvar sent at runtime is lost every time the map's server restarts.

A watcher (`ops/amtal/`, systemd timer, every minute) closes that gap:

1. Read the PvP map's current server id.
2. When it changes and the new server has logged that it is ready, send the cvar to that server only, through the
   server-command channel `scripts/dune-service-broadcast.py` already uses.
3. Read it back, record the server id as applied, and post the result to the ops channel.

Kill switch: a file under `/etc/lastsietch/`. Without the watcher, the first restart after a deploy quietly turns
your PvP map back into a zone-gated one.

### Safe areas and the zone banner

With the PvP table and the runtime flag, our tradeposts became PvP and the two faction settlements stayed safe.
Two things players will notice:

- The tradepost banner can flicker to a peace label. The PvP table lacks one of the stock town zone rows.
- In the open world the client's zone banner can still read PvE while the server enforces PvP. A player who trusts
  the banner can be killed by a rule their HUD said did not apply.

So every surface says it plainly: the portal, the rules, Discord, and a one-line whisper on every arrival
(below). Walk every area before you call it safe, and publish only what you walked.

We placed CHOAM exchange terminals inside each safe area so players can trade without leaving cover. The terminal
placement SQL carries live entity ids and is not published.

## Jobs that run around the map

All of them follow the house pattern: a systemd timer, dry run until a live flag file exists, a kill switch file,
state under `/var/lib/`, and results posted to the ops channel.

### Arrival placer

A player arriving from another map lands wherever the travel system puts them, which on a full PvP map can be in
the open, next to someone waiting. The placer:

1. Each tick, reads every online character's current map. A change to the PvP map from another map marks the
   character pending. The overmap in between is ignored.
2. Waits for the server log line that marks the player in control. A teleport sent during the loading screen is
   dropped by the game.
3. Skips characters still in the opening tutorial.
4. Teleports the rest to a base on this map that they own (owner rank only, not member rights), or, if they own
   none, to their faction's safe area. Anyone without a faction gets a coin flip.

A relog on the PvP map is not an arrival (the last map seen is still the PvP map), so it cannot be used to escape a
fight. Teleports go through `scripts/dune-server-command.py`, which refuses the verb unless the caller enables it
explicitly.

### Arrival whisper

Every player arriving on the PvP map gets a one-line PvP warning in game, every arrival, base owners included. The
state file holds only the players warned during their current stay; anyone who logs off or leaves is dropped from
it after a successful query, so their next arrival is warned again, and a database blip cannot re-warn everyone. A
failed whisper is not recorded, so it retries. A logged-out character stays in the world for a few minutes, so a
quick relog is not a new arrival.

### PvP board, titles and streak bounties

The telemetry service folds the PvP map's death records into a board: kills, deaths, K/D and best streak for the
week and the season, and who is on a streak right now. The portal's PvP page shows it, and everything below reads
the same fold, so no two surfaces can disagree.

| Title | Who |
|---|---|
| Sardaukar of the Week | Most player kills last week, minimum 2 |
| Kanly | Longest kill streak last week, minimum 3 |
| Headtaker | Collected a streak bounty (permanent) |
| Blooded | 10 kills this season (permanent) |

We started with more titles and cut to four a small population can actually earn. Staff appear on the board but
never hold a title; the next eligible player gets it.

- **Discord roles.** A job mirrors the titles onto cosmetic Discord roles (no permissions, not hoisted, not
  mentionable) for players who linked their account in the portal. It only adds and removes those roles. A failed
  or empty read changes nothing: a title is never stripped because the board could not be read.
- **Streak bounties.** A player on a streak of 5 or more who is killed by another player pays out to the killer:
  2,500 spice per 5 kills of the ended streak. The payout is void if the two share a guild or a base claim, so nobody
  farms a guildmate's streak. Payouts go to online collectors only and are confirmed by reading the item back;
  offline collectors stay pending and retry for 14 days. Entries from before the job's first live tick never pay,
  so switching it on pays nothing retroactively.

## Things we tried and dropped

- **Blocking moved-in bases.** For a while bases on the PvP map had to be built there or from a Solido blueprint,
  not moved in with the Base Reconstruction Tool. Once the PvP map was the only PvP sietch we allowed the tool, so
  players can back a base up and take a break.
- **A PvP ledger and bounty board** in the portal. Retired; not shipped.

## Related

- [16-pvp-maps.md](16-pvp-maps.md): adding a PvP partition
- [21-custom-settings-and-rates.md](21-custom-settings-and-rates.md): per-map arguments and rates
- [20-player-portal.md](20-player-portal.md): the PvP page, board and titles in the portal
- [08-updates.md](08-updates.md): the per-pod arguments and the runtime flag on update day
