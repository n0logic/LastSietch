# Base snapshot vault

Admin-only, non-destructive archives of a placed base's persisted database graph.
The vault is independent of the live Dune database and map lifecycle. It does not
pack a base, remove it from the world, reserve a BRT slot or change ownership.

For the operator walkthrough see `docs/24-base-vault.md`.

## Safety boundaries

- The exporter uses explicit `REPEATABLE READ READ ONLY` transactions and rolls
  them back. It never calls BRT pack-up or other game mutation functions.
- Each final payload is selected within one database snapshot. Metadata is read
  beforehand and checked again inside that snapshot. Queries have time, row and
  size limits. No game process is stopped or asked to flush.
- This captures **persisted database state, not unsaved server RAM**. Database
  consistency does not prove the engine has flushed every related save operation.
- Original IDs, transforms, ownership ranks, actor fields, component JSON and
  selected inventory rows are retained without numeric reserialization.
- The graph starts from a specific totem and its owner entity, not a map-wide
  export or an obsolete list of actor IDs. It follows placeables, building pieces,
  actor components and recursively nested inventories.
- Unsupported incoming references, shared building actors, cross-map actors,
  packed actors, pending item operations and exchange/vehicle inventory parents
  cause refusal. This is intentionally not a universal base or vehicle exporter.
- The pending-operation guard checks rows visible in the same database snapshot.
  It cannot prevent later writes or identify work still only in game RAM.
- Owner and permission-holder identity mappings are protected inside the archive.
  These are not complete player-account backups and must never enter GitHub.
- There is no production restore command, timer, automatic retention pruning,
  player-accessible route or delete command in this implementation.
- Never insert a snapshot into a loaded map. The running map is authoritative in
  RAM and database writes do not update it. A future restore needs separate
  approval, collision handling, a controlled lifecycle and in-game verification.

## Files and storage

`export-base.py` executes on `<game-host>` as an operator. `vault.py` and
`lab-check.py` execute on `<web-host>`, with the sibling exporter available for
shared constants. Use a versioned code directory under `/opt/lastsietch-base-vault/`.

The fixed archive root is `/opt/backups/dune/base-vault`, with root-owned 0700
directories and 0600 files:

```text
base-vault/
  catalog.sqlite
  catalog.consistent.sqlite
  vault.lock
  snapshots/<uuid>.json.gz
  snapshots/<uuid>.manifest.json
  validations/<uuid>.json
```

Each capture gets a new UUID. Payloads and manifests are exclusive-create files;
existing archives are never overwritten. SHA-256 covers the original payload and
compressed archive. Verification decompresses within a size bound and rechecks
the schema shape, counts, keys, internal references, ownership and manifest.
Hashes detect corruption; they are not signatures against a compromised root user.

SQLite indexes snapshots and validations. Writes use an exclusive vault lock,
FULL synchronous commits and a consistent backup API copy. Immutable manifests
retain the essential index data if the catalog is lost. There is no automated
catalog rebuild command yet. `list` returns the newest 1,000 entries.

The existing `/opt/backup/vps-backup.sh` includes `/opt/backups/` in its offsite
backup scope. That configuration alone is **not proof** that a new snapshot has
reached offsite storage. Record a successful scoped backup and restore/hash check
before claiming offsite recovery. Do not run a whole backup/prune job just to test
this vault. Never prune Medic's existing protected archive.

## Operator workflow after deployment approval and coordination

1. Read the latest resume and receipt. Verify the exact totem, owner and current
   map through read-only queries. Announce the deployment in private mod-ops.
2. Install the exact reviewed code into a new, root-owned versioned directory.
   Verify deployed hashes. Do not overwrite an existing release or old evidence.
3. Capture on `<game-host>` into a new root-only file with a restrictive umask:
   `python3 export-base.py --totem 137099 --expected-owner 3089`.
   Direct stdout to that protected file using exclusive creation. Never display
   it in a terminal transcript or route it through Git.
4. Transfer the file over the existing authenticated SSH path to protected
   staging on `<web-host>`. Verify source and destination SHA-256 and 0600 mode.
5. Ingest with `python3 vault.py ingest --input /absolute/protected/capture.json
   --label 'Medic DD current-state snapshot'`. Keep the returned UUID and hashes.
6. Verify with `python3 vault.py verify --snapshot UUID`. Do not stop at a catalog
   row or a file-exists check.
7. Run the isolated reconstruction below and record its receipt. Preserve the
   original protected full-database backup independently.

## Isolated reconstruction, not production restore

Create a **new** disposable PostgreSQL container, not the existing Medic recovery
clone. The cached approved image ID on <web-host> is:

```text
sha256:979c4379dd698aba0b890599a6104e082035f98ef31d9b9291ec22f2b13059ca
```

Use a name beginning `ls-base-vault-check-`, label
`lastsietch.role=base-vault-lab`, `--network none`, no published ports, no
privileged mode, at most 1 GiB memory and 1 CPU. Mount PGDATA using a size-bounded
tmpfs, with no bind mounts or named data volumes. Use database `vaultcheck`.
Local trust authentication is acceptable only inside this new, network-isolated
lab; never change production authentication. Do not pull or replace game images.

Run `python3 lab-check.py --snapshot UUID --container ls-base-vault-check-NAME`.
The helper verifies container identity, isolation and image before using its
immutable container ID. It creates schema `dune`, so an already populated lab
fails instead of being cleared.

The lab recreates captured column types, nullability, primary/unique keys,
reviewed checks and foreign keys. Minimal external identity/partition stubs are
explicitly counted. Foreign keys are deferred only during the lab transaction,
then forced before comparing all 18 captured tables for exact JSONB equality.
The original payload bytes reach PostgreSQL without floating-point conversion.

This proves a typed database graph round-trip, not in-game spawning, gameplay,
all game database functions/triggers, full account recovery or cross-version
compatibility. Do not label the test as a full Dune restore. Failed SQL retains
bounded stderr in the root-only validations directory, never in GitHub.

## Tests

```bash
python3 scripts/tests/test_base_vault.py
python3 scripts/tests/test_ci_suites.py
```

The new suite is registered in CI. Hosted CI is currently subject to the account's
billing/spending block; local test results must not be called hosted CI green.

## Admin snapshot capture and history

The admin integration adds a Base Vault directory and a shared panel reached
from placed bases and the admin maps. A capture archives persisted state without
packing, adopting, deleting or restarting the base. The production restore
control remains unavailable.

The API is admin-only: `GET /api/dune/v2/claims/{totem_id}/vault` returns at most
50 snapshots, newest first, with the total count, capture timestamp, table
counts, size, checksums and separately dated verification and lab results.
Archive contents and identity mappings are never returned. Missing or unavailable
live bases retain their history but cannot be captured from the panel.

`POST /api/dune/v2/claims/{totem_id}/vault/snapshots` requires the existing admin
CSRF token and exactly `{"request_id":"canonical-uuid"}`. It records an audit
before requesting work. The same request ID returns its recorded operation;
changing its base or initiating admin is refused. The API cannot specify a path,
command, owner, archive label or restore action.

The root helper starts a separate systemd capture instance. Closing a browser or
restarting the portal does not cancel it. A root-only SQLite operation journal
records pending, running, completed, failed or uncertain state. Only one capture
can be active across the vault. Workers are bounded to 300 seconds, source SSH
to 245 seconds, payload output to the existing 72 MiB ceiling and memory to
768 MiB. Existing exporter SQL time, row and read-only transaction limits remain.

The remote wrapper resolves the live rank-one owner and the existing exporter
checks that owner and metadata again inside the final repeatable-read snapshot.
Displayed base metadata is cached for at most 30 seconds; capture does not trust
that cache. A saved-state timestamp does not include unsaved server RAM.

Verification status means the actual archive passed validation at its displayed
check time. A previously unverified historical entry is checked on demand, at
most one per history request. An isolated database round-trip is shown separately
and never described as an in-game restore.

### Deploy the admin integration

Announce in private mod-ops and verify the current executed paths first. Install
the reviewed source into a new root-owned 0700 versioned directory on both
hosts, preserving existing `/opt/lastsietch-base-vault/27828e4e` and all archives.
The directory must contain `vault.py`, `export-base.py`, `admin-helper.py` and
`admin-export.py`, root-owned 0600. Retain `lab-check.py` for operator validation.
Atomically point `/opt/lastsietch-base-vault/current` to that reviewed release.

1. On <game-host>, install the read-only wrapper and exporter first. Verify the
   wrapper through its real interpreter using a `describe` request for a known
   placed base. Do not print an exported archive or owner identity mappings.
2. On <web-host>, verify root's existing `lastsietch-dune` SSH alias and fixed remote
   wrapper path. It uses the existing authenticated operator transport; no SSH
   key or secret is copied into the portal service. The helper never accepts a
   remote host or command from a caller.
3. Install `lastsietch-base-vault-capture@.service` root-owned 0644 in
   `/etc/systemd/system/`. Run `systemd-analyze verify` and `systemctl daemon-reload`.
   No capture starts from installing this instance template.
4. Install `lastsietch-base-vault.sudoers` root-owned 0440 as
   `/etc/sudoers.d/lastsietch-admin-base-vault`, after `visudo -cf` validates it. This
   permits only the exact isolated Python helper command with no extra arguments.
   Test allowed history and refused alternate commands as the real `lastsietch-admin`
   account. Never grant general Python, shell, SSH or systemctl privileges.
5. Set `/etc/lastsietch/base-vault-admin.enabled` to `1` on both hosts, root-owned 0600.
   The root broker and exporter each fail closed when this gate is absent,
   unreadable or disabled. Set `LASTSIETCH_BASE_VAULT_ENABLED=1` through the portal's
   service environment without rewriting existing signing or feature settings.
6. Install the tested `admin-backend/base_vault.py`, both changed admin routers,
   and the reviewed admin templates/assets. Restart only `lastsietch-admin.service`.
   Verify its real user, interpreter, working directory, flags, public login,
   Reports and existing portal behavior. This feature needs no game restart,
   relay restart, credential migration or existing catalog schema migration.
7. Use the authenticated admin path to verify history, refusal controls and one
   approved current-state capture. Poll until a completed operation references
   its verified archive, then verify hashes and protected modes as root. Verify
   the actual remote wrapper and exporter used by that worker. Use a disposable
   lab for a fresh typed round-trip when validating a new graph.

Rollback disables the portal flag and both capture gates, then restores the
previous portal code and assets. Preserve the operation journal, new archives,
old release and catalog. An already running read-only worker may finish safely;
do not clear or retry it just because the frontend was rolled back.

### Recovery from uncertain capture

A request left pending or running beyond 350 seconds becomes uncertain and
blocks further capture. A failure once ingest begins is also uncertain because
an immutable archive or catalog row may already exist. A failed source read
before ingest is safe to show as failed. Neither state triggers an automatic
retry, deletion or map operation.

Inspect the exact systemd instance and root-only journal. Reconcile the fixed
label `Admin snapshot <request UUID>` against the catalog and immutable manifests,
then verify any matching archive. Preserve the journal and files before any
operator repair. There is deliberately no HTTP clear/retry-uncertain command;
an operator must prove whether ingest committed before changing that record.
Do not remove an old lock file or mark an operation failed while a worker lives.

## Future restore integration

### Original-location recovery implementation

The restore backend now builds concrete, ten-minute plans for a specific verified
snapshot. `POST /api/dune/v2/claims/{totem_id}/vault/restore-plans` accepts only
`request_id` and `snapshot_id`, with admin authentication, CSRF and audit records.
The plan is an immutable root-only file bound to its initiating admin, snapshot
payload checksum, original totem and original location. Retries reconcile its
index after an interrupted commit. History lists the newest ten plans; a bound
`GET .../restore-plans/{plan_id}` reads one. Neither path returns archive rows or
provider identity mappings.

The fixed remote `restore-probe.py` validates the archive and builds its own
read-only query. It accepts snapshot bytes, never SQL, paths or destination
coordinates. The probe checks captured schema, all captured primary/unique key
collisions, the missing original totem, original partition/map/dimension,
external dependencies and original controller/character/account/provider
bindings. A failed or unavailable probe records unknown checks. Original-location
mode never shifts transforms, reassigns ownership or consumes a BRT.

Partition identity accepts exact actor/partition map equality and only two
verified native map-group pairs: actor `HaggaBasin` with partition `Survival_1`,
and actor `DeepDesert` with partition `DeepDesert_1`. Partition ID and dimension
must still match exactly. No suffix removal, inferred alias or lookalike map name
is accepted.

`restore.py` also implements an actual insert-only transactional recovery for a
disposable lab. It locks target and identity tables in a SERIALIZABLE transaction,
rechecks the captured column/key/check/type metadata, collisions, dependencies,
ownership, partition identity and absence of unreviewed user triggers, then inserts
the exact captured values with explicit column lists. Every table must match the
source exactly before commit. Unknown boolean checks fail closed. SQL preserves
the raw JSON bytes through hex transport, never serializing high-precision numbers
through floating point. It contains no overwrite, upsert, deletion or automatic
retry. A duplicate or lost-reply retry encounters existing IDs and refuses.

Row identity uses a primary key or a captured unique key whose values are all
nonnull. The two reviewed keyless tables use their complete captured parent
scope: `actor_inventories.inventory_id` follows captured inventories, and
`landclaim_segments.totem_id` follows the captured totem. Their actual foreign
keys must match the reviewed schema, including landclaim segments referencing
`actors.id`. Any existing row in either parent scope blocks recovery. Readback
compares the full scoped multiset, preserving duplicate rows. Unsupported
populated keyless tables and null or unselected scoped parents refuse; no keys
are added to production.

Create a new lab using the existing pinned image and isolation requirements above,
then run as root on <web-host>:

```bash
python3 restore.py lab-setup --snapshot UUID --container ls-base-vault-check-NEW
python3 restore.py lab-restore --snapshot UUID --container ls-base-vault-check-NEW
```

Setup creates empty typed target tables and explicit external identity fixtures;
it refuses an existing `dune` schema. Foreign-key actions and CHECK expressions
are retained, while FK deferral is deliberately enabled only in this isolated
lab. The typed schema check normalizes only that deferral suffix. Restore requires
the fixed `vaultcheck` database and a verified immutable container identity.
The resulting receipt records a typed transaction and exact 18-table readback.
It is distinct from an in-game restore. A timeout or ambiguous result is unverified;
inspect the lab before doing anything further.

**Production application is categorically disabled.** The HTTP apply endpoint
authenticates, checks CSRF, audits and refuses. The root helper accepts no apply
action, and the remote probe has no write operation. The SQL executor itself
requires the isolated lab database. There is no environment switch or client
approval field that enables production application.

These remain explicit production blockers:

- A concrete owner-approved target and maintenance window have not been bound.
- The shared maintenance lease coordinates operators but does not prove completed
  shutdown-save, an unloaded map or a fence preventing startup during the write.
- Version-one snapshots lack trusted engine-build provenance. A database schema
  match does not prove engine or cooked-content compatibility.
- Terrain and Coriolis-cycle/layout provenance, spatial overlap, loaded-map
  behavior and live trigger/function effects remain unverified.
- A controlled in-game restoration canary has not been completed for this path.

The historical Deep Desert replant demonstrates
why a reusable lifecycle fence is necessary: its first insert raced map loading.
It also documents terrain changes outside the shield wall. Original coordinates
alone are therefore insufficient placement proof. Do not reuse the older BRT
relocation script, which consumes a different source graph and hardcodes a target.

Deploy planning by including `restore.py`, `restore-probe.py` and the updated
`lab-check.py` with the versioned vault package on both hosts before updating the
root helper and portal routes. Existing capture gates, sudo restrictions, archive
permissions and startup behavior remain. Deployment requires no game restart or
game-data write. Real production planning must show collisions for any base that
still exists, including Medic's current captured base, and must keep Apply disabled.

Preserve the server-side role and audit boundaries when adding recovery controls;
do not expose archive contents or owner identity mappings through player routes.

Suggested sequence:

1. Complete snapshot capture and history deployment and verification first.
2. `Stage for restore` produces a separate non-live recovery plan. Show source
   snapshot, intended target, ownership, schema/build compatibility, actor/item ID
   collisions, missing dependencies and map lifecycle requirements. Staging must
   not modify the game database or imply that a live restore is safe.
3. Keep `Apply restore` unavailable until a separately tested, approval-gated
   recovery workflow exists. Neither a completed snapshot nor a staged plan is
   permission to write into a running map.

The owner authorized snapshot capture and history first, followed by separately
tested restore work. A snapshot does not authorize writes into a loaded map.
