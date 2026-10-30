# Last Sietch

The operational stack behind a real, self-hosted **Dune: Awakening** community server, published so other operators do not have to rediscover it. It runs on bare-metal Debian and k3s, outside Funcom's Windows and Hyper-V wizard, and this snapshot is tested against server build `2168446` (Update 1.5.15.3) on k3s 1.35.9.

> v0.8 brings the stack up to the post-1.5 game (1.5.15.x). It documents the in-game Custom Settings
> screen and per-map rates, adds a second sietch run as a full PvP map with safe settlements, a weekend
> events runner, a base backup and restore tool, and a large portal update (new Home, Sand Table maps,
> titles, self-rescue). v0.8.1 makes map instance names configuration, not code, and adds player requests
> to staff and a map-close tool. Start with [`docs/README.md`](docs/README.md).

## Start here

1. **Stand it up:** [01 install](docs/01-install.md), [02 canonical config](docs/02-canonical-config.md), [03 dual Deep Desert](docs/03-dual-deep-desert.md).
2. **See what runs where:** [25 host topology](docs/25-host-topology.md).
3. **Before your first update day:** [08 updates](docs/08-updates.md), including the regression checklist.
4. **Then the feature docs you need:** [21 Custom Settings and per-map rates](docs/21-custom-settings-and-rates.md), [16 PvP maps](docs/16-pvp-maps.md) and [22 a full PvP sietch](docs/22-amtal-pvp-sietch.md), [23 events runner](docs/23-events-runner.md), [24 Base Vault](docs/24-base-vault.md), [20 player portal](docs/20-player-portal.md).

The full list, in reading order, is [docs/README.md](docs/README.md). What changed between versions is in [CHANGELOG.md](CHANGELOG.md). Before you publish your own fork, read the [publication checks](docs/18-publication-checks.md).

## How the pieces fit

```text
browser
  |  HTTPS
Caddy
  |  reverse proxy
admin-backend            portal + admin panel. SQLite only. No game DB access
  |  HTTP + X-API-Key
relay                    the only component that can reach the game host
  |  SSH, forced command
dispatcher               fixed allowlist of actions, on the game host
  |  exec
action script            one script per action, each doing one thing
  |
dq.sh -> kubectl exec -> psql
```

The web tier never holds a game database credential; its whole vocabulary is the dispatcher's allowlist. Why, and what each hop is for: [15 control plane architecture](docs/15-control-plane-architecture.md). Which host runs which piece: [25 host topology](docs/25-host-topology.md).

## What is in the repository

| | What it is |
|---|---|
| [docs/](docs/) | Install, configuration, operations and feature docs, indexed in [docs/README.md](docs/README.md) |
| [scripts/](scripts/) | The on-host toolkit: `dq.sh` database access, the forced-command dispatcher and the action scripts it routes to (grants, bans, storage, guilds, market, bases, blueprints, telemetry feeds), presence guard, pre-window readiness, backups, schema ownership check, memory limits, config watch |
| [ops/](ops/) | Battlegroup watchdog, update orchestration and hotfix watching, PvP partition pinning, and the v0.8 tools: per-map rates, the full PvP map jobs, the events runner, Base Vault, welcome pack |
| [relay/](relay/) | The Dune-only relay: the web-tier side of the control plane |
| [dune-telemetry/](dune-telemetry/) | Presence, world events, combat, market and progression collection. Standalone |
| [discord/cielago-bot/](discord/cielago-bot/) | The support bot: help-channel tickets, digests, in-game chat herald, server status, onboarding, events sync |
| [admin-backend/](admin-backend/) | The player portal and admin panel: FastAPI API server, and under `portal-nextgen/` the SvelteKit front end |
| [references/](references/) | Canonical item ids, community-sourced field notes |

### What ships, what you regenerate, what is not provided

| Class | Examples | How you get it |
|---|---|---|
| Ships | Code, configuration examples, docs, curated tables that are ours (crest map, spice sites, settings catalog, presets) | `git clone` |
| Regenerated on your machine | Item icons and names, map glyphs, fonts, the three.js core, map marker snapshots from your own database | `scripts/build-portal-assets.py`, `scripts/dune-markers-export.py` ([20](docs/20-player-portal.md)) |
| Not provided | Anything extracted from the game: baked 3D relief, Sand Table miniatures, map backdrops. Our own art and branding. Player data. Deployment values | Yours to make, or leave out: the portal renders without them |

## Tested against

This is reference source, not a production configuration. Each row is what we ran at that point; it is not a blanket compatibility guarantee.

| | |
|---|---|
| Self-host product | Steam app ID `4754530`, "Dune: Awakening Self-Hosted Server" |
| **This snapshot (v0.8)** | `seabass-server:2168446-0-shipping` (Update 1.5.15.3), k3s `v1.35.9+k3s1`, October 2026 |
| Player portal snapshot (v0.7) | `seabass-server:2111270-0-shipping` (Update 1.5.3), k3s `v1.34.11+k3s1`, September 2026 |
| Later maintenance observations | `seabass-server:2064155-0-shipping`, September 2026 |
| Historical install walkthrough | `seabass-server:2051294-0-shipping`, July 2026 |
| Funcom operators | `v1.7.0` (battlegroup, database, server, utilities), at Update 1.5.15.3 |
| Host OS | Debian 12 reference; Debian 13 package compatibility notes are not a full deployment certification |
| Database | `igw-postgres:17.4-alpine-fc-13`, at Update 1.5.15.3 |

Funcom ships updates frequently and changes operator behaviour with them. If your versions differ from these, expect drift, and read before you run.

The private production rolling controller is deliberately not included: a portable release needs an independently reviewed credential-proof provider and operator configuration. The update orchestrator is disabled until explicitly configured and enabled.

## Why this exists

Funcom ships self-hosted servers as a Windows-wizard-driven Hyper-V VM image. The Linux dedicated-server payload packaged inside that VM is the same workload this repository deploys directly on bare-metal Debian, without the Hyper-V layer. Funcom's own self-hosting FAQ acknowledges this is possible; it is simply not the supported path, so nobody documents it.

We had to work it out to run our own community. A player asked whether we shared any of it. We did not, so now we do.

## What is deliberately not here

Being explicit, because the gaps are intentional and you will notice them:

- **No Funcom code, binaries, container images, or server-side stored procedures.** Not extracted, not paraphrased, not included.
- **No game assets.** No meshes, textures, icons, or art extracted from the game's package files. Tooling that generates what it needs on *your* machine is fine, and that is how the portal gets its icons and map glyphs: `scripts/build-portal-assets.py` pulls them from the community wiki's public API (every item a player can see or trade), and the rest fall back to a generic glyph. The baked 3D relief and our own visual dressing (hero art, posters, crests, PWA icons) are not shipped either; the portal renders without them.
- **No binary patching or package-signing tooling.** We have some. It stays private. Where a Funcom bug has an operational workaround, the symptom and the detection method are documented so you can recognise it, without shipping the patch itself.
- **No exploit paths.** Where we have found something that could be abused, it is not published. Some of it becomes a defensive rule here, stated as the operator guidance without the mechanism.
- **No player data.** No names, ids, coordinates, chat logs, or ticket text.
- **No scraped third-party content.** Other communities' Discord servers are theirs.

## Scope and support

This is a **reference deployment, published as-is**. It is not a product, it is not supported, and it comes with no warranty beyond what the licence says.

- It reflects one server's configuration, on one hardware profile, at one point in Funcom's patch cycle. Read before you run.
- Funcom can change the wizard, the CR schema, or operator behaviour in any update, and periodically does. Audit before you trust.
- Issues and pull requests are welcome and will be read. Response time depends entirely on what the live server is doing that week.
- Anything that touches a running server should be understood before it is executed. Several of these tools write to a live game database.

## Credit where it is due

Thanks to **Funcom** for shipping self-hosted servers at all. Plenty of studios treat server software as a black box and lock it down. Funcom built a well-architected k3s-based system, packaged it cleanly, and put it in players' hands, and the fact that it *also* runs cleanly outside their Hyper-V wrapper is what makes any of this possible. The Funcom-shipped `bootstrap/setup` script and `world-template.yaml` are the foundation; everything here is a layer on top.

Thanks also to the wider self-hosting community, whose published tooling and field notes closed gaps we would otherwise have hit blind.

## Licence

Code and configuration are MIT. Documentation is CC-BY-4.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Dune: Awakening and related trademarks are the property of Funcom Oslo AS and Legendary Entertainment. This project is an independent community effort and is not affiliated with, endorsed by, or sponsored by Funcom or any Dune rights holder.
