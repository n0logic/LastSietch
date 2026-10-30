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

## First live run

Proven: a 3,356-piece restored backup places through the BRT (previous BRT proof: 3 actors).
The BRT recreates only the rank-1 owner; the four member ranks must be re-added in game.
