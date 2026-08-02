#!/usr/bin/env python3
"""Verify the Landsraad weekly house goal dropped to the new target at the
next term rollover.

Background: on 2026-06-02 the cvar m_TaskGoalAmount was changed 70000 -> 20000
in UserGame.ini (confirmed live in the pods + golden file). The goal is baked
per-task into dune.landsraad_tasks.goal_amount at TERM CREATION. The active term
(term 3, started 2026-06-02 04:55 UTC) predates the change, so its 25 houses are
all still 70000. Landsraad terms roll weekly (~Tuesdays 04:55 UTC); the next term
(term 4) generates ~2026-06-09 04:55 UTC and should read the live cvar -> 20000.

This script checks the LATEST term and reports:
  PASS    - new term has rolled (start >= ROLLOVER_AFTER) and all goals == EXPECT
  FAIL    - new term rolled but goals != EXPECT (cvar did NOT drive the goal;
            likely pak-baked or read elsewhere -> investigate)
  PENDING - the latest term still predates the rollover (term hasn't rolled yet)

Read-only. Usage: dune-verify-landsraad-goal.py
"""
import json
import subprocess
import sys
from datetime import datetime, timezone

NS = "funcom-seabass-sh-<your-hostid>-<random>"
DB_POD = "sh-<your-hostid>-<random>-db-dbdepl-sts-0"

EXPECT = 20000
PREV = 70000
# The new term should start at/after this instant (term-4 rollover ~04:55 UTC).
ROLLOVER_AFTER = datetime(2026, 6, 9, 4, 0, 0, tzinfo=timezone.utc)


def kubectl(args):
    return subprocess.run(["sudo", "kubectl", *args],
                          capture_output=True, text=True, timeout=60)


def psql(sql):
    pw = kubectl(["exec", "-n", NS, DB_POD, "--", "printenv", "POSTGRES_PASSWORD"]).stdout.strip()
    r = subprocess.run(
        ["sudo", "kubectl", "exec", "-i", "-n", NS, DB_POD, "--", "env", f"PGPASSWORD={pw}",
         "psql", "-h", "localhost", "-p", "15432", "-U", "postgres", "-d", "dune",
         "-t", "-A", "-F|", "-v", "ON_ERROR_STOP=1"],
        input=sql, capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(f"psql failed: {r.stderr.strip()}")
    return [ln.split("|") for ln in r.stdout.splitlines() if ln.strip()]


def main():
    term_rows = psql(
        "SELECT term_id, start_time FROM dune.landsraad_decree_term "
        "ORDER BY term_id DESC LIMIT 1;"
    )
    if not term_rows:
        print("ERROR: no landsraad terms found")
        return 3
    term_id = int(term_rows[0][0])
    start_raw = term_rows[0][1]

    goal_rows = psql(
        "SELECT goal_amount, count(*) FROM dune.landsraad_tasks "
        f"WHERE term_id = {term_id} GROUP BY goal_amount ORDER BY goal_amount;"
    )
    goals = {int(g): int(c) for g, c in goal_rows}

    # parse start_time (postgres ISO-ish, e.g. '2026-06-09 04:55:00.12+00')
    try:
        start_dt = datetime.fromisoformat(start_raw.replace(" ", "T"))
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
    except Exception:
        start_dt = None

    rolled = bool(start_dt and start_dt >= ROLLOVER_AFTER)
    all_expect = bool(goals) and set(goals.keys()) == {EXPECT}

    if not rolled:
        verdict = "PENDING"
        msg = (f"Term {term_id} (started {start_raw}) still predates the "
               f"{ROLLOVER_AFTER.isoformat()} rollover; goals not yet regenerated. "
               f"Current goal spread: {goals}. Re-check after the next rollover.")
        code = 2
    elif all_expect:
        verdict = "PASS"
        total = sum(goals.values())
        msg = (f"Term {term_id} (started {start_raw}) rolled with ALL {total} house "
               f"goals == {EXPECT}. The m_TaskGoalAmount cvar DID drive the new "
               f"term's goal. (was {PREV})")
        code = 0
    else:
        verdict = "FAIL"
        msg = (f"Term {term_id} (started {start_raw}) rolled but goals are {goals}, "
               f"NOT all {EXPECT}. The cvar did NOT take effect as expected — the "
               f"goal may be pak-baked or read from another source. INVESTIGATE.")
        code = 1

    out = {
        "verdict": verdict,
        "checked_utc": datetime.now(timezone.utc).isoformat(),
        "latest_term_id": term_id,
        "term_start": start_raw,
        "rolled": rolled,
        "goal_spread": goals,
        "expected": EXPECT,
        "message": msg,
    }
    print(f"[{verdict}] {msg}")
    print(json.dumps(out))
    return code


if __name__ == "__main__":
    sys.exit(main())
