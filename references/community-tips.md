# Community-contributed tips

Tricks shared by Dune: Awakening PTC discord members during the GA rollout (2026-05-15 → 2026-05-17). These are *not* n0logic-originated. Credit the original posters.

## 1. Population not showing in server list - make BGD declare it

**Source:** RedBlink, Dune PTC discord, 2026-05-17.

**Symptom:** The BGD logs `Battlegroups_DeclarePopulationAndActivity` every minute, but the `PartitionIdToPopulationAndActivityMap` only contains the spawn map's partition. The in-game browser shows a low max-capacity number that doesn't match what the BG actually hosts (e.g. 60 instead of 360 for a 6-map setup).

**Cause:** Each `[ <MapName> ]` section in `/etc/app/conf.d/director.ini` needs `ShouldUpdatePlayerCountOnFls=true`. Default behavior is to skip declaration.

**Diagnostic** (substitute your `$NS` and `$BG`):

```bash
BGD=$(sudo kubectl -n "$NS" get pod -l "app=${BG}-bgd-deploy" -o jsonpath='{.items[0].metadata.name}')

# View current settings
sudo kubectl -n "$NS" exec "$BGD" -- sh -lc \
  'grep -nE "^\[|PlayerHardCap|ShouldUpdatePlayerCountOnFls|MinServers" /etc/app/conf.d/director.ini'

# Tail recent population declarations - look for missing partitions in the map
sudo kubectl -n "$NS" logs "$BGD" --since=10m \
  | grep -E "Battlegroups_DeclarePopulationAndActivity|Error|INVALID" \
  | tail -60
```

**Fix:** For each map that should report population (i.e. each one with `replicas >= 1` and `dedicatedScaling=false` - the "always-on" set), edit the BG CR to add `ShouldUpdatePlayerCountOnFls=true` under the matching `[ <MapName> ]` section in `spec.configFiles.files["director.ini"]`.

```bash
# Back up first
sudo kubectl -n "$NS" get battlegroup "$BG" -o yaml > "/root/${BG}-before-population-fix.yaml"

# Edit the BG CR; the operator regenerates the BGD configmap automatically.
sudo kubectl -n "$NS" edit battlegroup "$BG"
#   under spec.configFiles.files."director.ini", add:
#       ShouldUpdatePlayerCountOnFls=true
#   into each active map's section.

# Restart only BGD
sudo kubectl -n "$NS" rollout restart deploy "${BG}-bgd-deploy"
sudo kubectl -n "$NS" rollout status deploy "${BG}-bgd-deploy" --timeout=180s
```

Verify the next FLS declaration includes all expected partition IDs.

## 2. Making maps persistent across BG restart

**Source:** Morcrist Meleth, Dune PTC discord, 2026-05-15.

**Symptom:** The Funcom installer's interactive `battlegroup.ini` interface, option #14, lets you bump `MinServers` for a map, but the change only sticks until the next BG restart.

**Fix:** Edit `director.ini` directly instead.

1. From the `battlegroup.ini` interface, pick option #7 `edit-battleground-advanced` and confirm.
2. Press `?`, type `director.ini`, press ENTER.
3. Find the map's section, set `MinServers = 1` (or higher).
4. Save with `:wq`.

Equivalent for k3s-native setups: set `replicas >= 1` and `dedicatedScaling: false` on the matching `spec.serverGroup.template.spec.sets[].map` entry in the BG CR. Survives restarts because it's part of the desired state.

## 3. Reduce staking unit placement times to 1 second

**Source:** Morcrist Meleth, Dune PTC discord, 2026-05-15.

**Default behavior:** Staking unit (claim flag) horizontal/vertical extensions scale from 60s to 30720s by default. Forces players to plan extensions hours in advance.

**Server-side override** in `UserGame.ini`, under `[/Script/DuneSandbox.BuildingSettings]`:

```ini
[/Script/DuneSandbox.BuildingSettings]
m_StakingUnitVerticalExtensionDefaultTimes=1
m_StakingUnitExtensionDefaultTimes=1

-m_StakingUnitExtensionDefaultTimes=60.000000
-m_StakingUnitExtensionDefaultTimes=120.000000
-m_StakingUnitExtensionDefaultTimes=240.000000
-m_StakingUnitExtensionDefaultTimes=480.000000
-m_StakingUnitExtensionDefaultTimes=960.000000
-m_StakingUnitExtensionDefaultTimes=1920.000000
-m_StakingUnitExtensionDefaultTimes=3840.000000
-m_StakingUnitExtensionDefaultTimes=7680.000000
-m_StakingUnitExtensionDefaultTimes=15360.000000
-m_StakingUnitExtensionDefaultTimes=30720.000000
-m_StakingUnitVerticalExtensionDefaultTimes=60.000000
-m_StakingUnitVerticalExtensionDefaultTimes=120.000000
-m_StakingUnitVerticalExtensionDefaultTimes=240.000000
-m_StakingUnitVerticalExtensionDefaultTimes=480.000000
-m_StakingUnitVerticalExtensionDefaultTimes=960.000000
-m_StakingUnitVerticalExtensionDefaultTimes=1920.000000
-m_StakingUnitVerticalExtensionDefaultTimes=3840.000000
-m_StakingUnitVerticalExtensionDefaultTimes=7680.000000
-m_StakingUnitVerticalExtensionDefaultTimes=15360.000000
-m_StakingUnitVerticalExtensionDefaultTimes=30720.000000
```

The leading `-` syntax is UE5's "remove this value from the inherited array" prefix - it strips the defaults inherited from `DefaultGame.ini` so only the 1-second value remains.

Server-side only - clients pick up the change automatically. Restart the affected map pods after editing.
