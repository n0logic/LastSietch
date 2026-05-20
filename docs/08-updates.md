# 08 - Updates

Funcom ships new server builds regularly. Keeping your server build current is not optional cosmetic maintenance - a stale build breaks server browser visibility entirely.

## Why updates matter

If your running server build falls behind Funcom's current build, the older Battlegroup Director silently stops calling `Battlegroups_DeclareBattlegroupUpdates` (and `Director_InitializeDirector`) while still sending heartbeats. The Battlegroup reports `Healthy`, every pod is `Ready`, and the server is invisible in the in-game browser. See [04-server-browser-visibility.md](04-server-browser-visibility.md) for the full mechanism.

In short: **build skew is the first thing to check when a "Healthy" server isn't listed.** Updating promptly avoids the problem entirely.

The Steam app ID for the production self-host server is **`4754530`** ("Dune: Awakening Self-Hosted Server").

## Update procedure (bare-metal Linux)

Funcom's wizard-bundled `battlegroup update` flow assumes a few things that are true inside their Hyper-V VM image but not on a fresh bare-metal Linux host. The steps below cover the workarounds.

### Step 1 - Download the new server build via SteamCMD

Pull the new build with SteamCMD:

```bash
sudo -u dune steamcmd \
  +force_install_dir /home/dune/.dune/download \
  +login <steam-account> \
  +app_update 4754530 \
  +quit
```

Two things to be aware of:

- **Authenticated login is required.** Anonymous Steam login (`+login anonymous`) can be refused - observed (unconfirmed): some hosting-provider IP ranges receive "Access Denied" on the manifest for this app, even though anonymous login works fine for many other Steam apps. Use an authenticated Steam account that owns the product. Steam caches a refresh token after the first interactive login, so subsequent updates do not need a password.
- **The Steam Linux Runtime dependencies must be pre-installed.** Funcom's bundled VM image has these baked in; a fresh Linux host does not. If they are missing, SteamCMD aborts with a `missing required app` error. Install the runtime dependency apps once via SteamCMD before your first update.

Confirm the download finished with a `Success! App '4754530' fully installed.` line.

### Step 2 - Ensure the containerd socket symlink exists

The Funcom battlegroup tooling imports the new container images with `ctr`, which expects containerd's socket at the default path. k3s places its socket elsewhere. If the symlink bridging the two is missing, image import fails.

Verify it exists (it should already be in place from install - see [01-install.md](01-install.md)):

```bash
sudo ls -la /run/containerd
# Expect: /run/containerd -> /run/k3s/containerd
```

If it is missing, recreate it via `tmpfiles.d` so it persists across reboots (see [01-install.md](01-install.md) Step 4).

### Step 3 - Run the battlegroup update step

Run the Funcom tooling's update-from-downloads flow, which imports the freshly downloaded images and patches the Battlegroup CR to reference the new image tags:

```bash
sudo -u dune /home/dune/.dune/bin/battlegroup update-from-downloads
```

Use `update-from-downloads`, not `battlegroup update`. The plain `update` command re-runs the SteamCMD phase with anonymous login hardcoded, which undoes the authenticated download from Step 1. `update-from-downloads` skips the SteamCMD phase and goes straight to:

1. `ctr` image import for each Funcom container tarball (server, BG director, gateway, text router, RabbitMQ, DB utils)
2. A `kubectl patch` updating every image reference in the Battlegroup CR to the new tag

The Battlegroup operator then rolls all game pods automatically. The UE5 server image is large, so expect 60-120 seconds of image extraction per pod.

## Re-apply your canonical config

A Funcom image update can reset the deployed `UserGame.ini` / `UserEngine.ini` to defaults and revert CR-level customizations. After every update, re-apply your canonical config:

```bash
scripts/apply-canonical.sh
scripts/audit.sh
```

`apply-canonical.sh` re-applies the customizations idempotently. `audit.sh` then verifies that the things most likely to have been reset survived:

- The dual Deep Desert configuration (second DD partition, `replicas: 2`, `dedicatedScaling: false`)
- The Battlegroup title and the sietch display name
- The PvP partition selection in `UserGame.ini`
- Any custom per-map memory limits

See [02-canonical-config.md](02-canonical-config.md) for the reapply playbook and [03-dual-deep-desert.md](03-dual-deep-desert.md) for the dual-DD config the audit checks.

## Post-update verification

Pod phase is not the real "is it working" check. After the rolling restart settles:

1. Confirm all game pods are `Running` and the Battlegroup status shows Database `Running`, Gateway healthy, Director `Healthy`.
2. **Confirm the BGD is firing `Battlegroups_DeclareBattlegroupUpdates` again.** This is the real verification - a `Healthy` phase with no `DeclareBattlegroupUpdates` calls means the server is invisible. See [04-server-browser-visibility.md](04-server-browser-visibility.md) for how to grep the BGD logs, or just run `scripts/audit.sh`.
3. Open the in-game server browser and confirm your server appears under the correct region. Allow ~2 minutes after the Declare call fires.

## Rollback

If an update breaks the Battlegroup, you can patch the CR's image tags back to the previous build - but only if the previous build's image tarballs are still imported into containerd. Keep at least one prior build's tarballs around until the new build is verified browser-visible and has accepted at least one player connection.

## Related documentation

- [04-server-browser-visibility.md](04-server-browser-visibility.md) - why build skew breaks visibility
- [02-canonical-config.md](02-canonical-config.md) - the canonical config reapply playbook
- [07-troubleshooting.md](07-troubleshooting.md) - "Funcom update wipes UserSettings/*.ini"
