# 16 - PvP maps: a second Hagga Basin partition

Funcom's official servers run Hagga Basin (the `Survival_1` map family) as PvE. A self-hosted World can add more
Hagga partitions next to the default one and opt any of them into PvP. Players pick the partition from the in-game
sietch picker, exactly as they pick a Deep Desert instance.

This doc covers the mechanics: adding the partition, opting it into PvP, and what the opt-in does and does not
cover. [22-amtal-pvp-sietch.md](22-amtal-pvp-sietch.md) covers running such a map day to day as a full PvP sietch
with safe settlements. Read [03-dual-deep-desert.md](03-dual-deep-desert.md) first: the concepts (map family,
partition, ServerSet, the `dimension` field, `dedicatedScaling`) are the same and are explained there in full.

## How we run it today

| Sietch | Map family | Partition (ours) | Ruleset |
|---|---|---|---|
| Habbanya | `Survival_1` | 1, dimension 0 | PvE |
| Amtal | `Survival_1` | 33, dimension 2 | Full PvP, safe only inside two faction settlements (Riftwatch and Helius Gate) |
| Deep Desert PvE | `DeepDesert_1` | 8 | PvE |
| Deep Desert PvP | `DeepDesert_1` | 31 | PvP |

Partition numbers are ours. Yours will differ: always discover the live mapping (see
[09-persistent-map-maintenance.md](09-persistent-map-maintenance.md)) instead of copying ours.

Until October 2026 we also ran a third Hagga sietch, Kulon, on the zone-gated model described under
[History](#history-the-zone-gated-model). After a community poll we closed it and moved its bases into Amtal. The
generic procedure is in [08-updates.md](08-updates.md#closing-a-map-instance).

## Build history

What changed for PvP operators, by the server image tags we ran.

| Build | When | What we saw |
|---|---|---|
| `2051294`, `2064155` | July to September 2026 (before 1.5) | Per-partition PvP opt-in in `UserGame.ini` (`+m_PvpEnabledPartitions`). With stock security zones it arms PvP only in the map's NullSec areas (zone-gated). |
| `2111270` | 1.5.3, September 2026 | The in-game Custom Settings framework ([21](21-custom-settings-and-rates.md)), which carries the battlegroup-wide `PVPMode` and `PVPDamageStructures` keys and lets a per-pod `-ini:ServerCustomSettings:` argument override a key for one partition. PvP shield damage reduced by a third (Funcom, global). The shipped `UserEngine.ini` template sets `SecurityZones.PvpResourceMultiplier=2.5`. |
| 1.5.3.x hotfixes | late September 2026 | No PvP change from Funcom. We moved Amtal from zone-gated to full PvP with a per-pod security-zone table and a runtime flag: a configuration change, not a build change. |
| `2168446` | 1.5.15.3, October 2026 | The build this snapshot is pinned to. No PvP-specific change observed. Kulon was closed in this build's window. |

## Concept: PvP is opt-in per partition

A pod reads `[/Script/DuneSandbox.PvpPveSettings]` from `UserSettings/UserGame.ini` at startup and decides its
flavour from its own partition index:

- `m_bShouldForceEnablePvpOnAllPartitions=False` keeps every partition that is not listed PvE. `True` would drag
  your PvE Hagga into PvP as well.
- `+m_PvpEnabledPartitions=<id>` opts one partition into PvP. The key is additive: one line per PvP partition.

Every pod reads the same file; only the listed partitions enable PvP. There is no cross-pod coordination.

## Adding the partition

### Partition id

Pick the next free id and confirm nothing else uses it:

```bash
sudo kubectl get igwbg -o json | jq '..|.id?' | sort -n | uniq
```

The examples below use `33`. Substitute yours everywhere.

### Find the map family's indexes

```bash
NS=funcom-seabass-sh-<hostid>-<random>
BG=sh-<hostid>-<random>
sudo kubectl get igwbg -n $NS $BG -o json | \
  jq '.spec.database.template.spec.deployment.spec.worldPartitions
      | to_entries | map(select(.value.map=="Survival_1")) | .[].key'
sudo kubectl get igwbg -n $NS $BG -o json | \
  jq '.spec.serverGroup.template.spec.sets
      | to_entries | map(select(.value.map=="Survival_1")) | .[].key'
```

Call the first `$P` and the second `$S`. Do not assume either is 0, or that the two lists share an order.

### 1. Add the partition

```bash
sudo kubectl patch igwbg -n $NS $BG --type=json -p='[{
  "op": "add",
  "path": "/spec/database/template/spec/deployment/spec/worldPartitions/'"$P"'/partitions/-",
  "value": {"dimension": 2, "disable": false, "id": 33, "maxX": 1, "maxY": 1, "minX": 0, "minY": 0}
}]'
```

Each Hagga partition needs its own `dimension`. Dimensions of the same map family share coordinates: a base at a
given spot on one dimension sits at the same x, y, z on another. That matters when you close a map and move its
bases ([08](08-updates.md#closing-a-map-instance)).

### 2. Scale the ServerSet to cover it

```bash
sudo kubectl patch igwbg -n $NS $BG --type=json -p='[
  {"op": "replace", "path": "/spec/serverGroup/template/spec/sets/'"$S"'/partitions", "value": [1, 33]},
  {"op": "replace", "path": "/spec/serverGroup/template/spec/sets/'"$S"'/replicas", "value": 2}
]'
```

Hagga is always on, so leave `dedicatedScaling` at `false`.

### 3. Opt it into PvP

In the PVC's `UserSettings/UserGame.ini`:

```ini
[/Script/DuneSandbox.PvpPveSettings]
m_bShouldForceEnablePvpOnAllPartitions=False
+m_PvpEnabledPartitions=31
+m_PvpEnabledPartitions=33
```

`ops/pvp-partition-pin/` pins this block idempotently, so an update that ships a fresh `UserGame.ini` does not
silently drop your PvP partitions. When you close a PvP map, its line becomes inert; remove it at the next planned
update rather than in a hurry.

### 4. Start and verify

```bash
sudo kubectl get igwbg -n $NS $BG -o jsonpath='{.spec.database.template.spec.deployment.spec.worldPartitions['"$P"']}'
sudo -u dune /home/dune/.dune/bin/battlegroup start
```

`kubectl get pods` shows one `Survival_1` pod per partition, ending `-pod-1` and `-pod-33`. Verify PvP where the
ruleset says it applies, not in the open desert (see the next section).

## What the opt-in covers on its own

The opt-in alone, with stock security zones, gives zone-gated PvP (observed live in August 2026):

| Area | Zone type | Behaviour on an opted-in partition |
|---|---|---|
| Shipwreck and similar contested areas | NullSec | PvP |
| Tradeposts, sietches, social hubs | Town, SocialHub | Safe |
| Open desert and everything else | Security | PvE |

To make the open desert hostile too, you need Funcom's PvP security-zone table. [22](22-amtal-pvp-sietch.md) covers
how we apply it to one partition only, and what that changed for safe areas.

Two traps on the table route:

1. **A bad asset path crash-loops the server.** Pointing the data-table setting at a missing or misspelled asset
   hard-faults the server process at start. Copy the path exactly and change one thing at a time.
2. **The PvP table lacks some stock zone rows.** Areas that rely on those rows can change behaviour, and the zone
   banner the client draws may not match what the server enforces. Walk every area you plan to call safe before you
   publish it as safe.

## Bases on a PvP partition

Bases live in the world of the partition they were built on. A base on the PvE partition is not reachable from the
PvP one. Decide the split before players build: the only in-game way to move a base between partitions is to pack
it with the Base Reconstruction Tool and place it again. [24-base-vault.md](24-base-vault.md) covers admin capture
and restore.

The Custom Settings key `PVPDamageStructures` is battlegroup-wide, and we have not tested a per-map value. Test
what your structures take on a throwaway partition before you write any rule about it.

## Cost

Every always-on Hagga partition costs another always-on pod. With the canonical 20 GB per-pod limit, budget another
20 GB of memory. Size with [06-memory-tuning.md](06-memory-tuning.md) before you commit.

## History: the zone-gated model

Earlier versions of this doc described a second Hagga partition as zone-gated PvP with safe tradeposts. That is
still what the opt-in gives you with stock zones, and it is how Kulon ran until it closed. If zone-gated PvP is
what you want, the mechanics above are all you need.

## Related

- [03-dual-deep-desert.md](03-dual-deep-desert.md): the same technique for Deep Desert
- [22-amtal-pvp-sietch.md](22-amtal-pvp-sietch.md): running a full PvP sietch
- [21-custom-settings-and-rates.md](21-custom-settings-and-rates.md): per-map settings and rates
- [08-updates.md](08-updates.md): update day, and closing a map instance
