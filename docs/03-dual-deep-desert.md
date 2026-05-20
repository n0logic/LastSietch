# 03 - Dual Deep Desert (PvP + PvE)

Funcom's official live servers offer two flavors of Deep Desert under each World - a PvP instance with the classic high-stakes Coriolis-storm experience, and a PvE instance for pure exploration / survival without player conflict. Players pick which one to travel into via the in-game instance picker (sometimes called the "Kanly picker" because the in-game lore name for Deep Desert encounters is Kanly).

Funcom hasn't documented this for self-hosted servers, but the schema in their `world-template.yaml` supports it natively via the **dimension** field on each partition. This guide walks through enabling dual DD on a self-hosted World.

## Concept

The Funcom data model has three layers for a "map":

1. **Map** - the actual `.umap` content (the physical Deep Desert sand world)
2. **Partition** - a logical instance of a map. A map can have multiple partitions, distinguished by `id` and `dimension`. Each partition gets its own game-server pod.
3. **ServerSet** - the deployment template that produces pods for a map. A ServerSet can route requests to multiple partitions of the same map by listing partition IDs in its `partitions: []` array.

By default, the wizard generates one partition per map. Survival_1 (Hagga Basin) is partition id=1 dimension=0. DeepDesert_1 is partition id=8 dimension=0. To get dual DD, you add a second DeepDesert_1 partition with a different id and dimension=1, then expand the ServerSet to scale 2 pods (one per partition).

## Partition ID conflict (post 1.4 GA)

The default Funcom partition IDs in the GA wizard go from `1` through `30` for the 30 maps. If your guide / community / older docs reference `id: 29` for the PvE DD companion, **watch out** - that ID is now used by `CB_Overland_S_08`, a map Funcom added in 1.4. Picking 29 for your PvE DD will collide.

The next free ID is `31`. Use it for the PvE DD partition, and leave Funcom's default `CB_Overland_S_08` at id 29 alone.

If a future Funcom update introduces id 31, pick the next free ID (32, 33, etc). Run `kubectl get igwbg -o json | jq '..|.id?'` to scan in-use IDs.

## Patches

Assuming your BG namespace is `funcom-seabass-sh-<hostid>-<random>` and your BG name is `sh-<hostid>-<random>`:

```bash
NS=funcom-seabass-sh-<hostid>-<random>
BG=sh-<hostid>-<random>
```

### 1. Add the PvE companion partition to worldPartitions

The DeepDesert_1 entry is at index 7 in the default GA wizard output. Verify with:

```bash
sudo kubectl get igwbg -n $NS $BG -o json | \
  jq '.spec.database.template.spec.deployment.spec.worldPartitions | to_entries | map(select(.value.map=="DeepDesert_1"))'
```

Then patch to add the PvE companion:

```bash
sudo kubectl patch igwbg -n $NS $BG --type=json -p='[{
  "op": "add",
  "path": "/spec/database/template/spec/deployment/spec/worldPartitions/7/partitions/-",
  "value": {"dimension": 1, "disable": false, "id": 31, "maxX": 1, "maxY": 1, "minX": 0, "minY": 0}
}]'
```

### 2. Promote the DeepDesert_1 ServerSet to always-on with both partitions

```bash
sudo kubectl patch igwbg -n $NS $BG --type=json -p='[
  {"op": "replace", "path": "/spec/serverGroup/template/spec/sets/7/partitions", "value": [8, 31]},
  {"op": "replace", "path": "/spec/serverGroup/template/spec/sets/7/replicas", "value": 2},
  {"op": "replace", "path": "/spec/serverGroup/template/spec/sets/7/dedicatedScaling", "value": false}
]'
```

`dedicatedScaling: false` is critical. The Funcom default for DD is `true` (on-demand). On-demand means the operator only spawns ONE pod when a player travels - even if both partitions are defined, only one gets a pod. With `false` and `replicas: 2`, the operator maintains both pods always-on, and the player's instance pick routes them to the right one.

### 3. Mark partition 8 as PvP in UserGame.ini

In the filebrowser pod's mounted `UserSettings/UserGame.ini`, under `[/Script/DuneSandbox.PvpPveSettings]`:

```ini
m_bShouldForceEnablePvpOnAllPartitions=False
+m_PvpEnabledPartitions=8
```

Each game-server pod reads this file on startup and self-determines its PvP/PvE flavor based on its `-PartitionIndex` command-line argument (set by the operator). A pod hosting partition 8 reads `m_PvpEnabledPartitions=8` and enables PvP. A pod hosting partition 31 reads the same file but its partition isn't listed, so it stays PvE.

PvP enforcement is per-pod and local - no cross-pod coordination. The ServerState payload broadcast to FLS does NOT include `m_PvpEnabledPartitions`. The client trusts the server's local enforcement.

### 4. Verify

After applying the patches:

```bash
sudo kubectl get igwbg -n $NS $BG -o jsonpath='{.spec.database.template.spec.deployment.spec.worldPartitions[7]}'
# Expect: map=DeepDesert_1, 2 partition entries

sudo kubectl get igwbg -n $NS $BG -o jsonpath='{.spec.serverGroup.template.spec.sets[7]}'
# Expect: map=DeepDesert_1, partitions=[8,31], replicas=2, dedicatedScaling=false

sudo -u dune /home/dune/.dune/bin/battlegroup start
```

When healthy, you should see TWO DD pods in `kubectl get pods` (one ending in `pod-8`, one ending in `pod-31`).

## Player UX

In game, when a player approaches a Deep Desert travel point (e.g. an Overmap → DD transition), they see an instance picker showing both partitions under the same World, with their PvP/PvE flavor labelled. They pick one and get routed to that partition's pod.

The compass turns red while in a PvP zone within the PvP partition (rows B-I on the DD map). The Shield Wall (row A) is always safe even on PvP partitions.

## Cost

Two DD pods are heavy. With 20 GB memory limit per pod (canonical config), dual DD consumes 40 GB of always-on memory whether players are present or not. Pod load itself is light when idle - most of the limit is headroom for peak player counts (80 per partition).

A host with under 64 GB RAM probably doesn't have headroom for dual DD plus the rest of the BG. Keep DD single-partition until you upgrade hardware.

## Related Documentation

- [02-canonical-config.md](02-canonical-config.md) - full canonical config
- [05-display-name.md](05-display-name.md) - sietch display name
- [06-memory-tuning.md](06-memory-tuning.md) - per-map memory budget planning
