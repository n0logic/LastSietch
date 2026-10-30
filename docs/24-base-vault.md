# 24 - Base Vault: base backup and restore

Players lose bases: a bad update, a map that closes, a wrong click on a permission, a break from the game. The Base
Vault is our admin tool for keeping a copy of a base outside the game and putting it back. It ships under
`ops/base-vault/` (capture, vault, lab, restore) and `admin-backend/` (the admin panel views).

It is deliberately narrow. Read the safety rules before you run any of it against a live database.

## What a capture is

A capture is a verified archive of one base's **persisted database graph**, taken read-only. It does not pack the
base, remove it from the world, use a Base Reconstruction Tool slot or change ownership. The player notices nothing.

- **Starting point.** A specific totem and its owner, not a map-wide export. The capture follows placeables,
  building pieces, actor components and recursively nested inventories.
- **One snapshot.** Every table is read inside one `REPEATABLE READ READ ONLY` transaction that is rolled back.
  Metadata is checked again inside the snapshot. Queries have time, row and size limits.
- **Exact values.** Original ids, transforms, ownership ranks, component JSON and inventory rows are kept without
  numeric re-serialisation. The capture records each array's lower bound, because `to_jsonb` drops it and the game
  writes some of these columns 0-based and some 1-based.
- **Refusal over guessing.** Unsupported incoming references, building actors shared with another base, actors on
  another map, packed actors, pending item operations, and inventories owned by the exchange or a vehicle all
  refuse the capture. This is not a universal base exporter, and should not pretend to be.

A capture is database state, **not unsaved server memory**. A running map keeps loaded containers in RAM; a database
read with the map up can be behind what players see.

### Packed bases

A base packed with the Base Reconstruction Tool lives in the backup tables, with every linked actor in a packed
state. A live capture refuses it. A **native capture** archives it as it is: the header, the links and the same table
set, with checks that exactly one header is owned by the expected player, every link is present and packed, nothing
is linked twice, and no half-packed permissions remain. It records the **parked** location, not where the base
stood.

### Archive layouts

Funcom changes the schema. Each archive records its layout and is read with that layout's table set:

| Layout | Game | Change |
|---|---|---|
| v1 | before 1.5.3 | 18 tables |
| v2 | 1.5.3 and later | 17 tables: actor state moved onto the actors table |
| v3 | 1.5.3 and later | v2 plus building blueprints, for containers holding blueprint items |
| native | 1.5.3 and later | a packed backup with its header and links |

A v1 archive is refused against a 1.5.3 database before any table is locked. Translating it is a separate, reviewed
step.

## Storage and verification

Archives live under one root-owned directory (0700, files 0600). Each capture gets a new UUID, files are created
exclusively and never overwritten, and SHA-256 covers the payload and the compressed archive. A SQLite catalog
indexes them, and an immutable manifest beside each archive holds what the catalog would need if it were lost.

Verification decompresses within a size bound and rechecks the schema shape, counts, keys, internal references,
ownership and manifest. Do not stop at "the file exists". Hashes detect corruption; they are not signatures against
a compromised root user.

Your backup job including the directory is not proof the archive reached offsite storage. Record a scoped restore
and hash check before you claim offsite recovery.

## The isolated lab

Before any restore, the archive is rebuilt in a **new, disposable** PostgreSQL container: network `none`, no
published ports, not privileged, data on a size-bounded tmpfs, limited memory and CPU. The lab recreates column
types, keys and foreign keys, inserts the archive, and compares every table for exact JSONB equality.

That proves a typed database round trip. It does not prove the game will load the base, and should never be
reported as a restore.

## Restoring

Two rules come before everything else:

- **Never insert into a loaded map.** The running map is authoritative in memory, and database writes do not update
  it. At best the write is ignored; at worst the map saves over it.
- **Every live restore is a separate, approved operation**, with a read-only preflight, a lab run, a dry run that
  rolls back, and a named player who checks the result in game.

### Hagga Basin: restore as a packed backup

The base goes back into the database as a **parked backup** owned by the player, and the player places it with the
Base Reconstruction Tool. No restart: packed actors are never loaded by a map, and the tool lists backups from the
database when it opens.

1. **Preflight**, read-only: schema match, id collisions, dependencies, owner bindings, triggers, target partition.
2. **Lab:** insert, exact readback, and a second run that must refuse because the base is already there.
3. **Build the live transaction** in two variants, `COMMIT` and `ROLLBACK`, from the same code.
4. **Dry run** the `ROLLBACK` variant against live. Any unreviewed trigger refuses. We allow exactly one Funcom
   trigger, pinned by name and full definition, that cannot fire on an insert-only restore.
5. **Commit** with the player's go, then verify the backup appears in the game's own list function for that player.

Run large restore SQL inside the database pod. Piping a 15 MB file through `kubectl exec` stdin stalled with nothing
reaching the database; the in-pod run took two seconds.

What we learned on the first live run, a 3,356-piece base:

- **A base from another map needs relocating, all of it.** A Deep Desert base restored for placement on Hagga keeps
  its Deep Desert coordinates, about 1,000 km away. Moving only the totem's origin is not enough: the tool then draws
  no outline and reports "Not enough Support". `relocate.sql` moves the origin, every actor and every building piece
  by one translation, refuses if any linked actor is not parked, and indexes each array from its own lower bound.
- **The tool listed the restored backup only after the next server restart.** We have not isolated why. Plan the
  restore before a scheduled restart.
- **The tool recreates only the owner.** Member ranks must be re-added in game.
- **Funcom fixed the backup marker in 1.5.3.4** to read the base's original location 0-based. Rows written 1-based
  before that show the marker in the wrong place; check for them after the update.

### Deep Desert: restore in place

A Deep Desert base is restored where it stood, during a scheduled restart of that one partition: the map is
unloaded, the transaction commits, and the map loads the base. `unpark-window.sh` drives it:

- `--dry-run` with the pod up runs the whole transaction and rolls back, proving every guard against live.
- `--apply --i-have-owner-approval`, in an announced window only, stops the partition's pod and waits for the
  shutdown save, commits while it is down, waits for it to come back, then proves from the server log that the actor
  load happened **after** the commit. If not, the write was not seen by this load, and the script prints the
  command for a second bounce rather than doing it on its own.

It refuses when the archive's Deep Desert layout differs from the live one: after a Coriolis reset the ground may
not be there.

## In the admin panel

Players > Base Vault, the claims view and the world maps open the same panel: take a snapshot, see a base's
history, and for packed backups the backup drawer shows the same history labelled with the backup number. Every
action is audited. Planning a restore is read-only; there is no production apply button.

## Related

- [13-safe-database-writes.md](13-safe-database-writes.md): giving is safe, taking is not
- [09-persistent-map-maintenance.md](09-persistent-map-maintenance.md): backup and recovery boundaries
- [08-updates.md](08-updates.md#closing-a-map-instance): the vault as the undo for closing a map
