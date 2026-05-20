# 02 - Canonical Config

After Funcom's wizard generates a default Battlegroup, you'll get a working but vanilla configuration. This guide layers on the customizations that make a Dune self-host server feel like a real community server:

- A real sietch name in the server browser (instead of the procedural "Sietch Abbir" / "Sietch Hagal" / etc.)
- A short, recognizable Battlegroup title
- Tuned per-map memory and scaling for hosts above the 20 GB minimum tier
- PvP/PvE knobs that match Funcom's official server settings
- Sandstorm and security zone configuration
- Building limit and item deterioration tuning

## What the wizard gives you

After running the bootstrap, the wizard produces:

- A `BattleGroup` Custom Resource (CR) in namespace `funcom-seabass-sh-<your-hostid>-<random>`
- A `server-gateway-secret` (your JWT)
- A `rmq-game-secret` (RabbitMQ auth)
- A set of pods for: postgres database, RabbitMQ, file browser, battlegroup director, server gateway, text router, and one game-server pod per starting partition

The default config has:

- **All non-essential maps set to on-demand scaling** (`dedicatedScaling: true`). They start cold when a player requests them and idle out otherwise.
- **Modest per-map memory limits** sized for a 20 GB host.
- **Default building limits and game settings** matching Funcom's official PvP-enabled servers.
- **No custom server name** - your sietch shows up with a procedural placeholder.

## Two layers of configuration

There are two places to make changes:

| Layer | Where | Scope | When applied |
|---|---|---|---|
| **Per-server CR settings** | `BattleGroup` CR (`kubectl edit igwbg ...`) | Partition / pod count / memory / scaling / args | Operator reconciles on the next status check |
| **Game runtime configuration** | `UserGame.ini` and `UserEngine.ini` in the filebrowser PVC | Building limits, PvP partitions, console variables, mining multipliers | Each game-server pod reads on startup; restart needed |

The wizard's `apply-default-usersettings` step writes default versions of `UserGame.ini` and `UserEngine.ini` into the filebrowser pod's mounted `Saved/UserSettings/` directory. After the wizard exits, you can edit those files in place and restart the partition pods to pick up your changes.

## Canonical settings

### 1. Battlegroup title

The title is what shows in some Funcom-internal interfaces and may surface in the browser. Set it short:

```bash
NS=funcom-seabass-sh-<your-hostid>-<random>
BG=sh-<your-hostid>-<random>

sudo kubectl -n $NS patch igwbg $BG \
  --type=json \
  -p='[{"op": "add", "path": "/spec/title", "value": "Your Server Name"}]'
```

### 2. Sietch in-game display name

See [05-display-name.md](05-display-name.md) for the deep dive. Short version:

```ini
# UserEngine.ini → [ConsoleVariables]
Bgd.ServerDisplayName="Your Sietch Name"
```

The `Bgd.` prefix and the `UserEngine.ini` file (NOT `UserGame.ini` or `director.ini`) are mandatory.

### 3. PvP partition selection

By default Funcom's official PvE servers have all partitions in PvE mode and the player chooses a PvP instance via the in-game menu. For a self-host server, you control the PvP/PvE mapping per partition via:

```ini
# UserGame.ini → [/Script/DuneSandbox.PvpPveSettings]
m_bShouldForceEnablePvpOnAllPartitions=False
+m_PvpEnabledPartitions=8    ; partition 8 is the PvP instance
```

The default Funcom map ID 8 is `DeepDesert_1`. Listing it here makes that partition the PvP flavor; any partition NOT listed defaults to PvE. See [03-dual-deep-desert.md](03-dual-deep-desert.md) for standing up a second DD partition in PvE.

### 4. Always-on maps

Funcom's wizard defaults all maps except `Survival_1` (Hagga Basin) and `Overmap` to **on-demand** scaling - they boot when needed and shut down when empty. This minimizes idle memory on small boxes. On a host with comfortable RAM, you may want certain maps always-running to eliminate cold-start delays:

```yaml
# patch on each ServerSet you want always-on
spec:
  serverGroup:
    template:
      spec:
        sets:
        - map: DeepDesert_1
          dedicatedScaling: false   # always-on
          replicas: 1               # or 2 for dual-flavor - see 03-dual-deep-desert.md
        - map: SH_Arrakeen
          dedicatedScaling: false   # optional always-on
        - map: SH_HarkoVillage
          dedicatedScaling: false   # optional always-on
```

The trade-off: each always-on map reserves its memory limit. With a 20 GB host you're already tight just with `Survival_1` + `Overmap`. With 64+ GB you have room for `DeepDesert_1` always-on, and 128+ GB lets you add the social hubs too. See [06-memory-tuning.md](06-memory-tuning.md) for the per-map memory targets we've validated.

### 5. Security zones, sandstorms, building limits

These all live in `UserGame.ini`. The wizard's default has reasonable values; here are the keys you're most likely to want to tune:

```ini
[/Script/DuneSandbox.SecurityZonesSubsystem]
m_bAreSecurityZonesEnabled=True    ; False = free-for-all PvP everywhere

[/Script/DuneSandbox.SandStormConfig]
m_bCoriolisAutoSpawnEnabled=True

[/DeteriorationSystem.ItemDeteriorationConstants]
UpdateRateInSeconds=1.0             ; lower = faster decay, 0 = off

[/Script/DuneSandbox.BuildingSettings]
m_MaxNumLandclaimSegments=6
m_BuildingBlueprintMaxExtensions=4
m_BaseBackupMaxExtensions=8
m_bBuildingRestrictionLimitsEnabled=True
```

### 6. Mining multipliers and economy

These live in `UserEngine.ini` under `[ConsoleVariables]`:

```ini
[ConsoleVariables]
Dune.GlobalMiningOutputMultiplier=1.0
Dune.GlobalVehicleMiningOutputMultiplier=1.0
SecurityZones.PvpResourceMultiplier=2.5      ; PvP zones yield 2.5x by default
dw.VehicleDurabilityDamageMultiplier=1.0
```

### 7. Sandworm behavior

```ini
[ConsoleVariables]
Sandstorm.Enabled=1
Sandstorm.Treasure.Enabled=1
sandworm.dune.Enabled=1
Vehicle.SandwormCollisionInteraction=false
Sandworm.SandwormDangerZonesEnabled=true
Vehicle.SandwormInvulnerabilitySecondsOnExit=900.0
Vehicle.SandwormInvulnerabilitySecondsOnServerRestart=7200.0
```

## Order of operations

When you're applying canonical config to a fresh wizard-generated BG:

1. **Stop** the BG (`/home/dune/.dune/bin/battlegroup stop`) - apply changes against a stopped state to avoid pods restarting mid-edit
2. **Patch the BG CR** for any structural changes (worldPartitions, ServerSets, memory limits, title)
3. **Edit `UserGame.ini` and `UserEngine.ini`** in the filebrowser pod's mounted volume (these survive across pod restarts because they live on a PVC, not in the container)
4. **Start** the BG (`/home/dune/.dune/bin/battlegroup start`)
5. **Verify** server appears in the in-game Experimental tab within ~2 minutes of `Healthy` phase

## Audit / drift detection

Funcom updates can occasionally reset `UserSettings/*.ini` to defaults (when the image is updated mid-game). Run an audit script after each Funcom update to verify your canonical settings survived. See [scripts/audit.sh](../scripts/audit.sh) for an example.

## Reapply playbook

If your customizations get reset by a Funcom image update:

```bash
# Stop BG
sudo -u dune /home/dune/.dune/bin/battlegroup stop

# Re-apply UserGame.ini PvP partition
sudo kubectl -n $NS exec deploy/$(...filebrowser pod...) -- sh -c "
  PVC=/path/to/saved/UserSettings
  sed -i '/^;+m_PvpEnabledPartitions=1\$/a +m_PvpEnabledPartitions=8' \$PVC/UserGame.ini
"

# Re-apply UserEngine.ini sietch display name
sudo kubectl -n $NS exec deploy/$(...filebrowser pod...) -- sh -c "
  PVC=/path/to/saved/UserSettings
  sed -i '/^\[ConsoleVariables\]/a Bgd.ServerDisplayName=\"Your Sietch Name\"' \$PVC/UserEngine.ini
"

# Re-apply CR patches
sudo kubectl -n $NS patch igwbg $BG --type=json -p='[ ... your patches ... ]'

# Start
sudo -u dune /home/dune/.dune/bin/battlegroup start
```

Or maintain a `scripts/apply-canonical.sh` that runs all the patches idempotently. See [scripts/apply-canonical.sh](../scripts/apply-canonical.sh) for an example.
