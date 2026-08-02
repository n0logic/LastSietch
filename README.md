# Last Sietch

The operational stack behind a real, self-hosted **Dune: Awakening** community server, published so other operators do not have to rediscover it.

Last Sietch runs on bare-metal Debian and k3s, outside Funcom's Windows and Hyper-V wizard. Everything here came out of actually running that server for players: the install path, the config that survives a Funcom update, the things that break at 2am, and the tooling built on top.

## Why this exists

Funcom ships self-hosted servers as a Windows-wizard-driven Hyper-V VM image. The Linux dedicated-server payload packaged inside that VM is the same workload this repository deploys directly on bare-metal Debian, without the Hyper-V layer. Funcom's own self-hosting FAQ acknowledges this is possible; it is simply not the supported path, so nobody documents it.

We had to work it out to run our own community. A player asked whether we shared any of it. We did not, so now we do.

## What is here today

| | |
|---|---|
| [docs/](docs/) 01 to 11 | Fresh install on Debian 12, canonical config, dual Deep Desert, server browser visibility, display names, memory tuning, troubleshooting, update procedure, BGD admin UI access, welcome package design |
| [docs/12-operations.md](docs/12-operations.md) | Keeping a server alive: the tools, the `pg_dump` trap, restart rules, free maintenance windows, backups, and the failures that are quiet |
| [docs/13-safe-database-writes.md](docs/13-safe-database-writes.md) | Read before writing to the game database. Giving is safe, taking is not, and why |
| [docs/14-known-funcom-issues.md](docs/14-known-funcom-issues.md) | Symptoms and detection for problems we have hit, so you can identify them in minutes instead of an evening |
| [docs/15-control-plane-architecture.md](docs/15-control-plane-architecture.md) | How a web app safely drives a live game server it must never touch directly. The one idea here most worth copying |
| [references/](references/) | Canonical item ids, community-sourced field notes |
| [scripts/](scripts/) | The whole on-host toolkit: `dq.sh` database access, the forced-command dispatcher, and the 54 action scripts it routes to (grants, bans, storage, guilds, market, bases, blueprints, telemetry feeds), plus presence guard, pre-window readiness, backups, schema ownership check, memory limits |
| [ops/](ops/) | Battlegroup watchdog (a stopped battlegroup never recovers itself), update orchestration and hotfix watching |
| [relay/](relay/) | The Dune-only relay, 106 routes. The web-tier side of the control plane |
| [dune-telemetry/](dune-telemetry/) | Presence, world events, combat, market and progression collection. Standalone and usable today |
| [discord/cielago-bot/](discord/cielago-bot/) | The support bot: watches help channels, classifies and dedups player reports into tickets, posts daily and weekly digests, in-game chat herald |

## Tested against

Everything here is in production on one server. What that server currently runs:

| | |
|---|---|
| Self-host product | Steam app ID `4754530`, "Dune: Awakening Self-Hosted Server" |
| Steam buildid | `24376904` (installed 2026-07-24) |
| Server image | `seabass-server:2051294-0-shipping` |
| Funcom operators | `v1.5.0` (battlegroup, database, server, utilities) |
| Host OS | Debian 12 bookworm, kernel 6.1.0-50-amd64, dedicated hardware, public IPv4 |
| k3s | `v1.34.5+k3s1`, single node, containerd 2.1.5-k3s1 |
| Database | `igw-postgres:17.4-alpine-fc-13` |

Funcom ships updates frequently and changes operator behaviour with them. If your
versions differ from these, expect drift, and read before you run.

## What is coming

This repository is being expanded from a private monorepo, in stages, with each stage scrubbed of deployment-specific values before it lands. Rough order:

1. ~~**Operations.**~~ Landed: see `docs/12` through `docs/14`, `scripts/`, and `ops/`.
2. **Player portal.** A player-facing web portal: character and inventory views, CHOAM market browse and sell, storage management, coordinate-accurate Deep Desert and Hagga maps from your own database, guild directory, Landsraad board, and a 3D base blueprint viewer. This is the piece nothing else in the ecosystem currently offers. The control plane it drives is already here; what is missing is the front end.
3. ~~**Telemetry and Discord.**~~ Landed: see `dune-telemetry/` and `discord/cielago-bot/`.

No dates. This is volunteer work done around running an actual server, and a half-working portal helps nobody.

## What is deliberately not here

Being explicit, because the gaps are intentional and you will notice them:

- **No Funcom code, binaries, container images, or server-side stored procedures.** Not extracted, not paraphrased, not included.
- **No game assets.** No meshes, textures, icons, or art extracted from the game's package files. Tooling that generates what it needs on *your* machine from *your* licensed install is fine, and that is how the portal gets its icons: roughly 86% of item templates resolve to their real icon from public community sources, and the rest fall back to a generic glyph.
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
