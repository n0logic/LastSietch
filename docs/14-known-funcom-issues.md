# 14. Known Funcom issues and how to recognise them

Symptoms and detection for problems we have hit on a live self-hosted server.
These are observations from operating the software, offered so you can identify
a failure quickly rather than spend an evening on it.

Where we have a local workaround that involves modifying Funcom's own files,
the workaround is not published here. The diagnosis is the useful part anyway,
and it is the part that stays true.

Versions move. Everything below was observed on the builds noted; check whether
it still applies to yours.

## A stopped battlegroup does not recover itself

**Observed:** ongoing.

**Symptom:** the server disappears from the browser and players cannot connect.
Pods look wrong or absent.

**Detection:**

```bash
kubectl -n "$DUNE_NS" get battlegroup "$DUNE_BG" -o jsonpath='{.spec.stop}'
```

If that returns `true`, nothing in the stack will change it back. Any host
reboot can leave it stopped.

**This is the first thing to check on any "server is down" report.** It looks
alarming and is a one-field fix:

```bash
kubectl -n "$DUNE_NS" patch battlegroup "$DUNE_BG" --type=merge -p '{"spec":{"stop":false}}'
```

`ops/lastsietch-bg-watchdog/` automates exactly this, with an attempt limit so it does
not fight a deliberate stop.

## Director crash loop on a null-player login

**Observed:** 1.4.10.4.

**Symptom:** the battlegroup director crash-loops. In the logs, a segmentation
fault on a login request where the player object is null. Players cannot get
in, and the director restarts repeatedly.

**Detection:** watch the director pod's restart count and check its log for a
segfault correlated with login traffic rather than with startup.

**Workaround:** exists locally, involves patching a shipped assembly, and is
not published here. If you hit this, the realistic options are to wait for a
Funcom hotfix or to report it with your logs. Knowing the cause at least tells
you it is not your configuration.

## Sietch travel breaks after a scaling change

**Observed:** 1.4.10.4.

**Symptom:** players cannot travel between sietches or into hub maps. Travel
appears to do nothing, or times out.

**Cause:** a change to how server sets are scaled left always-on sets without a
running server to travel to.

**Fix:** set the affected sets to dedicated scaling with a minimum of one
server, so an instance is always up rather than scaled from zero on demand.
Check your hub and always-on sets first.

## Configuration keys that do nothing

A large number of plausible-looking keys in the shipped configuration files
have no reader in the binary. They are inert. Setting them changes nothing, and
community documentation confidently lists several as working.

Two habits that save time:

- **Confirm a key has a reader before believing it works.** If a key is not
  referenced anywhere in the binary, it is folklore.
- **Prefer console variables over configuration keys** where both appear to
  exist. In our experience the console variable is more often the live lever
  and the similarly named configuration key is the inert one.

A worked example: a base tooling cooldown had both a configuration key and a
console variable with near-identical names. The configuration key was inert on
both client and server. The console variable was the real control, and it only
took effect from the player's own client, not from the server.

**Search caveat:** dotted console variable names may not be stored as plain
ASCII in the server binary. A naive `strings | grep` for them returns nothing
and reads as proof of absence when it is nothing of the sort. If you are
searching a binary for a lever, make sure your probe can find something you
already know is there before you trust a negative result.

## Schema ownership aborts the update

**Observed:** ongoing.

**Symptom:** a Funcom update fails partway through, during its pre-update
database dump, with your server already down.

**Cause:** `pg_dump` aborting on an object owned by a different role. If you
have created your own tables in Funcom's schema as a different user, this is
waiting for you.

**Detection and fix:** `scripts/dune-owner-check.sh`, run before every update.
Fix with targeted `ALTER TABLE ... OWNER TO` statements rather than a
database-wide reassign, and set a short lock timeout so you cannot queue behind
a live transaction.

## Reporting these

If you hit something here and can add detail, open an issue. If it is a genuine
server-side bug, report it to Funcom as well: they read the self-hosting
channels, and a bug fixed upstream is better for everyone than a workaround
passed around between operators.
