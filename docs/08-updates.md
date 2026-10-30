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

This is the simple path. Once your BattleGroup carries per-pod arguments
(per-map rates, a PvP map's settings) or a customised `director.ini`, use the hotfix shape below instead: on our host
Funcom's update command replaced `director.ini` with stock and dropped a per-pod argument.

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

## The hotfix shape (what we run now)

This is the procedure that settled out of the 1.5.3.x to 1.5.15.x hotfixes. Each step is run by a person after the
owner's go; the only automation is a notify-only watcher that says a new build is out.

| # | Step | Notes |
|---|---|---|
| 1 | **Pre-checks**, read-only | Disk, latest backups, player presence, SHAs of the four settings files, current image refs, ownership of every table in the game schema. |
| 2 | **Stage and diff** the new build | Download into a fresh intake directory (`+app_update 4754530 validate`). Diff `scripts/`, `setup/config/*.ini`, `setup/templates/world-template.yaml`, the operator CRDs and image sizes against the previous intake. The new image tag is in `images/battlegroup/version.txt`. A changed settings template means a re-base ([21](21-custom-settings-and-rates.md#funcoms-template-and-the-re-base)). |
| 3 | **Database dump** | `pg_dump -Fc` from the database pod over TCP (the pod has no unix socket). Check the size and the `PGDMP` header. |
| 4 | **Drop your own schemas** | If you keep tables in a schema of your own, back it up, verify the backup, and drop it before the update. Funcom's migration replays a dump into one temporary schema and aborts on a foreign `CREATE TABLE`. Every table in the game schema must be owned by the game's database user, or the pre-migration dump fails. |
| 5 | **Import images** before the stop | `k3s ctr -n k8s.io images import` each tarball. Importing while the fleet runs shortens the downtime. |
| 6 | **Notices** | Discord, then in game at ten minutes and one minute. |
| 7 | **Stop** to zero | `spec.stop=true`, wait for zero game pods, zero server processes, and the generated settings file quiet for 30 seconds. Mute your watchers for the window. |
| 8 | **The gap** | With nothing running: move every image ref from the old tag to the new one with one compare-and-swap JSON patch (test the uid and each old value, then replace), so per-pod arguments and `director.ini` are untouched. Re-base settings if the template changed. Any settings edit happens here, never with pods up. |
| 9 | **Start** | `spec.stop=false` and poll until the battlegroup is registered. Ours takes 12 to 16 minutes. |
| 10 | **Verify** | Every pod on the new tag with zero restarts, the director ready for every partition, the settings files, table ownership, first player logins. Then the regression checklist below. |
| 11 | **Restore your schemas** | From the verified backup; check the table count and ownership. |
| 12 | **Config watch** | Last, once settings are green ([21](21-custom-settings-and-rates.md#config-watch)). |
| 13 | **Reopen** | Unmute watchers, post back-online notices. |

Read the BattleGroup CR with a jsonpath on `.spec` only. Its metadata annotation holds an old copy of the CR,
including stale `director.ini` and database passwords.

## Update-day regression checklist

A green start proves the pods are up, nothing more. Each row is a thing an update has broken or reverted for us.
State which stage you observed for each: written, parsed, in effect, or a player saw it work.

| Area | Check | Why |
|---|---|---|
| Settings file | Every key of your value set present; `DifficultyLevel=Custom` | A preset overrides every key |
| Settings template | SHA of the new template against the last one | Funcom adds and removes keys in hotfixes |
| Generated settings | Only the keys your file does not set; no preset rewritten | Normal since 1.5.3.1 |
| Engine cvars | File value **and** a live read on every pod | A file value is not a runtime value |
| `UserGame.ini` | Landsraad `DedicatedServerData` line present; PvP partition list; global PvP force `False` | 1.5.3 renamed the Landsraad line and the old name was silently ignored |
| BattleGroup CR | All image refs on the new tag; per-pod arguments still there (read from the CR) | The update tool dropped an argument on our host |
| PvP map runtime flag | Read back after the map's first boot | Lost on every restart ([22](22-amtal-pvp-sietch.md#the-runtime-pvp-flag)) |
| `director.ini` | Diff against your saved copy | Replaced with stock by the update tool |
| On-demand maps | Instance scaling enabled where you raised the spare count | Funcom shipped five 1.5.3 maps with spares at zero and no scaling gate, so raising the count alone changed nothing |
| Database migration | The migration job log ends "up to date" or "all patches applied"; read the patch list for anything touching your tables | |
| Stored procedures you patched | Your fix still in the function definition | A migration restores the stock function |
| Writers | Exercise every writer once (a grant, a reward, a Solari send) | 1.5.3 changed a currency id from integer to an enum; every grant rolled back cleanly and nothing looked wrong except missing items |
| Exchange terminals you placed | Render after a cold pod boot | |
| Timers | `LastTriggerUSec` advancing after the window, no maintenance drop-ins left | A stop and start can leave a timer active and dormant ([23](23-events-runner.md#deploying-it)) |
| Server browser | `DeclareBattlegroupUpdates` firing ([04](04-server-browser-visibility.md)) | |
| Travel | A player moves between your sietches | |

## Closing a map instance

We closed one of our three Hagga sietches after a community poll and moved every base on it to another sietch of
the same map family, at the same coordinates. The generic procedure, with the instance name, partition and target
as parameters:

**Before the window**

1. **Announce early, with what happens to bases.** Ours: every base, vehicle and container, contents included, is
   moved to the target at the same spot; overlaps are sorted out; nothing is lost. Players may move bases and items
   off on their own until the close.
2. **Tell players on the map itself.** A short in-game broadcast on each join to the closing map, rate-limited per
   player, until the close time.
3. **Scan for overlaps.** Dimensions of the same map family share coordinates, so a moved base lands at the same
   x, y, z on the target. Bucket every building piece and placeable of both partitions into 10 m cells, keep the
   height range per cell, and list pairs of bases that share cells at overlapping heights. Decide each overlap with
   the players before the window (we packed one into a Base Reconstruction Tool backup in its owner's name).
4. **Rehearse** the move on a database copy, including the revert.

**In the window**

1. **Stop the closing map at the close time.** Keep it down: it must not come back before the move is verified.
2. **Record only after the stop.** Container contents live in the map's memory until it stops, so a read with it up
   is stale, and anything captured before the close would duplicate whatever players moved out since. A pre-close
   capture is forensics only; diff it against the post-stop record and expect changes.
3. **Stop the target map too**, for the same reason, and capture what is about to be written with the Base Vault as
   your undo ([24](24-base-vault.md)).
4. **Move in one transaction.** In our schema nothing below the actor row carries a partition, so the move re-keys
   the actors' map, partition and dimension. Guards: no game server for either partition holds a database session,
   no player is online, the actor count matches the post-stop record. Write the move set into record tables inside
   the same transaction, so the revert reads exactly what moved. Characters logged out on the closing map need
   their location moved as well, or they log in to a map that no longer exists.
5. **Start the target.** Verify pieces, containers, ownership and vehicles by stage, then the jobs that fire on the
   target's boot (for a PvP map, the runtime flag).
6. **Remove the partition** from the BattleGroup CR (the world partition entry, and the ServerSet's partitions and
   replicas). Leave its PvP opt-in line, which is now inert, for the next planned update.

Since v0.8.1, [`ops/map-close/`](../ops/map-close/) implements the overlap scan, the post-stop record and the move
for any two instances of one map, from the map instance config ([20](20-player-portal.md#map-instances)):
`inventory.sh` writes the post-stop record, `map-close.sh overlap` runs the overlap scans, and `map-close.sh` runs the
move or its revert as `print`, `dry-run` (rolled back) or `apply`. It refuses a missing config file, refuses
anything beyond `print` and `overlap` while the BattleGroup runs, and `apply` and `revert` need an explicit approval
flag. The `ssh <game-host>` lines are a placeholder for your game host.

**After**

Update every surface that lists the map: the rules register, pinned posts, the website. The portal (map pickers,
chat rooms, old links) and the Discord bot's status and role pickers follow the map instance config: mark the
instance `"closed": true` and restart the API server and the bot. Packed Base Reconstruction Tool backups belong to
the player, not the map, and are not moved.

## Rollback

If an update breaks the Battlegroup, you can patch the CR's image tags back to the previous build - but only if the previous build's image tarballs are still imported into containerd. Keep at least one prior build's tarballs around until the new build is verified browser-visible and has accepted at least one player connection.

## Related documentation

- [04-server-browser-visibility.md](04-server-browser-visibility.md) - why build skew breaks visibility
- [02-canonical-config.md](02-canonical-config.md) - the canonical config reapply playbook
- [07-troubleshooting.md](07-troubleshooting.md) - "Funcom update wipes UserSettings/*.ini"
- [21-custom-settings-and-rates.md](21-custom-settings-and-rates.md) - the settings re-base and config watch
- [24-base-vault.md](24-base-vault.md) - the undo for closing a map
- [09-persistent-map-maintenance.md](09-persistent-map-maintenance.md) - empty-state checks and one operation at a time
