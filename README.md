# n0logic Dune Linux Server

Community-driven guide for self-hosting **Dune: Awakening** on bare-metal **Linux** (Debian 12 + k3s), built from the work of n0logic's deployment.

Funcom's official Update 1.4 (May 19 2026) ships self-hosted servers as a Windows-wizard-driven Hyper-V VM image. The Linux dedicated server payload they package inside the VM is the same workload this guide deploys directly on bare-metal Debian - without the Hyper-V layer.

## Why this guide exists

The official Funcom path is Windows-only: download the Steam product → run a `.bat` → it provisions a Hyper-V VM with Debian inside → the wizard SSH's into that VM to bootstrap. That works great for Windows users running locally, but for anyone who wants to host on a Linux VPS, dedicated server, or homelab box, the wizard isn't applicable.

The Funcom-shipped `bootstrap/setup` script doesn't actually care that it's running inside a Hyper-V VM. Drop it on a Debian 12 host with the right prereqs, point it at the GA Steam product, and the same flow completes natively. This guide documents that path step by step.

If you'd rather not hand-roll k3s, CubeCoders AMP can run the same Funcom server container images on plain Linux/Docker as an alternative deployment path.

## What you get

- **A bare-metal Linux Dune Awakening server** on the same product Funcom ships (Steam app ID 4754530 - "Dune: Awakening Self-Hosted Server")
- **Canonical config** as a layer on top of the wizard output:
  - Dual Deep Desert (PvP partition + PvE partition) so your community can pick the flavor at the instance picker
  - Custom sietch display name in the in-game server browser
  - Per-map memory and scaling tuning for boxes well above Funcom's 20 GB minimum tier
  - Configurable PvP zones, building limits, sandworm behavior, mining multipliers, etc.

## Status

Active deployment + documentation work in progress. Tested against:

- **Game version**: Dune: Awakening 1.4.0.0 (GA, released 2026-05-19)
- **Self-host product**: Steam app ID 4754530 ("Dune: Awakening Self-Hosted Server"), buildid 23301681
- **Host OS**: Debian 12 on a dedicated server with a public IPv4
- **k3s**: v1.34.5+k3s1
- **Funcom operators**: v1.5.0

## Documentation index

| File | Contents |
|---|---|
| [docs/01-install.md](docs/01-install.md) | Fresh-install procedure on Debian 12 |
| [docs/02-canonical-config.md](docs/02-canonical-config.md) | Canonical config layered over the Funcom wizard output |
| [docs/03-dual-deep-desert.md](docs/03-dual-deep-desert.md) | Standing up both a PvP and PvE Deep Desert under one sietch |
| [docs/04-server-browser-visibility.md](docs/04-server-browser-visibility.md) | How servers get listed in the Experimental tab + debugging when they don't |
| [docs/05-display-name.md](docs/05-display-name.md) | Sietch / server browser display name configuration |
| [docs/06-memory-tuning.md](docs/06-memory-tuning.md) | Per-map memory budgets for hosts with more than 20 GB |
| [docs/07-troubleshooting.md](docs/07-troubleshooting.md) | Common failure modes and fixes |
| [docs/08-updates.md](docs/08-updates.md) | Funcom update procedure and what to re-apply afterward |
| [docs/10-bgd-admin-ui-access.md](docs/10-bgd-admin-ui-access.md) | Securely accessing the Battlegroup Director admin web UI |
| [docs/11-welcome-package-design.md](docs/11-welcome-package-design.md) | Designing a sietch welcome package for new players |
| [references/](references/) | Funcom artifact excerpts (template YAML structure, wizard prompt order, etc.) |
| [scripts/](scripts/) | Helper scripts - config audit, backup, restore |

## Credit where it's due

Huge thanks to **Funcom** for shipping self-hosted servers in the first place. They didn't have to. Plenty of MMOs treat their server software as a black box and lock it down. Funcom built a well-architected k3s-based system, packaged it cleanly, and put it in players' hands - and the fact that it _also_ runs cleanly outside of their Hyper-V wrapper is what makes this guide possible at all. The Funcom-shipped `bootstrap/setup` script and `world-template.yaml` are the foundation; everything in this repo is a thin documentation layer on top.

If you self-host using this guide, please remember the work that went into making this possible. Drop by the official PTC Discord if you can offer field feedback to Funcom's team - they actively read it.

## Disclaimer

This guide is a community effort and is **not endorsed by Funcom**. The Linux-on-bare-metal path is not officially supported; Funcom's official path is Windows + Hyper-V. Everything here works at the time of writing, but Funcom can change the wizard, the CR schema, or the operator behavior in any update. Audit before you trust.

The configuration patterns documented here are reverse-engineered from real deployment experience plus the schemas already visible in Funcom's own setup scripts. We're sharing because the community asked for it, not because we have any special access.

## Contributing

If you stand up a Linux Dune server with this guide and hit a snag, open an issue. Patches welcome for new map IDs, new wizard versions, or different host distributions.

## License

Code and configuration snippets in `scripts/` are MIT-licensed. Documentation in `docs/` is CC-BY-4.0. Funcom and Dune: Awakening assets / trademarks are not redistributed here - only references with attribution.
