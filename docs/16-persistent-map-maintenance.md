# Persistent maps and safe maintenance

Player-facing sietch names are not server map identifiers. Several sietches can
use the same map family with separate dimensions and partitions. Always discover
the live mapping instead of copying another operator's partition numbers.

| Map family | Typical role | Admission consideration |
|---|---|---|
| Survival_1 | Hagga Basin sietches | A family-wide hold affects every configured dimension |
| DeepDesert_1 | Deep Desert instances | Check all affected dimensions and the scheduled reset window |
| SH_Arrakeen, SH_HarkoVillage | City instances | Verify instance count, capacity and scaling before a hold |
| Overmap | Overland travel | Verify the single-server mode independently |
| Mission, dungeon and testing maps | Temporary or warm instances | Empty content does not prove a process or credential was replaced |

A deployment might have three Hagga sietches, two Deep Desert instances, two
cities and an Overmap server. That is an example, not an eight-server invariant.
Configured topology and lifecycle behavior can change with a server update.

## Empty means more than no connected players

Read fresh, identity-matched observations for every member affected by admission
changes. Include connected players, pending travel, issued grants, completions,
queues and reconnect grace. Missing, stale or unrecognized fields mean unknown,
not zero. Require advancing reports over a quiet interval and recheck after a
hold; a previously issued arrival may still complete.

Bind a restart request to the exact current pod identity. Preserve the normal
shutdown grace and persistent storage. A successful deletion request does not
prove graceful saving, replacement readiness, or restored service.

## One operation at a time

An operation needs an announced maintenance scope, a durable ownership record,
fresh verified backups, exact original configuration and independent recovery.
Coordinate build updates, scheduled resets and manual maintenance through the
same ownership mechanism. Do not steal an old-looking lock or erase an uncertain
journal. Keep recovery available when disabling new work.

With shared admission controls, require the whole affected family empty, restart
one selected instance, verify recovery and restore admission before selecting
again. Busy families need not prevent work on unrelated empty maps. Do not
restart healthy instances merely to complete a checklist.

If a request's outcome is uncertain, inspect its effect before acting again.
Do not repeat a deletion or clear somebody else's configuration override.
After a build or topology change, revalidate the assumptions before rearming.

## Backup and recovery boundaries

Keep a consistent complete database backup and validate an independent copy by
restoring it into an isolated disposable database. Do not confuse listing a dump
with restoring it. Match identities, ownership and relationships as well as row
counts. Bounded per-table digests can compare large saved-state sets without
loading raw player graphs into the controller.

A database snapshot does not capture unsaved process memory. Fuel, serials,
weather and similar runtime fields can change with elapsed time or startup;
that is a reason to investigate differences, not to dismiss every difference.
Never restore into a live production database as a test.

Temporary instances may resolve an expiry naturally if a replacement obtains a
verified fresh credential. A content reset, an empty map, a missing instance,
or a new process identity alone does not prove healthy authentication.

These are design constraints, not a universal restart recipe. This repository
does not publish a native-memory credential reader or an armed rolling package.
