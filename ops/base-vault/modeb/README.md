# VAULT-04 Mode B: native backup restored for BRT placement (first live run, 2026-10-28)

Mode B of `../VAULT-04-PLAN-2026-09-29.md`: a packed base that no longer exists in the game DB
is re-inserted from its native vault capture as a parked `base_backups` entry, and the owner
places it in game with the Base Reconstruction Tool. No restart: packed actors are never
loaded by a partition, and the BRT lists backups from the DB when it is opened.

These scripts wrap the deployed `restore.py` (vault release `current`) without changing it:
they accept the native format, scope the keyless `base_backup_linked_actors` by its header,
and (live only) swap the lab-only database guard for a `dune` guard. Run as root on <web-host>.

| Script | Touches | Output |
|---|---|---|
| `preflight.py SNAPSHOT OUT.sql` | nothing (writes a READ ONLY preflight SELECT) | schema, ID collisions, dependencies, owner bindings, triggers |
| `lab.py SNAPSHOT CONTAINER` | a fresh isolated `ls-base-vault-check-*` lab only | insert, exact readback, refused re-run |
| `build_live.py SNAPSHOT OUT.sql OUT.dryrun.sql` | nothing (writes SQL) | the live transaction, `COMMIT` and `ROLLBACK` variants |
| `relocate.sql` (`-v bid= x= y= z= db=`) | one parked backup's origin, actors and pieces | all moved by one offset, refuses if any linked actor is not parked |

🔴 **A Deep Desert base needs `relocate.sql` after the restore.** Its pieces keep their DD
coordinates (about 1,000 km north of Hagga). Moving only the totem's origin is not enough: the
BRT then builds the ghost ~1,000 km away (no outline, "Not enough Support"). Relocate the origin and
the pieces together to a chosen Hagga point at ground level.

Run the SQL **inside the DB pod** (`kubectl cp` it to `/tmp`, `chmod 600`, `psql -f`, delete it).
Piping the 15 MB file through `dq.sh` stdin stalled in `kubectl exec` with nothing reaching the
DB; the in-pod run takes ~2 s.

`build_live.py` allows exactly one user trigger, pinned by name and full definition: Funcom's
`actor_fgl_entities_cleanup_orphaned_entities` (AFTER DELETE). It cannot fire on an insert-only
restore. Any other trigger still refuses.

## Run receipt: backup 112 (Suk Medic, DD event base, totem 137099)

- Source: native capture `5a009ce8-96d1-4aa2-81f2-e1d2ed2ae29e` (09-29), owner controller 3089,
  785 actors, 3,356 building pieces, 784 placeables, parked HaggaBasin p1 dim 0.
- GO: Suk Medic (owner of the base), mod-ops, 2026-10-28T00:52Z, after the dry run below.
- Preflight vs live (read only): typed schema match, 0 ID collisions in 19 tables, root absent,
  0 missing dependencies, owner bindings match, target partition exists.
- Lab: 19/19 exact readback; second run refused `vault_restore_root_present`.
- Live dry run (ROLLBACK): first refused `vault_restore_unreviewed_trigger` (the trigger above),
  then passed 19/19 exact in 2 s after the pinned allowance.
- Live commit: 2026-10-28T00:58:31Z, 2 s, `modeb_live_restore_verified` 19/19 exact.
  SQL sha256 `8422417d…` (dry run `250427be…`), reproduced byte-for-byte by `build_live.py`.
- Verify: `base_backup_get_available_backups(3089)` lists 94, 97 and
  `112 | Medic DD pre-1.5 park 2026-09-16 | 137099 | HaggaBasin`; 785/785 linked actors BaseBackup.
- Owner now at 3/3 backups.

- 01:01Z: the BRT showed only 94 and 97 after a relog, although the list function returns 112.
  Suspect: 112's stored origin was its Deep Desert point (Y 930,826, outside Hagga's bounds),
  unlike 94/97. 01:04Z (Medic GO): `totems.landclaim_original_global_location` for 137099 set
  from `[0:2]={-16990.758,930826.9,17431.965}` to `[0:2]={179377,-98644,2199}` (Medic's chosen
  Amtal site), guarded on the old value. If it still does not list, the remaining suspect is a
  server-side cache that only reloads at server start.
- After the 08:30Z maintenance restart, the BRT listed 112 but showed "Not enough Support" and no
  outline. Cause: only the origin had moved. The 785 actors and 3,356 pieces were still at their DD
  coordinates, ~1,000 km from the origin, while every native backup's totem sits within ~40 m of
  its origin. 15:37Z (Medic GO): every actor and piece shifted by the same offset
  (+196367.758, -1029470.9, -15232.965), in one transaction guarded on parked state and row
  counts. Rotation unchanged. Pre-shift snapshot on <game-host>:
  `/root/vault112-preshift-{actors,bi}-20261028T153700Z.tsv`. 15:39Z: Medic sees the outline.
  `relocate.sql` is the reusable form. In a fresh lab restore of 112 it gave the same extents as
  live, refused a wrong database and an unparked actor, and moved the base back to the DD origin.
- Game-written backups store piece transforms with either 0- or 1-based arrays (16 live backups
  are 1-based, as is 112), so offsets index from each row's `array_lower`.
- 16:07Z: **Medic deployed it with the BRT.** Read-only check: backup 112 consumed (header and
  links gone), all 785 snapshot actors `Default` on Amtal (partition 33, dimension 2), totem 137099
  live with its 5 segment rows, 3,360 building pieces on the base (3,356 restored + 4).

Proven: a 3,356-piece restored backup places through the BRT (previous BRT proof: 3 actors).
The BRT recreates only the rank-1 owner; the four member ranks must be re-added in game.
