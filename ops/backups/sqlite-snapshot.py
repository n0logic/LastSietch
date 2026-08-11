#!/usr/bin/env python3
"""Consistent snapshots of the WAL-mode SQLite databases, for restic.

WHY THIS EXISTS. vps-backup.sh excludes *.sqlite-wal / *.sqlite-shm from restic,
which is correct on its own terms: a -wal captured at a different instant than
its main file is worse than useless. But nothing was producing a consistent
single-file copy first, so restic was archiving ONLY the main file. In WAL mode
the main file is not updated until a checkpoint, so those snapshots restore to
whenever the database last checkpointed.

Measured on 2026-08-04: /opt/cielago/data/support.sqlite was 364 KB with a
4.1 MB WAL, and its main file had not changed since Aug 1 while the bot wrote
continuously. A restore would have silently lost days, and looked fine doing it
-- the file size and mtime are unremarkable. `cp` of the main file has the same
flaw, which is how three hand-made backups in that directory ended up stale.

VACUUM INTO is the fix: it produces a guaranteed-consistent single file even
while another process is writing, which cp/docker cp cannot. The target must not
already exist, so each snapshot is written to .tmp and renamed into place --
that also means a failed run leaves the PREVIOUS good snapshot untouched rather
than a half-written one.

🔴 FAILS LOUDLY. A silent snapshot failure recreates exactly the bug being
fixed: restic would keep backing up a stale snapshot and every log line would
say OK. Any failure exits non-zero so vps-backup.sh's fail() sends the alert.
"""

import os
import sqlite3
import subprocess
import sys

DEST_DIR = "/opt/backups/sqlite"

# Host-side databases. Anything in WAL mode inside a restic-backed path belongs
# here; a database in `delete` journal mode does not need it (idea-history.db,
# openscores.db) but including one is harmless.
DATABASES = [
    "/opt/cielago/data/support.sqlite",
    "/opt/lastsietch-admin/admin.db",
    "/opt/lastsietch-admin/mirror.sqlite",
]

# Databases living inside a container. `docker cp` of the main file has the same
# WAL problem, so snapshot INSIDE the container first, then copy the snapshot.
CONTAINERS = [
    ("famplan-backend-1", "/app/data/famplan.db", "/opt/backups/famplan/famplan.db"),
]

BUSY_TIMEOUT_MS = 30000


def _snapshot(src: str, dest: str) -> int:
    """VACUUM INTO a temp file then rename. Returns the snapshot's byte size."""
    tmp = dest + ".tmp"
    for path in (tmp,):
        if os.path.exists(path):
            os.unlink(path)
    conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("VACUUM INTO ?", (tmp,))
    finally:
        conn.close()

    # Prove the snapshot is readable before it replaces the previous one.
    check = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
    try:
        pages = check.execute("PRAGMA page_count").fetchone()[0]
        if not pages:
            raise RuntimeError("snapshot has zero pages")
        check.execute("PRAGMA quick_check").fetchone()
    finally:
        check.close()

    os.replace(tmp, dest)
    return os.path.getsize(dest)


def main() -> int:
    os.makedirs(DEST_DIR, exist_ok=True)
    failures = []

    for src in DATABASES:
        name = os.path.basename(src)
        dest = os.path.join(DEST_DIR, name + ".snapshot")
        if not os.path.exists(src):
            print(f"SKIP  {name}: not present")
            continue
        try:
            size = _snapshot(src, dest)
            wal = src + "-wal"
            wal_sz = os.path.getsize(wal) if os.path.exists(wal) else 0
            print(f"OK    {name}: {size} bytes (WAL at snapshot time: {wal_sz})")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}: {exc}")
            print(f"FAIL  {name}: {exc}")

    for container, inner, dest in CONTAINERS:
        name = os.path.basename(inner)
        inner_tmp = "/tmp/" + name + ".snapshot"
        try:
            subprocess.run(
                ["docker", "exec", container, "python3", "-c",
                 "import sqlite3,os,sys\n"
                 "src,dst=sys.argv[1],sys.argv[2]\n"
                 "os.path.exists(dst) and os.unlink(dst)\n"
                 "c=sqlite3.connect('file:%s?mode=ro'%src,uri=True)\n"
                 "c.execute('PRAGMA busy_timeout=30000')\n"
                 "c.execute('VACUUM INTO ?',(dst,))\n"
                 "c.close()\n",
                 inner, inner_tmp],
                check=True, capture_output=True, timeout=600)
            subprocess.run(["docker", "cp", f"{container}:{inner_tmp}", dest],
                           check=True, capture_output=True, timeout=600)
            subprocess.run(["docker", "exec", container, "rm", "-f", inner_tmp],
                           check=False, capture_output=True, timeout=60)
            print(f"OK    {name} (via {container}): {os.path.getsize(dest)} bytes")
        except Exception as exc:  # noqa: BLE001
            detail = getattr(exc, "stderr", b"")
            detail = detail.decode(errors="replace")[:300] if detail else exc
            failures.append(f"{name}: {detail}")
            print(f"FAIL  {name}: {detail}")

    if failures:
        print("SNAPSHOT FAILURES: " + "; ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
