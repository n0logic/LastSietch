#!/usr/bin/env python3
"""Copy Solari balance-change events out of dune.event_log before it rotates.

Deployed to the game host:/root/dune-solari-log-snapshot.py (mode 0755), run by a systemd
timer there. Reads ONLY from Funcom tables; writes ONLY to dune.ls_solari_events.

── WHY ────────────────────────────────────────────────────────────────────────
Funcom's dune.adjust_player_virtual_currency_balance() calls log_event_solaris() on EVERY
Solari change, writing a category='solaris' row into dune.event_log with the delta, the
resulting balance and the acting fls_id. So a complete currency audit trail exists by design.

It is also useless beyond about nine days, because event_log rotates. Measured 2026-07-27:
the entire table spanned exactly 9 days.

That cost a real investigation. Three accounts hold ~1e17 Solari between them, 99.99998% of
the money on the server, and their last activity was twelve days before the surviving log
window opened. The rows that would have shown where it came from existed, and had aged out
by the time anyone looked.

So: take a copy while the rows are still there. The next anomaly becomes a query instead of
an archaeology problem.

🔴 TWO LIMITS, both found 2026-07-27 by LT-2. See scripts/sql/ls_solari_events.sql for detail.

1. `solaris_delta` is NOT sign-reliable. `adjust_player_virtual_currency_balance` logs a SIGNED
   delta; `dune_exchange_modify_user_solari_balance` logs an UNSIGNED MAGNITUDE, so an exchange
   PURCHASE of 230,000 logs +230000 while the balance falls. Derive direction from
   `solaris_balance` via lag(), never from the delta. The `notable` list below reports
   `abs(delta)`, which is correct for "how big was this move" and deliberately makes no claim
   about direction -- do not "fix" it into an inflow/outflow label.

2. `character_transfer_import` writes the wallet and logs NOTHING, so this archive cannot see a
   balance that arrives by character transfer. That is the mechanism two of the three
   quadrillion-Solari accounts used, i.e. the exact event this job was built for is the one it
   cannot capture. Catching that needs balance snapshotting or an `account_removal_log` watch.

── WHY IT CANNOT MISS ROWS, AND WHY IT CANNOT DOUBLE-COUNT ────────────────────
It tracks a HIGH-WATER MARK: the greatest event_log_id already archived. Each run copies
everything above it. Combined with the UNIQUE constraint on event_log_id and ON CONFLICT DO
NOTHING, that makes a run idempotent and a missed run harmless -- the next one picks up the
gap, as long as it happens inside the ~9-day retention window. The timer runs hourly, which
leaves a very wide margin.

🔴 The high-water mark is read from OUR table, never stored in a file. A state file can go
stale, be restored from a backup, or be deleted; the destination table cannot disagree with
itself about what it already contains.

⚠️ event_log ids come from a sequence on a partitioned table. If Funcom ever recreates that
sequence, ids could repeat and the UNIQUE constraint would silently skip genuinely new rows.
The run reports when it sees the newest source id go BACKWARDS, which is the only cheap
signal available for that.

Usage:
  dune-solari-log-snapshot.py              # copy new rows, print a json summary
  dune-solari-log-snapshot.py --dry-run    # report what WOULD be copied, write nothing
  dune-solari-log-snapshot.py --human      # json + a readable line on stderr

Exit 0 = ok (including "nothing new"). 1 = copied, but something is worth looking at.
3 = could not run, which is NOT the same as "nothing to do".
"""
import json
import subprocess
import sys

RC_OK, RC_ATTENTION, RC_BROKEN = 0, 1, 3

# A move this large is worth surfacing in the run summary. Not an alert, just a pointer: the
# routine economy trades in thousands, so anything at this scale is either a big legitimate
# purchase or the start of a story.
# ⚠️ Compared with abs() on purpose: the logged delta's SIGN is unreliable across procs (see the
# header), so this measures magnitude only and the report must not imply a direction.
NOTABLE_DELTA = 1_000_000_000

# Every statement here is schema-qualified and there is NO `SET search_path`: a SET leaks its
# own command tag as line 1 of `-At` output, which would corrupt the json parse.
COPY_SQL = r"""
WITH hw AS (
  SELECT COALESCE(max(event_log_id), 0) AS mark FROM dune.ls_solari_events
),
src AS (
  SELECT el.id, el.event_time, el.partition_id,
         el.function_name::text AS function_name,
         el.message::text       AS message,
         el.meta
    FROM dune.event_log el, hw
   WHERE el.category::text = 'solaris'
     AND el.id > hw.mark
),
ins AS (
  INSERT INTO dune.ls_solari_events
    (event_log_id, event_time, partition_id, function_name, message,
     fls_id, solaris_delta, solaris_balance, meta)
  SELECT s.id, s.event_time, s.partition_id, s.function_name, s.message,
         s.meta ->> 'fls_id',
         NULLIF(s.meta ->> 'solaris_delta', '')::bigint,
         NULLIF(s.meta ->> 'solaris_balance', '')::bigint,
         s.meta
    FROM src s
  ON CONFLICT (event_log_id) DO NOTHING
  RETURNING event_log_id, solaris_delta, fls_id
)
SELECT json_build_object(
  'ok', true,
  'high_water_before', (SELECT mark FROM hw),
  'candidates', (SELECT count(*) FROM src),
  'copied', (SELECT count(*) FROM ins),
  'newest_source_id', (SELECT COALESCE(max(id), 0) FROM dune.event_log WHERE category::text = 'solaris'),
  'oldest_source_time', (SELECT min(event_time) FROM dune.event_log WHERE category::text = 'solaris'),
  -- + copied, because every CTE in this statement sees the SAME pre-INSERT snapshot, so a
  -- bare count(*) here reports the archive as it was BEFORE this run's rows landed. The first
  -- run reported "4126 copied, archive 0 rows", which is not wrong so much as useless.
  'archive_rows', (SELECT count(*) FROM dune.ls_solari_events) + (SELECT count(*) FROM ins),
  'archive_span_days', (SELECT COALESCE(EXTRACT(DAY FROM (max(event_time) - min(event_time)))::int, 0)
                          FROM dune.ls_solari_events),
  'notable', (SELECT COALESCE(json_agg(json_build_object(
                 'fls_id', fls_id, 'delta', solaris_delta) ORDER BY abs(solaris_delta) DESC), '[]'::json)
                FROM ins WHERE abs(COALESCE(solaris_delta, 0)) >= {notable})
) AS result
"""

# --dry-run: identical selection, no INSERT.
PEEK_SQL = r"""
WITH hw AS (
  SELECT COALESCE(max(event_log_id), 0) AS mark FROM dune.ls_solari_events
)
SELECT json_build_object(
  'ok', true, 'dry_run', true,
  'high_water_before', (SELECT mark FROM hw),
  'candidates', (SELECT count(*) FROM dune.event_log el, hw
                  WHERE el.category::text = 'solaris' AND el.id > hw.mark),
  'copied', 0,
  'newest_source_id', (SELECT COALESCE(max(id), 0) FROM dune.event_log WHERE category::text = 'solaris'),
  'oldest_source_time', (SELECT min(event_time) FROM dune.event_log WHERE category::text = 'solaris'),
  'archive_rows', (SELECT count(*) FROM dune.ls_solari_events),
  'archive_span_days', (SELECT COALESCE(EXTRACT(DAY FROM (max(event_time) - min(event_time)))::int, 0)
                          FROM dune.ls_solari_events),
  'notable', '[]'::json
) AS result
"""


def run_sql(sql, timeout=180):
    try:
        out = subprocess.run(["/root/dq.sh", "-tAc", sql],
                             capture_output=True, text=True, timeout=timeout)
    except OSError as exc:
        # OSError, not FileNotFoundError: a non-executable dq.sh raises PermissionError, which
        # is a sibling and not a subclass. Letting that escape would exit 1 with no json.
        return None, f"could not run /root/dq.sh: {exc.__class__.__name__}: {exc}"
    except subprocess.TimeoutExpired:
        return None, f"snapshot query timed out after {timeout}s"
    if out.returncode != 0:
        return None, ((out.stderr or out.stdout) or "").strip()[:400]
    return (out.stdout or "").strip(), None


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    dry = "--dry-run" in argv
    want_human = "--human" in argv

    sql = PEEK_SQL if dry else COPY_SQL.replace("{notable}", str(NOTABLE_DELTA))
    raw, err = run_sql(sql)
    if raw is None:
        print(json.dumps({"ok": False, "error": "snapshot_failed", "detail": err,
                          "message": "the Solari log snapshot could not run; rows may be "
                                     "rotating out unarchived"}))
        return RC_BROKEN
    try:
        r = json.loads(raw)
    except json.JSONDecodeError:
        print(json.dumps({"ok": False, "error": "bad_json", "detail": raw[:300],
                          "message": "the Solari log snapshot returned unparseable output"}))
        return RC_BROKEN

    attention = []

    # A sequence reset would make new ids look already-archived and the UNIQUE constraint would
    # silently skip them. This is the only cheap signal for it.
    if r.get("newest_source_id", 0) < r.get("high_water_before", 0):
        attention.append(
            f"event_log's newest solaris id ({r['newest_source_id']}) is BELOW our high-water "
            f"mark ({r['high_water_before']}). The source sequence may have been reset, in "
            f"which case genuinely new rows are being skipped as duplicates. Investigate "
            f"before trusting the archive.")

    # Candidates we chose not to copy means a real conflict, which should not happen.
    missed = (r.get("candidates") or 0) - (r.get("copied") or 0)
    if not dry and missed > 0:
        attention.append(f"{missed} candidate row(s) were not copied (ON CONFLICT). Expected 0 "
                         f"outside a re-run racing itself.")

    for n in (r.get("notable") or []):
        # "magnitude", not "delta": the logged sign cannot be trusted across procs, so naming this
        # an inflow or an outflow would be a guess dressed as a fact.
        attention.append(f"large move: fls={n.get('fls_id')} magnitude={abs(n.get('delta') or 0):,} "
                         f"(direction unknown from the log; derive it from solaris_balance)")

    r["attention"] = attention
    summary = (f"solari snapshot: {r.get('copied')} copied of {r.get('candidates')} candidates "
               f"| archive {r.get('archive_rows')} rows spanning {r.get('archive_span_days')}d "
               f"| source retains from {r.get('oldest_source_time')}")
    r["summary"] = summary
    print(json.dumps(r, default=str))
    if want_human:
        print(summary, file=sys.stderr)
        for a in attention:
            print("  ! " + a, file=sys.stderr)
    return RC_ATTENTION if attention else RC_OK


if __name__ == "__main__":
    sys.exit(main())
