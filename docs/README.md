# Docs index

In reading order. If you are new, read 01 to 03 to stand a server up, 25 for what runs where, and 08 before your
first update day. Then pick the feature docs you need.

| # | Doc | For | Purpose |
|---|---|---|---|
| **Stand it up** | | | |
| 01 | [Install on Debian 12](01-install.md) | New operators | Fresh bare-metal install outside Funcom's Hyper-V wizard, with Debian 13 notes |
| 02 | [Canonical config](02-canonical-config.md) | Operators | The canonical config baseline and how to reapply it |
| 03 | [Dual Deep Desert](03-dual-deep-desert.md) | Operators | A PvP and a PvE Deep Desert side by side; the partition concepts every map doc builds on |
| 04 | [Server browser visibility](04-server-browser-visibility.md) | Operators | Why a "Healthy" server is not listed, and how to check |
| 05 | [Display name](05-display-name.md) | Operators | The battlegroup title and per-sietch names |
| 06 | [Memory tuning](06-memory-tuning.md) | Operators | Per-map memory limits and host sizing |
| 07 | [Troubleshooting](07-troubleshooting.md) | Operators | Symptoms and fixes from real installs |
| **Run it** | | | |
| 25 | [Host topology](25-host-topology.md) | Operators, web tier | What runs where, the request chain, single box versus split, a Caddy example |
| 08 | [Updates](08-updates.md) | Operators | Update day: the hotfix shape, the regression checklist, closing a map instance |
| 09 | [Persistent map maintenance](09-persistent-map-maintenance.md) | Operators | Sietch instances versus map families, empty-state checks, backup and recovery boundaries |
| 10 | [BGD admin UI access](10-bgd-admin-ui-access.md) | Operators | Reaching the director UI safely, never publicly |
| 12 | [Operations](12-operations.md) | Operators | The tools, restart rules, free maintenance windows, backups, quiet failures |
| 13 | [Safe database writes](13-safe-database-writes.md) | Anyone writing to the game DB | Giving is safe, taking is not, and why |
| 14 | [Known Funcom issues](14-known-funcom-issues.md) | Operators | Symptoms and detection for problems we have hit |
| 17 | [Server time](17-server-time.md) | Tool builders | Read-only server clock and recurrence display helpers |
| **Configure the game** | | | |
| 21 | [Custom Settings and rates](21-custom-settings-and-rates.md) | Operators | The in-game Custom Settings screen, the settings re-base, config watch, per-map rates, the rules register |
| 16 | [PvP maps](16-pvp-maps.md) | Operators | Adding a PvP Hagga partition; what the opt-in covers; build history |
| 22 | [Full PvP sietch](22-amtal-pvp-sietch.md) | Operators running PvP | Per-pod settings, the runtime PvP flag, safe areas, arrival placer, board, titles and bounties |
| **Build on it** | | | |
| 15 | [Control plane architecture](15-control-plane-architecture.md) | Anyone building a web tier | How a web app drives a live game server it must never touch directly |
| 20 | [Player portal](20-player-portal.md) | Web tier operators | What the portal does, install, configuration, assets, what is new in v0.8 |
| 11 | [Welcome package](11-welcome-package-design.md) | Operators | Granting new players a starter kit safely |
| 23 | [Events runner](23-events-runner.md) | Operators | Weekend events started, kept and ended from the admin panel |
| 24 | [Base Vault](24-base-vault.md) | Operators | Capturing a base outside the game and restoring it |
| **Publish your own** | | | |
| 18 | [Publication checks](18-publication-checks.md) | Contributors | Staged-content, privacy, secret and provenance checks before publishing |
| 19 | [Configuration changes](19-configuration-changes.md) | Upgraders | Repository configuration that became explicit in the September refresh |

Versions and what changed between them: [CHANGELOG.md](../CHANGELOG.md).
