# Contributing

Thanks for considering a contribution. This project tracks a moving target - Funcom can change the operator schema, the wizard, or the world template at any update - so contributions that capture new failure modes or new map IDs are especially welcome.

## What to contribute

- **Bug reports** for any step in `docs/01-install.md` that doesn't work on a fresh Debian 12 host
- **Patches** for new Funcom map IDs that get added in future updates (especially anything that breaks our partition ID assumptions)
- **Alternative distro guides** - currently only Debian 12 is covered, but Ubuntu Server / Rocky / Alpine could be added under `docs/`
- **Audit script improvements** in `scripts/`
- **Translation or clarity edits** to existing docs

## What NOT to contribute

- **JWT tokens, secrets, FLS keys, or any other credentials** - the `.gitignore` blocks most filenames but please review your diff manually
- **Redistributed Funcom assets** - container images, binaries, copyrighted text from their docs. Quote sparingly with attribution; never bundle.
- **Player data / database dumps** - if you have a backup containing player saves, scrub it before sharing
- **Game-derived assets** - meshes, textures, icons, or anything else extracted from the game's package files. Tooling that generates these locally from someone's own installation is welcome; its output is not.
- **Other communities' content** - Discord logs, forum posts, or wiki text scraped from elsewhere. Link to it instead.

## Deployment-specific values

Nothing in this repository should contain a real hostname, IP, namespace, snowflake, or credential from anyone's actual deployment, including ours. The convention is a placeholder that documents its own shape, with an environment variable override where a script needs one:

```bash
NS="${NS:-funcom-seabass-sh-<your-hostid>-<random>}"
```

So a reader can see what the value looks like, an operator can export it, and nobody has to guess. Angle-bracket placeholders in prose and command examples, same idea.

CI enforces this. Every push and pull request runs a secret scan plus two gates: one rejecting file classes that must never be committed, and one rejecting known deployment-specific identifiers. If a gate fires on something legitimate, say so in the PR rather than working around it; a check that flags correct work is a bug in the check.

## Workflow

1. Fork
2. Create a topic branch - `add-ubuntu-instructions`, `fix-id-conflict-for-1.5-update`, etc.
3. Test against a fresh host where possible
4. Open a PR with a clear description of what changed and which version of the game you tested against

## Commit style

Conventional commits welcome but not required. Just make it clear what changed:

```
docs: document partition id remapping when Funcom adds new maps
fix: correct UserGame.ini sed pattern to handle leading semicolon
add: ubuntu 24.04 setup walkthrough
```

## Security disclosures

If you find a security issue in the operator workflow or the canonical config that could expose other deployments to risk, please open a private issue or email the maintainers first rather than filing publicly.

## Code of conduct

Be kind. The Dune self-host community is small and most of us are figuring this out in parallel. Disagree about technical approaches, never about people.
