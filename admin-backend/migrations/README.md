# admin-backend migrations

This directory holds **manual-apply** Postgres migration files for the
`holadmin.*` schema in the **dune** database (NOT admin-backend SQLite).

The admin-backend itself only owns the SQLite admin DB (users / sessions /
audit_log) — `database.py::init_db()` runs on every boot and is idempotent.
Postgres-side schema in the dune database is applied by the operator manually
during a maintenance window, because:

- admin-backend has no Postgres client (the "no new pip deps" hard rule
  blocks adding `psycopg`/`asyncpg`).
- The relay host and lastsietch-dune dispatch helpers have `psql` access,
  but those hosts are not the right place to run schema migrations from.
- Custom `holadmin.*` tables MUST be `OWNER dune` per
  the custom-table ownership rule (Funcom's pre-update `pg_dump`
  halts the entire game update otherwise). Setting the owner correctly
  needs a deliberate manual step, not a startup auto-apply.

## Apply convention

For each `*.sql` file in this dir, run:

```sh
# from a host with psql + dune database creds
psql -h <dune-pg-host> -U postgres -d dune -f admin-backend/migrations/<file>.sql
```

All migration files are written re-run-safe (`CREATE … IF NOT EXISTS`,
idempotent `ALTER TABLE`), so a second `psql -f` is a no-op.

## File naming

`YYYY-MM-DD-<short-slug>.sql` — date-ordered, slug describes the change.
Multiple files per day are fine; add a numeric suffix if needed (e.g.
`2026-05-26-cvar-changes-02.sql`).

## When to add to this dir vs `init_db()`

- New SQLite table / column → patch `database.py::init_db()`.
- New Postgres `holadmin.*` table / column → drop a SQL file here and ping
  the operator to apply.

## Inventory

| File | Schema | Adds |
|---|---|---|
| `2026-05-26-cvar-changes.sql` | `holadmin` | `cvar_changes` table + 2 indexes; CREATE SCHEMA holadmin if missing (P8) |
