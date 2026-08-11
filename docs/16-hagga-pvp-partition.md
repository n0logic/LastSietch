# 16 - Adding a PvP Hagga Basin partition

Funcom's official live servers run Hagga Basin (the `Survival_1` map, the starter overland) as PvE only. But the same partition/dimension model that powers [dual Deep Desert](03-dual-deep-desert.md) lets a self-hosted World stand up a **second Hagga Basin partition in PvP mode** alongside the default PvE one. Players pick which flavor they travel into from the in-game instance picker, exactly as they do for Deep Desert.

This guide adds a PvP companion to `Survival_1`. Read [03-dual-deep-desert.md](03-dual-deep-desert.md) first: the concepts (Map / Partition / ServerSet, the `dimension` field, `dedicatedScaling`) are identical here and are explained there in full.

## Why this is different from Deep Desert

Deep Desert is an on-demand map (`dedicatedScaling: true`): the operator only spawns a pod when a player travels in. Hagga Basin is a **base map** - it hosts persistent player bases, so its ServerSet is always-on. That changes two things:

1. You are adding a second **always-on** pod, not an on-demand one. The memory cost is paid 24/7 (see [Cost](#cost)).
2. Player bases live in a partition's world. A base built on the PvE partition is not reachable from the PvP partition and vice versa - they are separate worlds that happen to share the same `.umap`. Decide the split before players build, because there is no supported way to move a base between partitions.

## Concept: PvP is opt-in per partition

A pod reads `[/Script/DuneSandbox.PvpPveSettings]` from `UserSettings/UserGame.ini` on startup and self-determines its flavor from its own `-PartitionIndex`:

- `m_bShouldForceEnablePvpOnAllPartitions=False` keeps the default (PvE) for every partition NOT explicitly listed.
- `+m_PvpEnabledPartitions=<id>` opts a single partition into PvP.

So the PvE Hagga (the default `id: 1`) and the new PvP Hagga (`id: 32` below) read the same file; only the listed one turns hostile. There is no cross-pod coordination and the PvP flag is not part of the ServerState payload sent to FLS - each pod enforces locally and the client trusts it.

## Partition ID selection

The GA wizard assigns `id: 1` through `id: 30` for the 30 default maps, and 1.4 added maps above that. Pick the next free ID and confirm nothing else uses it:

```bash
sudo kubectl get igwbg -o json | jq '..|.id?' | sort -n | uniq
```

This guide uses `id: 32` for the PvP Hagga companion. If that is taken on your World, pick the next free one and substitute it everywhere below.

## Patches

Set your namespace and battlegroup name (see [03](03-dual-deep-desert.md#patches) for where these come from):

```bash
NS=funcom-seabass-sh-<hostid>-<random>
BG=sh-<hostid>-<random>
```

Find the `Survival_1` index in both the partition list and the ServerSet list (do not assume it is 0 - verify):

```bash
sudo kubectl get igwbg -n $NS $BG -o json | \
  jq '.spec.database.template.spec.deployment.spec.worldPartitions
      | to_entries | map(select(.value.map=="Survival_1")) | .[].key'
```

Call that index `$P` in the commands below.

### 1. Add the PvP companion partition

```bash
sudo kubectl patch igwbg -n $NS $BG --type=json -p='[{
  "op": "add",
  "path": "/spec/database/template/spec/deployment/spec/worldPartitions/'"$P"'/partitions/-",
  "value": {"dimension": 1, "disable": false, "id": 32, "maxX": 1, "maxY": 1, "minX": 0, "minY": 0}
}]'
```

`dimension: 1` is what distinguishes the PvP instance from the default PvE `dimension: 0`. It is also what the instance picker uses to label the two.

### 2. Scale the Survival_1 ServerSet to both partitions

```bash
sudo kubectl patch igwbg -n $NS $BG --type=json -p='[
  {"op": "replace", "path": "/spec/serverGroup/template/spec/sets/'"$P"'/partitions", "value": [1, 32]},
  {"op": "replace", "path": "/spec/serverGroup/template/spec/sets/'"$P"'/replicas", "value": 2}
]'
```

Hagga is already always-on, so `dedicatedScaling` stays `false` (do not flip it). `replicas: 2` gives one pod per partition.

### 3. Mark partition 32 as PvP in UserGame.ini

In the filebrowser pod's mounted `UserSettings/UserGame.ini`, under `[/Script/DuneSandbox.PvpPveSettings]`:

```ini
m_bShouldForceEnablePvpOnAllPartitions=False
+m_PvpEnabledPartitions=32
```

If you already run a PvP Deep Desert, add the Hagga id to the existing block rather than replacing it - the key is additive (`+m_PvpEnabledPartitions`), one line per PvP partition:

```ini
+m_PvpEnabledPartitions=8
+m_PvpEnabledPartitions=32
```

The `ops/pvp-partition-pin/` tooling in this repo automates this edit idempotently (it pins the whole `[PvpPveSettings]` block, including the anti-spawncamp respawn-cooldown key, so a Funcom update that ships a fresh `UserGame.ini` does not silently revert your PvP setup). Run it against your filebrowser mount rather than hand-editing if you want the edit to survive updates.

### 4. Restart and verify

```bash
sudo kubectl get igwbg -n $NS $BG -o jsonpath='{.spec.database.template.spec.deployment.spec.worldPartitions['"$P"']}'
# Expect: map=Survival_1, two partition entries (id 1 dim 0, id 32 dim 1)

sudo -u dune /home/dune/.dune/bin/battlegroup start
```

When healthy, `kubectl get pods` shows two Survival_1 pods, one ending `-pod-1` (PvE) and one ending `-pod-32` (PvP). Travel into each from the in-game picker and confirm the compass turns red only on the PvP instance.

## Safe tradeposts on a PvP partition

Enabling PvP on the partition makes the **whole world** hostile except where the game's built-in security zones apply. Funcom's tradeposts (the NPC exchange hubs) carry their own security zone, so they stay safe automatically on a PvP partition - you do not configure that, and you should not need to.

What has **no supported server-side mechanism** is a safe *player* base on a PvP partition: if the partition is PvP, player structures on it are attackable. If you want safe player bases, keep them on the PvE partition. Treat "full-PvP world with safe player bases" as unsupported until Funcom ships a per-structure override; the `SecurityZones.UsePvPOverrideTable` cvar exists but is unverified and is not part of this guide.

## Cost

A second always-on Hagga pod costs the same as the first: with the canonical 20 GB per-pod limit, budget another 20 GB of always-on memory. Combined with a dual Deep Desert (another 40 GB), a full PvP + PvE split across both base maps needs headroom most sub-64 GB hosts do not have. Size with [06-memory-tuning.md](06-memory-tuning.md) before committing.

## Related Documentation

- [03-dual-deep-desert.md](03-dual-deep-desert.md) - the same technique for Deep Desert, with fuller concept notes
- [02-canonical-config.md](02-canonical-config.md) - full canonical config
- [06-memory-tuning.md](06-memory-tuning.md) - per-map memory budget planning
