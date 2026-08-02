#!/usr/bin/env python3
"""The Karum escrow audit. READ-ONLY, game-host resident.

Deployed to the game host:/root/dune-karum-audit.py (mode 0755). Reached by the
dispatcher's 'karum-audit' action, so the web host can run it nightly over the relay and
an admin can run it on demand from the panel. Emits ONE json object on stdout.

Contract: docs/dune-research/v2-portal/KARUM-BUILD-CONTRACT-2026-07-27.md section 11.2

── WHY THIS EXISTS, AND IT IS TWO JOBS ────────────────────────────────────────
JOB ONE: it is the canary for an offline-gate regression, and that gate is the single
control standing between the Karum and item duplication. If the gate regresses, NOTHING
FAILS LOUDLY. The take succeeds, the seller's still-loaded client resurrects the item
under its original id, and the item now exists in both the player's inventory and Karum
escrow. Nothing errors, nothing alerts. The first signal would be a player noticing, or
not noticing.

That is what makes a nightly read worth having: the failure is silent by construction, so
the only way to see it is to go and look.

JOB TWO: it reconciles the escrow marker against reality, in both directions, because
Karum's escrow rows are definitionally shaped like the ~368k market-bot orphans they sit
among (a row in the exchange inventory with no order row). A cleanup job aimed at the
litter would eat live player goods while looking like housekeeping. This audit is also
what keeps that orphan count VISIBLE, so nobody rediscovers it in a year and reaches for
a purge query without knowing what else lives there.

── IT NEVER REPAIRS ───────────────────────────────────────────────────────────
Read-only, deliberately and permanently. An automated repair against a suspected
duplication event is how one bad row becomes many: every "fix" is a write, every write
against a loaded session is the very hazard being audited, and a script cannot tell a
duplication from a legitimate move. It reports; a human acts through the admin page,
which goes through the same gated writer chain as everything else.

Usage:
  dune-karum-audit.py            # json to stdout
  dune-karum-audit.py --human    # json + a readable summary on stderr

Exit code 0 = nothing to page about. 1 = at least one paging condition fired (the caller
should treat a non-zero exit as "wake someone"). 3 = the audit itself could not run,
which is NOT the same as "clean" and must never be reported as clean.
"""
import json
import subprocess
import sys

EXCHANGE_ID = 2
# Mirrors KARUM_POSITION_BASE in dune-karum-op.sh. Only used to describe where Karum
# allocates; the collision check does not assume it.
KARUM_POSITION_BASE = 1_000_000_000

RC_OK, RC_PAGE, RC_BROKEN = 0, 1, 3

# One query, one round trip. Every table is schema-qualified and there is NO `SET
# search_path`, deliberately: a SET leaks its own command tag as line 1 of `-At` output
# and has silently broken guards in this codebase before.
#
# Cost note: the duplicate-slot check groups over the exchange inventory, which holds
# ~368k rows. That is one sequential scan of one inventory, nightly, on a database the
# market bot hammers continuously. Acceptable. Do not "optimise" it by restricting to the
# Karum position range: the whole point is to catch the bot's allocator climbing INTO that
# range after a restart, and a range-limited check would be blind to the reverse case.
AUDIT_SQL = r"""
WITH exch AS (
  -- 🔴 DELIBERATELY NOT dune.get_exchange_inventory_id(). Two reasons, both found by the
  -- deploy's own executed-path check on 2026-07-27:
  --   1. That function IS NOT READ-ONLY. Read its body: if the lookup misses it INSERTs a new
  --      row into `inventories` and returns it. An audit that claims to be safe under a change
  --      freeze cannot call something that can write, however unlikely the branch.
  --   2. Its body references `inventories` UNQUALIFIED, so it only resolves with `dune` on the
  --      search_path -- and this file avoids `SET search_path` on purpose, because a SET leaks
  --      its own command tag as line 1 of `-At` output and would break the json parse.
  -- The direct read is what the function does on its happy path, minus the write, and it needs
  -- no search_path. A missing exchange inventory is reported, never created.
  SELECT id AS inv FROM dune.inventories
   WHERE exchange_id = {exchange_id}
   ORDER BY id LIMIT 1
),
held AS (
  SELECT e.id, e.correlation_id, e.listing_id, e.item_id, e.inventory_id AS expect_inv,
         e.seller_account_id, e.template_id, e.stack_size, e.quality_level, e.held_at
    FROM dune.ls_karum_escrow e
   WHERE e.state = 'held'
),
resolved AS (
  SELECT h.*,
         it.inventory_id  AS actual_inv,
         inv.inventory_type AS actual_type,
         (SELECT eps.account_id
            FROM dune.encrypted_player_state eps
           WHERE eps.player_pawn_id = inv.actor_id
           LIMIT 1) AS actual_owner_account
    FROM held h
    LEFT JOIN dune.items it  ON it.id = h.item_id
    LEFT JOIN dune.inventories inv ON inv.id = it.inventory_id
),
dupe_slots AS (
  SELECT k.position_index, count(*) AS occupants
    FROM dune.items k
   WHERE k.inventory_id = (SELECT inv FROM exch)
     AND k.position_index IN (
           SELECT x.position_index FROM dune.items x
            WHERE x.inventory_id = (SELECT inv FROM exch)
              AND x.id IN (SELECT item_id FROM held))
   GROUP BY k.position_index
  HAVING count(*) > 1
)
SELECT json_build_object(
  'ok', true,
  'read_at', now(),
  'exchange_inv', (SELECT inv FROM exch),
  'karum_position_base', {position_base},

  -- Ledger census. `held` is what we believe we are holding right now.
  'escrow_states', (SELECT COALESCE(json_object_agg(state, n), '{{}}'::json)
                      FROM (SELECT state, count(*) AS n
                              FROM dune.ls_karum_escrow GROUP BY state) s),
  'payment_states', (SELECT COALESCE(json_object_agg(status, n), '{{}}'::json)
                       FROM (SELECT status, count(*) AS n
                               FROM dune.ls_karum_payments GROUP BY status) p),
  'held_total', (SELECT count(*) FROM held),

  -- HEALTHY: the row is where the ledger says it is.
  'healthy', (SELECT count(*) FROM resolved
               WHERE actual_inv IS NOT NULL AND actual_inv = expect_inv),

  -- 🔴 EVAPORATED: the ledger holds it and dune.items does not. A purge, a cull, or a
  -- manual delete. The buyer is owed a refund if their listing is mid-sale.
  'evaporated', (SELECT COALESCE(json_agg(json_build_object(
                    'escrow_id', id, 'listing_id', listing_id, 'item_id', item_id,
                    'template_id', template_id, 'stack_size', stack_size,
                    'quality_level', quality_level,
                    'seller_account_id', seller_account_id, 'held_at', held_at)), '[]'::json)
                   FROM resolved WHERE actual_inv IS NULL),

  -- 🔴 MOVED: the row exists but not where escrow parked it. NOTHING should be able to do
  -- this. It is the duplication signature from the 2026-07-26 test: a resurrected row
  -- comes back under its ORIGINAL item id, so a move into a player-owned inventory is the
  -- offline gate having failed. `actual_type` 20 is the worst case of all: that is the
  -- in-person trade window, which only holds rows while a trade is OPEN, i.e. while the
  -- session is loaded.
  'moved', (SELECT COALESCE(json_agg(json_build_object(
                    'escrow_id', id, 'listing_id', listing_id, 'item_id', item_id,
                    'template_id', template_id,
                    'expected_inventory', expect_inv, 'actual_inventory', actual_inv,
                    'actual_inventory_type', actual_type,
                    'actual_owner_account', actual_owner_account,
                    'seller_account_id', seller_account_id,
                    'in_player_hands', (actual_owner_account IS NOT NULL),
                    'in_live_trade_window', (actual_type = 20))), '[]'::json)
              FROM resolved WHERE actual_inv IS NOT NULL AND actual_inv <> expect_inv),

  -- 🔴 SENTINEL WITHOUT A LEDGER ROW: a Karum write landed and its ledger insert did not
  -- (which one transaction should make impossible), or somebody hand-wrote the sentinel.
  -- Page, do NOT auto-clean: an unexplained marked row is evidence, not litter.
  'sentinel_orphans', (SELECT COALESCE(json_agg(json_build_object(
                          'item_id', it.id, 'template_id', it.template_id,
                          'position_index', it.position_index,
                          'marker', it.stats -> 'HolKarum')), '[]'::json)
                         FROM dune.items it
                        WHERE it.inventory_id = (SELECT inv FROM exch)
                          AND it.stats ? 'HolKarum'
                          AND NOT EXISTS (SELECT 1 FROM dune.ls_karum_escrow e
                                           WHERE e.item_id = it.id)),

  -- REPORT ONLY, NEVER ACT. The market bot's own litter: rows in the exchange inventory
  -- with no order row and no Karum marker. Baseline ~368k as of 2026-07-27 and expected
  -- to trend up. This line exists so the number stays visible.
  'unmarked_orphans', (SELECT count(*) FROM dune.items it
                        WHERE it.inventory_id = (SELECT inv FROM exch)
                          AND NOT EXISTS (SELECT 1 FROM dune.dune_exchange_orders o
                                           WHERE o.item_id = it.id)
                          AND NOT (COALESCE(it.stats, '{{}}'::jsonb) ? 'HolKarum')
                          AND NOT EXISTS (SELECT 1 FROM dune.ls_karum_escrow e
                                           WHERE e.item_id = it.id AND e.state = 'held')),

  -- LT-7. The market bot caches MAX(position_index) over this inventory ONCE at init and
  -- increments locally, so it can land on a slot Karum already holds. The high Karum base
  -- shrinks the window but does NOT close it: Karum rows become the MAX, so the next bot
  -- restart caches just above them and both allocators climb from adjacent points again.
  -- Consequences in an exchange inventory are UNPROVEN (inferred harmless, because the
  -- exchange addresses items through orders rather than by slot). Report, do not act.
  'slot_collisions', (SELECT COALESCE(json_agg(json_build_object(
                         'position_index', position_index, 'occupants', occupants)), '[]'::json)
                        FROM dupe_slots)
) AS audit
"""


def run_sql(sql, timeout=180):
    """dq.sh is the blessed psql wrapper on this host. -tAc: tuples only, unaligned,
    single command. Read-only here by construction: the statement is a lone SELECT."""
    try:
        out = subprocess.run(["/root/dq.sh", "-tAc", sql],
                             capture_output=True, text=True, timeout=timeout)
    except OSError as exc:
        # OSError, not FileNotFoundError: a missing dq.sh raises one subclass and a
        # non-executable or unreadable one raises another (PermissionError). Catching only
        # the first let the second escape as a traceback with exit code 1 -- which is also
        # the PAGE code -- and no json on stdout, so a caller would have seen a crash as
        # "findings found" and then failed to parse an empty response.
        return None, f"could not run /root/dq.sh: {exc.__class__.__name__}: {exc}"
    except subprocess.TimeoutExpired:
        return None, f"audit query timed out after {timeout}s"
    if out.returncode != 0:
        return None, ((out.stderr or out.stdout) or "").strip()[:400]
    return (out.stdout or "").strip(), None


def classify(a):
    """Turn the raw read into a verdict. The ONLY things that page are the ones that
    cannot have a benign explanation."""
    findings = []

    for row in a.get("moved") or []:
        if row.get("in_live_trade_window"):
            findings.append(
                f"DUPLICATION SUSPECTED: escrowed item {row['item_id']} "
                f"(listing {row['listing_id']}) is sitting in inventory "
                f"{row['actual_inventory']}, which is an IN-PERSON TRADE WINDOW (type 20). "
                f"That inventory only holds rows while a trade is open, so this is a loaded "
                f"session holding goods the ledger says are in escrow.")
        elif row.get("in_player_hands"):
            findings.append(
                f"DUPLICATION SUSPECTED: escrowed item {row['item_id']} "
                f"(listing {row['listing_id']}) is in a PLAYER inventory "
                f"{row['actual_inventory']} owned by account {row['actual_owner_account']}, "
                f"not in escrow inventory {row['expected_inventory']}. This is the "
                f"offline-gate regression signature.")
        else:
            findings.append(
                f"ESCROW MOVED: item {row['item_id']} (listing {row['listing_id']}) is in "
                f"inventory {row['actual_inventory']} (type {row['actual_inventory_type']}), "
                f"expected {row['expected_inventory']}. Nothing should be able to move it.")

    for row in a.get("evaporated") or []:
        findings.append(
            f"ESCROW EVAPORATED: item {row['item_id']} (listing {row['listing_id']}, "
            f"{row['stack_size']}x {row['template_id']} grade {row['quality_level']}) is in "
            f"the ledger as held but is GONE from dune.items. Seller account "
            f"{row['seller_account_id']}. If the listing is selling or paid_undelivered the "
            f"buyer is owed a refund; use the admin page, not SQL.")

    for row in a.get("sentinel_orphans") or []:
        findings.append(
            f"MARKED ROW WITH NO LEDGER: item {row['item_id']} ({row['template_id']}) in the "
            f"exchange inventory carries a HolKarum marker but has no escrow row. Do NOT "
            f"clean it up; work out where it came from.")

    warnings = []
    for row in a.get("slot_collisions") or []:
        warnings.append(
            f"SLOT COLLISION (LT-7): position_index {row['position_index']} in the exchange "
            f"inventory has {row['occupants']} occupants, one of them Karum escrow. Inferred "
            f"harmless because the exchange addresses items through orders, but it is "
            f"unproven and it means the bot's allocator has re-converged with ours.")

    return findings, warnings


def human(a, findings, warnings):
    st = a.get("escrow_states") or {}
    lines = [
        "Karum escrow audit",
        f"  read at            {a.get('read_at')}",
        f"  exchange inventory {a.get('exchange_inv')}",
        f"  escrow held        {a.get('held_total')}  (healthy {a.get('healthy')})",
        f"  ledger states      {', '.join(f'{k}={v}' for k, v in sorted(st.items())) or 'none'}",
        f"  payments           {', '.join(f'{k}={v}' for k, v in sorted((a.get('payment_states') or {}).items())) or 'none'}",
        f"  bot litter in 610  {a.get('unmarked_orphans')}  (report only, NEVER purge without"
        f" excluding Karum-marked rows)",
    ]
    if findings:
        lines.append("")
        lines.append(f"  🔴 {len(findings)} finding(s) that need a human:")
        lines += [f"     - {f}" for f in findings]
    if warnings:
        lines.append("")
        lines.append(f"  ⚠️ {len(warnings)} warning(s):")
        lines += [f"     - {w}" for w in warnings]
    if not findings and not warnings:
        lines.append("")
        lines.append("  ✅ nothing to act on")
    return "\n".join(lines)


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    want_human = "--human" in argv

    sql = AUDIT_SQL.format(exchange_id=EXCHANGE_ID, position_base=KARUM_POSITION_BASE)
    raw, err = run_sql(sql)
    if raw is None:
        # 🔴 A broken audit is NOT a clean audit. Say so, and exit distinctly, so a caller
        # cannot mistake "we could not look" for "we looked and it was fine".
        print(json.dumps({"ok": False, "error": "audit_failed", "detail": err,
                          "page": True,
                          "message": "the Karum audit could not run; this is NOT a clean result"}))
        return RC_BROKEN
    try:
        a = json.loads(raw)
    except json.JSONDecodeError:
        print(json.dumps({"ok": False, "error": "bad_json", "page": True,
                          "detail": raw[:300],
                          "message": "the Karum audit returned unparseable output"}))
        return RC_BROKEN

    if a.get("exchange_inv") is None:
        # Nothing to reconcile against. Not clean: it means the inventory the whole feature
        # escrows into could not be resolved.
        print(json.dumps({"ok": False, "error": "no_exchange_inventory", "page": True,
                          "message": "could not resolve the exchange inventory "
                                     "(dune.inventories has no row with exchange_id = %d); "
                                     "this is NOT a clean result" % EXCHANGE_ID}))
        return RC_BROKEN

    findings, warnings = classify(a)
    a["findings"] = findings
    a["warnings"] = warnings
    a["page"] = bool(findings)
    a["summary"] = human(a, findings, warnings)
    print(json.dumps(a, default=str))
    if want_human:
        print(a["summary"], file=sys.stderr)
    return RC_PAGE if findings else RC_OK


if __name__ == "__main__":
    sys.exit(main())
