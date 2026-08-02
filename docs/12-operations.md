# 12. Operations: keeping a server alive

The install docs get a server standing up. This one is about the months after
that, when the interesting failures are the quiet ones.

Everything here runs on the game host unless stated otherwise, and everything
here is in production on ours.

## Configuration

All of these read the same environment variables, with self-documenting
placeholder defaults. Set them once, in a shell profile or a systemd
`EnvironmentFile`:

```bash
export DUNE_NS=funcom-seabass-sh-<your-hostid>-<random>   # your namespace
export DUNE_BG=sh-<your-hostid>-<random>                  # your battlegroup
```

Find yours with `kubectl get ns | grep funcom-seabass`, then substitute the
real values. The angle brackets are the convention used throughout this
repository: a placeholder that shows the shape of the value it wants.

Deliberately not shown as a realistic-looking example id. A plausible fake is
indistinguishable from a real one to both a reader and a secret scanner, and
someone always copies the example verbatim.

Install paths in these scripts default to `/opt/<tool>` and are overridable.
Nothing depends on you using the same layout.

## The tools

| Tool | What it does |
|---|---|
| `scripts/dq.sh` | psql into the game database inside its pod. Almost everything else shells out to this. Read its header before editing: it has a real contract |
| `scripts/check-player-presence.sh` | Are players online right now. The guard you run before anything disruptive |
| `scripts/dune-prewindow-check.sh` | Read-only readiness check before a maintenance window: RBAC verbs, cluster health, deploy rails, backup staging |
| `scripts/dune-db-backup.sh` | Database plus config backup, with integrity gates. Timer and service units alongside |
| `scripts/dune-owner-check.sh` | Verifies custom table ownership. See "the pg_dump trap" below. Run before every Funcom update |
| `scripts/apply-dune-memory-limits.sh` | Per-map memory limits, for hosts above Funcom's minimum tier |
| `ops/lastsietch-bg-watchdog/` | Watches for a stopped battlegroup and restarts it, with attempt limits so it does not fight a deliberate change |
| `ops/dune-update-auto/` | Update orchestration and hotfix watching |

## The pg_dump trap

If you add your own tables, **they must be owned by the same role that owns
Funcom's schema.**

Funcom's pre-update process runs `pg_dump`. If it hits an object it does not
own, the dump aborts, and it aborts partway through an update window with your
server down. We found this the safe way, on a check rather than during an
update, and five of our tables were wrong.

`scripts/dune-owner-check.sh` gates on it. Run it before every update.

Related convention: we prefix all our own tables `ls_` and put them in
Funcom's `dune` schema. The prefix is what makes an ownership check writable at
all, since it is the only thing distinguishing our tables from theirs. Pick
your own prefix, but pick one, and do not scatter tables across schemas.

## Restarts

The single most important operational rule on a community server:

**Never restart a game pod, battlegroup, or k3s while players are online.**

Progression is held in RAM and flushed periodically. A restart under load can
cost players work. `check-player-presence.sh` exists so that "is anyone on" is
one command rather than a guess.

Safe to restart at any time: your own web, relay, telemetry, and bot services.
Not safe: anything Funcom's operators manage.

## Free windows

Deep Desert resets on a Coriolis cycle, and that reset restarts everything
anyway. Players expect it and it needs no separate announcement.

That makes every Coriolis boundary a **free maintenance window** for anything
requiring a restart. Ours is roughly 72 seconds from custom resource change to
pods running, 2 to 4 minutes player-visible. If you have a config change that
needs a restart, queue it for the boundary instead of spending a window.

## Backups

Back up more than the database. A restore also needs your configuration: the
custom resources, the `UserGame.ini` and `UserEngine.ini` on the persistent
volume, and your own tooling.

Ours dumps every 6 hours with integrity gates (gzip test, table presence, a
table-count floor), rotates locally, and sweeps offsite. The gates matter more
than the frequency: an unverified backup is a guess.

Worth knowing: a player who loses items to a bug can be made whole from a
pre-incident dump. Pull their inventory from the dump, diff against a
read-only live query, re-grant the delta. That is a real procedure we have run,
not a theoretical one.

## Things that are quiet when they break

- **A stopped battlegroup does not restart itself.** Any host reboot can leave
  it stopped, and nothing recovers it. That is what the watchdog is for, and
  it is the first thing to check when someone says "the server is down".
- **Verify through the executed path, not the deployed file.** If a script
  exists in two places, confirm which one your service actually runs. A deploy
  that edits the wrong copy verifies green and changes nothing.
- **Any live hot fix must be mirrored back to your repository the same day**,
  or the next deploy silently reverts it.
