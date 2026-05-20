# 06 - Memory Tuning

Funcom's baseline for a self-hosted server is a **20 GB minimum tier**. That number is a floor, not a recommendation. This guide explains how memory is actually consumed across the Battlegroup and how to size per-map limits for hosts with more headroom.

## How memory is consumed

Each game map runs as **its own UE5 server pod** and consumes memory independently. The total memory footprint of a Battlegroup is the sum of:

- The infrastructure pods (Postgres, RabbitMQ x2, file browser, Battlegroup Director, Server Gateway, text router) - modest, roughly a few GB combined
- One game-server pod **per running partition**

The game-server pods dominate the budget. The 20 GB tier assumes only the always-on maps are running and everything else scales on demand.

## Maps that run as pods

The Funcom wizard defines a partition for every map, but only some run by default. Typical maps you will see as pods:

- **Hagga Basin** - asset name `Survival_1`. The main starting world, always-on.
- **Overmap** - the world/travel view, always-on.
- **Deep Desert** - asset name `DeepDesert_1`. The high-stakes desert tile.
- **Sietch hub maps** - social hubs such as `SH_Arrakeen` and `SH_HarkoVillage`.

### Per-map memory expectations

Observed (from the Funcom-shipped template defaults): the wizard ships these approximate per-pod memory limits.

| Map | Asset name | Approx. limit | Notes |
|---|---|---|---|
| Hagga Basin | `Survival_1` | ~12 GiB | Always-on |
| Overmap | `Overmap` | ~2 GiB | Always-on |
| Deep Desert | `DeepDesert_1` | ~15 GiB | On-demand by default |
| Sietch hubs | `SH_Arrakeen`, `SH_HarkoVillage` | ~2 GiB each | On-demand by default |
| Story / dungeon maps | various | ~2-6 GiB each | On-demand by default |

Assumption: these are pod memory **limits** (ceilings), not steady-state usage. An idle map pod uses far less than its limit; the headroom is for peak player counts (Deep Desert partitions are sized for up to 80 players). Treat the table as a budgeting tool, not a measured-usage report - actual usage varies with population and Funcom build.

## Dual Deep Desert doubles the DD cost

Running both a PvP and a PvE Deep Desert partition (see [03-dual-deep-desert.md](03-dual-deep-desert.md)) means **two always-on DD pods**. Each pod reserves its own memory limit, so dual DD roughly doubles the Deep Desert memory cost - and because both pods are kept always-on, that cost applies whether or not any players are present.

With a ~15 GiB per-pod limit, dual DD reserves roughly 30 GiB of always-on memory on its own. A host below the 64 GB range generally does not have room for dual DD on top of the rest of the Battlegroup; keep Deep Desert single-partition until you have headroom.

## Setting per-map memory limits

Per-map memory limits live in the Battlegroup CR, under the ServerSet entries:

```
spec.serverGroup.template.spec.sets[]
```

Each entry in `sets[]` corresponds to one map. To raise or lower a map's memory ceiling, edit the memory resource limit on that map's set entry:

```yaml
spec:
  serverGroup:
    template:
      spec:
        sets:
        - map: DeepDesert_1
          # raise the per-pod memory limit for stability on a larger host
          resources:
            limits:
              memory: 20Gi
```

Apply the change the same way as any other CR edit - see [02-canonical-config.md](02-canonical-config.md) for the stop / patch / start / verify order of operations. Assumption: the exact path to the memory limit field can shift between Funcom operator versions; inspect your live CR with `kubectl get igwbg -o yaml` and confirm the field location before patching.

## Always-on vs on-demand

Each map can be run in one of two modes:

| Mode | `dedicatedScaling` | Behavior |
|---|---|---|
| **Always-on** | `false` | Pod stays warm. Instant joins, but the memory limit is reserved continuously. |
| **On-demand** | `true` | Pod scales up when a player travels in, scales down when empty. Frees RAM when idle, but adds a cold-start delay (image already local, but the UE5 process and level streaming still take time). |

`dedicatedScaling: false` keeps a set always-on. The Funcom wizard defaults most maps to on-demand (`dedicatedScaling: true`) precisely to fit the 20 GB tier - only Hagga Basin and the Overmap are always-on out of the box.

The tradeoff is straightforward: always-warm maps cost constant RAM but eliminate the cold-start wait; on-demand maps free that RAM when nobody is playing the map.

## Sizing guidance by host

- **At the 20 GB tier:** keep the wizard defaults. Hagga Basin + Overmap always-on, everything else on-demand. Do not add always-on maps.
- **Above 20 GB:** you have room to keep Deep Desert always-on, and to raise per-map limits for stability headroom under peak load.
- **Comfortably above 64 GB:** dual Deep Desert plus always-on sietch hubs become feasible.
- **Memory-constrained hosts:** Funcom ships an experimental swap-memory option that lets game servers use on-disk swap, which can bring a server under the normal RAM requirement. Observed (unconfirmed): Funcom documents this as intended for hosts below the 20 GB tier; expect a performance cost versus real RAM, and treat it as a last resort rather than a substitute for adequate memory.

## Related documentation

- [02-canonical-config.md](02-canonical-config.md) - applying CR edits
- [03-dual-deep-desert.md](03-dual-deep-desert.md) - the dual-DD memory cost
- [07-troubleshooting.md](07-troubleshooting.md) - OOM-related pod restarts
