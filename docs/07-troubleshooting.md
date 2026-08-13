# 07 - Troubleshooting

Common failures and how to recover. Built up from real deployment experience.

## BG stuck in Stopped phase, Database "Pending" forever

**Symptom:** `kubectl get igwbg` shows `PHASE=Stopped`, `DATABASE=Pending` for many minutes after `battlegroup start`. The `db-dbdepl-util` pods are in `Error` state.

**Root cause:** Funcom's schema migration script has a known bug in `setup_user_data_encryption()` - it tries to create an index on the `encrypted_player_state` table before the table itself is created. The migration fails partway through, leaving the `dune` schema in a half-initialized state. Subsequent retries fail with `duplicate key value violates unique constraint "pg_namespace_nspname_index"` because the schema already exists.

The util pods are short-lived migration jobs; once they fail twice, the operator stops retrying.

**Fix:**

```bash
NS=funcom-seabass-sh-<your-hostid>-<random>

# Get the postgres-internal password from the env of the dbdepl pod
PGPASS=$(sudo kubectl exec -n $NS sh-<bg-name>-db-dbdepl-sts-0 -- printenv POSTGRES_PASSWORD)

# Drop the partially-initialized dune database
sudo kubectl exec -n $NS sh-<bg-name>-db-dbdepl-sts-0 -- bash -c \
  "PGPASSWORD=\"$PGPASS\" psql -h localhost -p 15433 -U postgres -d postgres \
   -c \"DROP DATABASE IF EXISTS dune; CREATE DATABASE dune OWNER dune;\""

# Force-delete the Error migration pods
sudo kubectl delete pods -n $NS --field-selector status.phase=Failed --force --grace-period=0

# Stop + start to trigger fresh migration
sudo -u dune /home/dune/.dune/bin/battlegroup stop
sudo -u dune /home/dune/.dune/bin/battlegroup start
```

The fresh migration should complete successfully on the second attempt.

## Wizard interactive prompts wrong, BG region wrong

**Symptom:** You piped inputs to `world.sh` via stdin, but the BG ended up bound to the wrong region (e.g., "Europe" when you wanted "North America").

**Root cause:** The GA `world.sh` region menu has 5 options (Asia, Europe, North America, Oceania, South America). If your script picked `2`, you got Europe. PTC era had only 2 options (Europe Test, North America Test), so guides referencing "select option 2" are stale.

**Fix:** Delete the namespace and re-run the wizard.

```bash
sudo kubectl delete ns funcom-seabass-sh-<hostid>-<wrong-random>
# Update your inputs file: line 2 should be 3 for North America
# Re-run setup
sudo -u dune /home/dune/.dune/bin/setup < /tmp/dune-setup-inputs
```

Patching the region in 30+ places in the CR is technically possible but fragile. Delete + recreate is cleaner. The wizard takes ~5 minutes the second time around because the steam download is cached.

## BG Healthy but server doesn't appear in browser

**Symptom:** `kubectl get igwbg` shows `PHASE=Healthy`, `SERVERGROUP=Running`, all game pods report `Ready=true`. But the in-game Experimental tab doesn't show your server.

**Diagnostic:** Check the Director's `DeclareBattlegroupUpdates` calls.

```bash
NS=funcom-seabass-sh-<your-hostid>-<random>
BGD=$(sudo kubectl get pods -n $NS -l role=igw-battlegroup-director -o jsonpath='{.items[0].metadata.name}')

# Count empty vs populated Declares
sudo kubectl logs -n $NS $BGD 2>&1 | grep -c "DeclareBattlegroupUpdates"
sudo kubectl logs -n $NS $BGD 2>&1 | grep "DeclareBattlegroupUpdates" | grep -c 'UpDeclarationsByPartitionId":{}'

# Check most recent populated Declare
sudo kubectl logs -n $NS $BGD 2>&1 | grep "DeclareBattlegroupUpdates" | grep -v 'UpDeclarationsByPartitionId":{}' | tail -1
```

Verify the Declare payload has:
- `RegionId`: matches your selected region (e.g., `"North America"`, no "Test" suffix on GA)
- `DisplayName`: matches `Bgd.ServerDisplayName` from UserEngine.ini
- `IsStartingMap: true` on at least one partition
- `GameAddress`: your public IP (not a 10.x.x.x internal)
- `GamePort`: in the 7777-7810 range

If `UpDeclarationsByPartitionId` is empty (`{}`):

1. Are all game pods reporting `Ready=true` in `kubectl get serverstats`?
2. Is the Server Gateway logging "came up!" events? `kubectl logs <sgw-pod>`
3. Did you set `--node-external-ip` on k3s? Without it, the BGD broadcasts an internal IP.

If the Declare looks correct but the server still isn't visible:

1. Wait 2-3 minutes after Declare - FLS may have a propagation delay
2. Check the in-game Region filter dropdown - make sure it's set to the region your BG declared
3. Verify the host's public IP can be reached on UDP 7777-7810 from the internet

## "AlreadyMountedVolume" warning on pod startup

**Symptom:** `kubectl describe pod` shows:

```
Warning  AlreadyMountedVolume  ... The requested fsGroup is 65534, but the volume has GID 0. The volume may not be shareable.
```

**Status:** Benign. Pods start fine. The warning is about a PVC being mounted by multiple pods (the filebrowser pod + multiple game-server pods share the same `Saved/` PVC). The shared volume works in practice - Funcom designed for this - but k8s warns about the fsGroup mismatch.

**Fix:** Ignore.

## Game pods stuck in Terminating after `battlegroup stop`

**Symptom:** Game pods stay in `Terminating` for up to 2 minutes after stop.

**Root cause:** Each ServerSet has `terminationGracePeriodSeconds: 120` to allow players to be cleanly disconnected on shutdown. Empty servers have nothing to drain but still wait the full period.

**Fix:** If you don't have active players, force-delete:

```bash
sudo kubectl delete pods -n $NS -l app.kubernetes.io/component=server --grace-period=0 --force
```

Or just wait the 2 minutes. The operator will respawn pods on next start regardless.

## Game pods restart once on first boot

**Symptom:** All 6 (or however many always-on you configured) game pods have `RESTARTS=1` shortly after the BG starts up the first time.

**Root cause:** Game server pods come up faster than MQ admin / MQ game / BGD. The first boot of the UE5 server can't connect to dependencies and OOMs or exits. The operator restarts them and the second boot succeeds because deps are now ready.

**Fix:** Cosmetic only - restart count of 1 is normal on cold start. If pods keep restarting (RESTARTS climbing), check:
- MQ health: `kubectl get pods -n $NS | grep mq-` (should be 2/2 Running)
- BGD: `kubectl logs <bgd-pod> --tail=100`
- Game server logs: `kubectl logs <sg-pod> --tail=100` (look for connection errors)

## Multiple BG namespaces accidentally created

**Symptom:** `kubectl get ns | grep funcom-seabass` shows 2+ namespaces. The `battlegroup` CLI prompts for a selection because multiple BGs exist.

**Root cause:** A failed or interrupted setup run left an old namespace behind, and the new setup run created a new one.

**Fix:** Delete the namespaces you don't want, keeping only the canonical one.

```bash
sudo kubectl delete ns funcom-seabass-sh-<hostid>-<wrong-random>
```

The operator auto-cleans pods, PVCs, secrets - namespace deletion handles everything in one shot.

## `update-from-downloads` fails with "Invalid selection"

**Symptom:** During or after wizard, `battlegroup update-from-downloads` exits with "Invalid selection" or "resource name may not be empty".

**Root cause:** Multiple BG namespaces existed at the time the command ran, and the CLI's `select_battlegroup()` couldn't pick one non-interactively.

**Fix:** Make sure only one BG namespace exists, then re-run:

```bash
sudo kubectl get ns | grep funcom-seabass   # should show one
sudo -u dune /home/dune/.dune/bin/battlegroup update-from-downloads
```

## Empty DisplayName in Declare payload

**Symptom:** The Declare payload has `"DisplayName":""` (empty string) - FLS or the in-game UI filters the server out of browser results.

**Root cause:** Your `Bgd.ServerDisplayName` cvar isn't being read. Possible reasons:

1. You set the cvar in `UserGame.ini` instead of `UserEngine.ini`
2. You used bare `ServerDisplayName=` without the `Bgd.` prefix
3. You edited the file but didn't restart game-server pods (cvar is read on UE5 startup)
4. Your sed pattern didn't match - verify with `grep '^Bgd.ServerDisplayName' /path/to/UserEngine.ini`

See [05-display-name.md](05-display-name.md) for the full diagnostic.

## Funcom update wipes UserSettings/*.ini

**Symptom:** After running `battlegroup update`, your customizations to `UserGame.ini` or `UserEngine.ini` are gone.

**Root cause:** Funcom's image update flow can reset the deployed UserSettings to defaults (`apply-default-usersettings` runs on update).

**Fix:** Re-apply your canonical config. Use `scripts/apply-canonical.sh` for an idempotent recovery, or `scripts/audit.sh` to verify what's drifted.

The audit script also catches:
- BG title reset
- DD partition lost (back to 1-partition default)
- DD ServerSet flipped back to `dedicatedScaling: true`
- Custom memory limits reset to defaults

## `apt install steamcmd` - "Unable to locate package steamcmd"

**Symptom:** Step 2 fails with `E: Unable to locate package steamcmd`, even after editing the APT sources.

**Root cause:** `steamcmd` is a `non-free`, **i386-only** package. APT can only see it once three things are true: the `non-free` component is enabled, the `i386` architecture is added, and `apt update` has been run afterward. Missing any one produces "Unable to locate package."

**Common trap on Debian 13:** the `sed` that enables `non-free` targets the deb822 file `/etc/apt/sources.list.d/debian.sources`. Installs still using the legacy one-line `/etc/apt/sources.list` format do not have that file, so the `sed` silently changes nothing. Run `sudo apt modernize-sources` first to convert to the deb822 format, then re-run the `sed` (see Step 2 of the install guide).

**Fix:**

```bash
# 1. Confirm non-free is actually enabled (you should see contrib/non-free in a Components line)
grep -R "^Components:" /etc/apt/sources.list.d/*.sources /etc/apt/sources.list 2>/dev/null

# 2. Add the i386 architecture and refresh the package lists
sudo dpkg --add-architecture i386
sudo apt update

# 3. Confirm APT can now see the package (should show a Candidate version, not "(none)")
apt-cache policy steamcmd

# 4. Install
echo steam steam/question select "I AGREE" | sudo debconf-set-selections
echo steam steam/license note '' | sudo debconf-set-selections
sudo apt install -y steamcmd lib32gcc-s1 bc
```

If `apt-cache policy steamcmd` still reports no candidate after step 2, `non-free` is not active: recheck the `Components:` line from step 1 (it must list `non-free`), fix it, then `sudo apt update` again.

## SteamCMD download fails twice

**Symptom:** The bootstrap setup script reports "Steam download failed twice" and exits.

**Root cause:** Anonymous Steam login was rate-limited, the network was congested, or Steam's CDN was temporarily down.

**Fix:** Wait 10-15 minutes and retry:

```bash
sudo -u dune /home/dune/.dune/bin/setup < /tmp/dune-setup-inputs
```

The bootstrap detects existing downloaded files and skips already-cached content.

## Known issue: progression resets on pod restart

**Symptom:** After a game-server pod restarts (Funcom update, manual restart, OOM, host reboot), all players on that map find their character level and career skill points back at zero. Items, currency, and buildings are unaffected.

**Observed (unconfirmed):** On the current Funcom self-host build, character level, XP, and career skill points are RAM-resident in the game-server pod - they are not flushed to a persistence table. A pod restart resets them to zero for every player on that partition. Items (`dune.items`), currency (`dune.player_virtual_currency_balances`), and base buildings live in separate database tables and survive restarts normally; level/XP/skill-points do not.

**Root cause (Assumption):** Appears to be a Funcom game-code bug - an XP-event tag (`XP.Journey.Short`) is not mapped to any career tree, so the XP it represents is never committed to durable storage. This is current-build behavior and may change in a future Funcom update; re-test after each update.

**Mitigation:** There is no server-side fix while this bug exists. Minimize avoidable pod restarts: schedule updates during low-population windows, give game pods generous memory limits to avoid OOM kills (see [06-memory-tuning.md](06-memory-tuning.md)), and warn players before any planned restart.

## ImagePullBackOff on prerequisite images

**Symptom:** Pods stuck in `ImagePullBackOff` with errors like "failed to pull and unpack image".

**Root cause:** containerd can't find the image on local storage. Usually a containerd socket symlink issue or the image wasn't imported during `update-from-downloads`.

**Fix:**

```bash
# Verify the containerd socket symlink exists
sudo ls -la /run/containerd
# Should show: /run/containerd -> /run/k3s/containerd

# If missing, recreate
sudo bash -c 'cat > /etc/tmpfiles.d/k3s-containerd-symlink.conf << EOF
L /run/containerd /run/k3s/containerd
EOF'
sudo systemd-tmpfiles --create /etc/tmpfiles.d/k3s-containerd-symlink.conf

# Re-import images
sudo -u dune /home/dune/.dune/bin/battlegroup update-from-downloads
```
