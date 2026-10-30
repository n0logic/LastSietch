# Changelog

Newest first. Only v0.7 and v0.8 are tagged. Earlier entries are reconstructed from this repository's commit
history; where the history does not mark a version boundary, the range is grouped rather than given invented dates.

## v0.8: post-1.5 snapshot (unreleased)

Tested against server build `2168446` (Update 1.5.15.3) on k3s 1.35.9. The private commit this snapshot is cut from
is recorded here at release.

v0.8 brings the stack up to the post-1.5 game (1.5.15.x). It documents the in-game Custom Settings screen and
per-map rates, adds a second sietch run as a full PvP map with safe settlements, a weekend events runner, a base
backup and restore tool, and a large portal update (new Home, Sand Table maps, titles, self-rescue). Map instance
names are now configuration, not code.

**Docs**

- New: `docs/21` Custom Settings, per-map rates and the rules register; `docs/22` running a second sietch as full
  PvP; `docs/23` the weekend events runner; `docs/24` Base Vault backup and restore; `docs/25` host topology.
- `docs/08` extended: the hotfix procedure as we run it now, an update-day regression checklist, and closing a map
  instance.
- `docs/16` rewritten as the PvP maps doc for the current model, with a build history. Renamed from
  `16-hagga-pvp-partition.md` to `16-pvp-maps.md`.
- `docs/16-persistent-map-maintenance.md` renumbered to `docs/09-persistent-map-maintenance.md`, filling the gap.
- `docs/20` updated for the v0.8 portal and its new variables. `docs/11` updated for welcome pack v1.8 and the 1.5.3
  currency enum. `docs/15` points to `docs/25` for the request chain.
- New `docs/README.md` index in reading order; README rewritten around a "Start here" path, with the pinned build
  and a corrected version table.

**Components**

- Portal and admin panel: new Home (off by default), Sand Table maps with the storm layer off, PvP page with board
  and titles, self-rescue on PvE maps, weekend events card, Help and rules updates, Events Manager in the admin
  panel, 1.5.x schema fixes.
- Full PvP map jobs: runtime PvP flag watcher, arrival placer, arrival whisper, title roles, streak bounties.
- Events runner, Base Vault, per-map rates tooling, config watch v2, welcome pack v1.8.
- Map instance names and partition ids moved from code to configuration across the portal, API server, relay and
  Discord bot.
- Publication gate hardening.

## v0.7: player portal (2026-09-17)

- The player portal and admin panel under `admin-backend/`, code only: FastAPI API server and the SvelteKit front
  end.
- `scripts/build-portal-assets.py`: regenerates icons, names, glyphs and fonts from public sources, so no game
  asset is committed.
- `docs/20-player-portal.md`, the env template `admin-backend/.env.example`, NOTICE and ignore rules.
- Publication gate corrections.

## Between v0.6 and v0.7 (untagged, 2026-08-11 to 2026-09-09)

- 2026-08-11: the PvP Hagga partition guide (then `docs/16-hagga-pvp-partition.md`), PvP partition pinning,
  CPU-weight boot-contention mitigation, backups with consistent SQLite snapshots, augment reroll and swap writer,
  market source gate, repair and Karum gates, Discord bot intake and report flow.
- 2026-08-12 to 2026-08-14: Debian 13 compatibility notes, the PvP coverage correction (zone-gated, not world-wide),
  the automated Debian installer `scripts/install.sh` and its prerequisites.
- 2026-08-15: publication gate closes the gaps that let reverse-engineering detail and foreign paths through.
- 2026-09-09: deployment defaults removed and publication gates hardened; maintenance guidance (map maintenance,
  server time, publication checks, configuration changes) and read-only schedule tools.

## v0.6: the Discord support bot (2026-08-01)

- `discord/cielago-bot/`: help-channel ticket classification and dedup, Claim, Escalate and Resolve buttons,
  feature-request upvotes, daily and weekly digests, in-game chat herald. Self-contained, no game writes.

## v0.2 to v0.5 (untagged, 2026-05-20 to 2026-08-01)

The history does not mark these boundaries.

- 2026-05-20: initial public release as `dune-linux-tools`: docs 01 to 08, 10 and 11 (install, canonical config,
  dual Deep Desert, server browser visibility, display name, memory tuning, troubleshooting, updates, director UI
  access, welcome package), item references, and the apply, audit and welcome-pack scripts.
- 2026-08-01: relaunched as Last Sietch with the publication gate and the MIT and CC-BY-4.0 licence split.
- 2026-08-01: operations docs 12 to 15, the relay, the forced-command dispatcher and `dq.sh`, the battlegroup
  watchdog and update orchestration, telemetry, then all 54 action scripts, completing the control plane.
