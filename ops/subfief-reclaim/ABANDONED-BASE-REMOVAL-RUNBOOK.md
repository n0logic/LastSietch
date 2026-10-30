# Runbook: Reclaim & Remove an Abandoned Sub-Fief / Base

First used 2026-07-15 on a Hagga PvE base, reclaimed to an admin account with a single-partition
roll so it took effect. This runbook is the reusable procedure and the lessons.

Scope: your own self-hosted server. Ownership lookups and reclaims are an admin function. No em
dashes in player-facing copy.

---

## Inactive-base policy (D-2, owner decision 2026-10-28, revised 2026-10-29)

Applies to Habbanya. Published to players as rule 11 on portal `/rules`, the Discord rules pin,
lastsietch.com `/dune/` and the portal Help entry `inactive-bases`.

1. **30 days inactive:** the owner has not played for 30 days.
2. **Notice:** portal mailbox and Discord notice to the owner, stating the deadline.
3. **14-day grace.**
4. **Vault capture:** the base and its contents are captured to the owner's Base Vault BEFORE
   anything else is touched. This is the owner's only safety net; a returning owner can ask staff
   about a restore from the Vault.
5. **Timed public salvage event** on the base.
6. **Demolish** after the event.

- **No refunds** of materials or anything else.
- **No guildmate first claim.** Removed 2026-10-29: the community poll rejected it. Do not offer the
  base to guildmates ahead of the public salvage event.
- Under this policy, step 5 of the procedure below ("move the abandoned owner's items into YOUR
  storage") is replaced by the Vault capture (policy step 4) plus the public salvage event. The
  ownership transfer and single-partition roll mechanics below still apply.

---

## When to use
A player reports a base/sub-fief that is blocking them, dead (no power), or orphaned, and asks
who owns it / to remove it. Or we proactively clear a confirmed-abandoned claim.

## The data model (short version)
- `dune.totems` (id = totem ACTOR id) `landclaim_original_global_location[1/2]` = base world x/y.
- `dune.permission_actor` (actor_id, actor_name, actor_type: 3 = base/sub-fief console, 2 = vehicle).
- `dune.permission_actor_rank` (permission_actor_id = totem actor id, player_id = **player_controller_id**, rank). **rank 1 = owner**, 3 = associate. THIS is where ownership lives.
- `dune.building_instances` (owner_entity_id = totem ENTITY id, transform[1/2] = piece x/y, shelter, health, building_type). The physical pieces.
- `dune.actor_fgl_entities` (actor_id <-> entity_id) bridges totem actor id <-> building owner_entity_id.
- `dune.player_state` (player_controller_id, account_id, character_name [PLAINTEXT], server_id, online_status, last_login_time, transfer_count, previous_server_partition_id).

## Procedure

### 1. Identify the base + owner
```sql
-- totems in a bounding box around the reported area, with owner controller(s)
SELECT t.id AS totem, pa.actor_name,
       round(t.landclaim_original_global_location[1]) x, round(t.landclaim_original_global_location[2]) y,
       (SELECT string_agg(par.player_id||':r'||par.rank, ',' ORDER BY par.rank)
          FROM dune.permission_actor_rank par WHERE par.permission_actor_id=t.id) AS perms
FROM dune.totems t JOIN dune.permission_actor pa ON pa.actor_id=t.id
WHERE t.landclaim_original_global_location[1] BETWEEN <x0> AND <x1>
  AND t.landclaim_original_global_location[2] BETWEEN <y0> AND <y1>
ORDER BY x;
```
Anchor the box off the reporter's OWN bases (find their controller via `player_state.character_name`,
then their rank-1 totems). "Between my two bases" = same-latitude totem owned by someone else.
Resolve every controller id: `SELECT player_controller_id, character_name, last_login_time FROM dune.player_state WHERE player_controller_id IN (...)`.

### 2. Confirm it is actually abandoned
- `last_login_time` weeks/months old.
- `transfer_count = 0` means they did NOT move servers (they just stopped). `> 0` = transferred off.
- Check they did not just rebuild: any OTHER rank-1 totems for that controller.
- Judgment call, not just data: is this a stranger orphan or a known guildmate on a break? Surface it.

### 3. Transfer ownership (DB write, online-safe, backed up)
Adding yourself as a co-owner is NOT enough. The game treats the base as still the original
owner's and blocks you from managing/demolishing it. You must become the **SOLE rank-1 owner**.
```sh
mkdir -p /root/subfief-reclaim
# BACK UP pre-state first
printf '%s\n' "SELECT permission_actor_id||'|'||player_id||'|'||rank FROM dune.permission_actor_rank WHERE permission_actor_id=<TOTEM>;" \
  | /root/dq.sh -tA | tee /root/subfief-reclaim/perm-<TOTEM>-$(date -u +%Y%m%dT%H%M%SZ).pre.txt
# add yourself rank 1
printf '%s\n' "INSERT INTO dune.permission_actor_rank (permission_actor_id,player_id,rank) SELECT <TOTEM>,<YOUR_CTRL>,1 WHERE NOT EXISTS (SELECT 1 FROM dune.permission_actor_rank WHERE permission_actor_id=<TOTEM> AND player_id=<YOUR_CTRL>);" | /root/dq.sh
# remove the abandoned owner rank 1 (makes you SOLE owner)
printf '%s\n' "DELETE FROM dune.permission_actor_rank WHERE permission_actor_id=<TOTEM> AND player_id=<OLD_CTRL> AND rank=1;" | /root/dq.sh
```
The write is DURABLE (not RAM-clobbered), but a loaded partition keeps serving the CACHED
permission list. It only takes effect on the next **partition roll**.

### 4. Make it live: roll ONLY that partition (never the whole battlegroup)
Whole-battlegroup restart kicks EVERY player on EVERY sietch. Do not do that for one base.
Scope to the single partition/pod with `ServerRestart mode:Pods indexes:[N]`.

Partition/pod index map (survival = Hagga, deepdesert = DD). These are this server's indexes;
confirm yours from the pod names before rolling anything:
| Map | Pod | index |
|-----|-----|-------|
| Hagga PvE (dim0) | sg-survival-1-pod-1 | **1** |
| Deep Desert PvE | sg-deepdesert-1-pod-8 | 8 |
| Deep Desert PvP | sg-deepdesert-1-pod-31 | 31 |

**Verify the partition is EMPTY first (authoritatively):**
```sh
# DB: 0 online on that server_id (online_status can leave FALSE-online tombstones = over-count, so 0 is trustworthy)
printf '%s\n' "SELECT count(*) FROM dune.player_state WHERE server_id='<SERVER_ID>' AND online_status::text ILIKE '%online%';" | /root/dq.sh -tA
# Pod log: last connection + no joins in the last 90s
kubectl -n <NS> logs <POD> --since=120s | grep -E 'Join succeeded|AddClientConnection: Added client'
```
**Fire the scoped roll** (aborts if not empty):
```sh
NS=$(kubectl get ns -o name | sed -n 's#^namespace/\(funcom-seabass-.*\)#\1#p')   # the game namespace
BG=${NS#funcom-seabass-}                                                          # its battlegroup
cat <<YAML | kubectl apply -f -
apiVersion: igw.funcom.com/v1
kind: ServerRestart
metadata: { name: restart-lastsietch-hagga-pve-$(date -u +%Y%m%dt%H%M%Sz), namespace: $NS }
spec:
  battleGroup: $BG
  mode: Pods
  indexes: [1]
  reason: subfief-reclaim
YAML
```
Watch: pod Terminating -> fresh Running (age resets); log `Server farm is READY ... partition 1
... dimension 0`. Confirm OTHER pods keep their old AGE (blast radius contained).

### 5. Finish in-game (item preservation)
Do NOT DB-move items. Once your ownership is live (post-roll), log into that map, open the base's
placeable containers, move the abandoned owner's items into YOUR storage to hold, THEN demolish.
The game guarantees item integrity this way.

### 6. Reverse (if needed)
Re-add the original owner from the backup file: `INSERT ... (<TOTEM>,<OLD_CTRL>,1)` and drop your row.

---

## LESSONS / GOTCHAS (why each step is the way it is)
1. **Ownership = `permission_actor_rank` rank 1, keyed by `player_controller_id`** (stable across
   relog). Not `actors.owner_account_id` (NULL on totems).
2. **Co-owner is not enough. Must be SOLE owner** to manage/demolish. Remove the old rank-1 row.
3. **Permissions are read on partition LOAD, not per-access.** A DB write is durable but inert
   until the partition rolls. No roll = no visible change (this is the #1 "it didn't work" trap).
4. **Single-partition roll = `ServerRestart mode:Pods indexes:[N]`.** The default/BattleGroup mode
   hits every pod and kicks everyone. `indexes` is the pod-N number = the partition. (Coriolis reset
   uses the same CR with `indexes:[8,31]` for the DD pods.)
5. **Verify empty by server_id AND pod log.** DB `online_status` tombstones stay FALSE-online, so it
   only ever OVER-counts; a DB "0 online" is safe. Corroborate with the pod's last connection line.
6. **Empty-map rolls are safe** even though the standing rule is "never restart outside a window."
   The rule protects ONLINE players' RAM-only progression; with 0 players there is nothing to lose.
   Still gate hard: authoritative empty-check + owner go-ahead + scoped index only.
7. **Coordinate-space trap:** `totems.landclaim_*` / `building_instances.transform` (world coords)
   are a DIFFERENT space from `actors.transform.location`. You canNOT spatially join placeable
   container actors to a base by world position. So DB item enumeration for a base is unreliable;
   preserve items in-game instead.
8. **`player_state.server_id` changes on every pod restart** (new token). Do not key ownership or
   identity on it; use `player_controller_id` / `account_id`.
9. **Container/power model:** storage = `BP_*StorageContainer_C` / `BP_SpiceSiloContainer_C`
   (inventory_type 4); power = `BP_Generator_Placeable_C` / windtraps / turbines (inventory_type 3,
   their fuel). "No power" = no generator built or its fuel drained (runtime, not a DB flag).
10. **The pod-watcher will false-positive `[PROGRESSION-LOSS ALERT]` on any roll.** Known noise;
    mute-during-maintenance flag is still an open follow-up (7/14). Warn, do not chase.
11. **Building_type shell tell:** an all-`Atreides_Outpost_*` (or other set) piece list with no
    container/generator building pieces = a sealed decorative shell; any loot is in placeables.
