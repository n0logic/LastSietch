#!/usr/bin/env python3
"""Last Sietch market-bot control surface for the V2 admin panel.

Invoked on lastsietch-dune via the relay dispatcher (forced command). Read actions emit
JSON on stdout; write actions take a base64 JSON job on stdin.

Actions (argv[1]):
  status          -> JSON: service state + bot balance + ls_market_log activity.
  listings-search -> argv[2] term; JSON: matching active exchange listings.
  policy-get      -> JSON: the current market-policy.json (verbatim, validated).
  policy-set   -> read base64 JSON policy on stdin; validate, backup, write.
  service      -> argv[2] in {start,stop,restart}; systemctl the bot service.

Read-only by default; policy-set/service are the only writers and each backs up
or is reversible. DB reads go through /root/dq.sh (same wrapper the bot's docs
use). No secrets are printed. Mirrors the dune-cvars-* helper conventions.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys

SERVICE = "lastsietch-market-bot"
POLICY_PATH = "/opt/lastsietch-market-bot/market-policy.json"
DQ = "/root/dq.sh"
ALLOWED_SERVICE_VERBS = {"start", "stop", "restart"}


def die(msg: str, code: int = 1):
    print(json.dumps({"status": "error", "error": msg}))
    sys.exit(code)


def _dq(sql: str, timeout: int = 25) -> str:
    """Run one SQL statement through dq.sh. All table/function refs must be
    schema-qualified (dune.*) so no search_path/SET is needed -- a leading SET
    would otherwise print a 'SET' command tag and corrupt JSON output. Returns
    the last non-empty stdout line (tuples-only, unaligned)."""
    try:
        r = subprocess.run([DQ, "-tAc", sql], capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ""
    if r.returncode != 0:
        return ""
    # json_agg can wrap the result across several lines; return the whole blob
    # (json.loads tolerates embedded whitespace/newlines). All refs are
    # dune.-qualified so there is no SET command tag to strip.
    return (r.stdout or "").strip()


def _systemctl_state() -> dict:
    out = {}
    try:
        r = subprocess.run(
            ["systemctl", "show", SERVICE, "--no-page",
             "--property=ActiveState,SubState,ExecMainPID,ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=10)
        for line in (r.stdout or "").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k] = v
    except subprocess.TimeoutExpired:
        pass
    return {
        "active": out.get("ActiveState", "unknown"),
        "sub": out.get("SubState", "unknown"),
        "pid": out.get("ExecMainPID") or None,
        "since": out.get("ActiveEnterTimestamp") or None,
    }


def action_status():
    # Single round-trip JSON build keeps it cheap; the bot actor is class 'Revy'.
    sql = (
        "SELECT json_build_object("
        "  'owner_id', (SELECT id FROM dune.actors WHERE class='Revy' ORDER BY id LIMIT 1),"
        "  'balance', (SELECT solari_balance FROM dune.dune_exchange_users"
        "      WHERE owner_id=(SELECT id FROM dune.actors WHERE class='Revy' ORDER BY id LIMIT 1)),"
        "  'total_logged', (SELECT count(*) FROM dune.ls_market_log),"
        "  'sells_7d_units', (SELECT COALESCE(SUM(stack_size),0) FROM dune.ls_market_log"
        "      WHERE completion_type=4"
        "        AND owner_id=(SELECT id FROM dune.actors WHERE class='Revy' ORDER BY id LIMIT 1)"
        "        AND logged_at > now()-interval '7 days'),"
        "  'buys_7d', (SELECT count(*) FROM dune.ls_market_log"
        "      WHERE bot_trade IS TRUE AND logged_at > now()-interval '7 days'),"
        "  'active_npc_listings', (SELECT count(*) FROM dune.dune_exchange_orders"
        "      WHERE is_npc_order IS TRUE"
        "        AND owner_id=(SELECT id FROM dune.actors WHERE class='Revy' ORDER BY id LIMIT 1)),"
        "  'recent', (SELECT COALESCE(json_agg(r),'[]'::json) FROM ("
        "      SELECT logged_at, template_id, item_price, stack_size, bot_trade"
        "      FROM dune.ls_market_log ORDER BY logged_at DESC LIMIT 15) r)"
        ")")
    raw = _dq(sql)
    try:
        db = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        db = {}
    print(json.dumps({
        "status": "ok",
        "service": _systemctl_state(),
        "db_ok": bool(raw),
        "bot": {
            "owner_id": db.get("owner_id"),
            "balance": db.get("balance"),
            "active_npc_listings": db.get("active_npc_listings"),
        },
        "activity": {
            "total_logged": db.get("total_logged"),
            "sells_7d_units": db.get("sells_7d_units"),
            "buys_7d": db.get("buys_7d"),
            "recent": db.get("recent") or [],
        },
        "policy_path": POLICY_PATH,
    }))


def action_listings_search(term: str):
    # The term is interpolated into the SQL LIKE, so it is charset-restricted to
    # exactly what a template_id can contain (letters/digits/_/-). No quotes,
    # spaces, or SQL metacharacters can pass -- injection-safe by construction.
    if not term or not re.fullmatch(r"[A-Za-z0-9_-]{2,64}", term):
        die("search term must be 2-64 chars of letters, digits, _ or -")
    like = "%" + term + "%"
    sql = (
        "SELECT json_build_object("
        "  'total_matches', (SELECT count(*) FROM dune.dune_exchange_orders o"
        "      JOIN dune.dune_exchange_sell_orders s ON s.order_id=o.id"
        "      WHERE o.template_id ILIKE '" + like + "'),"
        "  'listings', (SELECT COALESCE(json_agg(r),'[]'::json) FROM ("
        "      SELECT o.id AS order_id, o.revision, o.template_id, o.item_price,"
        "             o.quality_level, o.is_npc_order,"
        "             COALESCE(i.stack_size, s.initial_stack_size) AS stack, o.owner_id"
        "      FROM dune.dune_exchange_orders o"
        "      JOIN dune.dune_exchange_sell_orders s ON s.order_id=o.id"
        "      LEFT JOIN dune.items i ON i.id=o.item_id"
        "      WHERE o.template_id ILIKE '" + like + "'"
        "      ORDER BY o.is_npc_order, o.item_price LIMIT 200) r)"
        ")")
    raw = _dq(sql)
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        data = {}
    listings = data.get("listings") or []
    print(json.dumps({
        "status": "ok",
        "query": term,
        "total_matches": data.get("total_matches"),
        "shown": len(listings),
        "limit": 200,
        "listings": listings,
    }))


def fetch_all_listings(qjson):
    """Collector-only: ALL active exchange listings (no term filter), for the
    the web host market mirror, which runs the substring search LOCALLY. Returns a
    list of {order_id,revision,template_id,item_price,quality_level,is_npc_order,
    stack,owner_id} (order_id+revision let the portal carry the listing identity
    into a BUY, and feed the writer's revision-drift guard).
    Same projection as action_listings_search's listing rows so the local search
    reproduces the live payload. `qjson(sql, fallback)` is the injected runner.
   """
    sql = (
        "SELECT COALESCE(json_agg(json_build_object("
        "  'order_id', o.id, 'revision', o.revision,"
        "  'template_id', o.template_id, 'item_price', o.item_price,"
        "  'quality_level', o.quality_level, 'is_npc_order', o.is_npc_order,"
        "  'stack', COALESCE(i.stack_size, s.initial_stack_size), 'owner_id', o.owner_id"
        ")), '[]'::json)"
        "  FROM dune.dune_exchange_orders o"
        "  JOIN dune.dune_exchange_sell_orders s ON s.order_id=o.id"
        "  LEFT JOIN dune.items i ON i.id=o.item_id")
    return qjson(sql, "[]") or []


def action_policy_get():
    try:
        with open(POLICY_PATH, encoding="utf-8") as fh:
            policy = json.load(fh)
    except (OSError, ValueError) as exc:
        die(f"could not read policy: {exc}")
    print(json.dumps({"status": "ok", "policy": policy}))


def _validate_policy(policy: dict):
    if not isinstance(policy, dict):
        die("policy must be a JSON object")
    # Shape-check the known sections; unknown keys are allowed (forward-compat).
    po = policy.get("price_overrides", {})
    if po and not isinstance(po, dict):
        die("price_overrides must be an object")
    for tid, ov in (po or {}).items():
        if not isinstance(ov, dict):
            die(f"price_overrides.{tid} must be an object")
        for k in ("min_price", "max_price"):
            if k in ov and not isinstance(ov[k], int):
                die(f"price_overrides.{tid}.{k} must be an integer")
    wb = policy.get("weekly_budget", {})
    if wb and not isinstance(wb, dict):
        die("weekly_budget must be an object")
    bs = policy.get("blocked_sellers", [])
    if bs and (not isinstance(bs, list) or any(not isinstance(x, int) for x in bs)):
        die("blocked_sellers must be a list of integers")


def action_policy_set():
    raw_b64 = sys.stdin.read().strip()
    if not raw_b64:
        die("policy-set requires a base64 JSON job on stdin")
    try:
        decoded = base64.b64decode(raw_b64, validate=True).decode("utf-8")
        policy = json.loads(decoded)
    except (ValueError, UnicodeDecodeError) as exc:
        die(f"invalid base64/JSON policy: {exc}")
    _validate_policy(policy)
    # Backup the current file, then atomically replace.
    ts = subprocess.run(["date", "-u", "+%Y%m%dT%H%M%SZ"],
                        capture_output=True, text=True).stdout.strip()
    try:
        if os.path.exists(POLICY_PATH):
            with open(POLICY_PATH, encoding="utf-8") as fh:
                prior = fh.read()
            with open(f"{POLICY_PATH}.bak-{ts}", "w", encoding="utf-8") as fh:
                fh.write(prior)
        tmp = f"{POLICY_PATH}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(policy, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, POLICY_PATH)
    except OSError as exc:
        die(f"could not write policy: {exc}")
    print(json.dumps({
        "status": "ok",
        "wrote": POLICY_PATH,
        "backup": f"{POLICY_PATH}.bak-{ts}",
        "note": "Policy applies on next bot restart (loaded at startup). "
                "Use the Restart control to apply now.",
    }))


def action_service(verb: str):
    if verb not in ALLOWED_SERVICE_VERBS:
        die(f"service verb must be one of {sorted(ALLOWED_SERVICE_VERBS)}")
    try:
        r = subprocess.run(["sudo", "systemctl", verb, SERVICE],
                           capture_output=True, text=True, timeout=40)
    except subprocess.TimeoutExpired:
        die("systemctl timed out")
    if r.returncode != 0:
        die(f"systemctl {verb} failed: {(r.stderr or r.stdout).strip()[:200]}")
    print(json.dumps({"status": "ok", "verb": verb,
                      "service": _systemctl_state()}))


def main():
    if len(sys.argv) < 2:
        die("usage: dune-market-control.py <status|policy-get|policy-set|service [verb]>")
    action = sys.argv[1]
    if action == "status":
        action_status()
    elif action == "listings-search":
        action_listings_search(sys.argv[2] if len(sys.argv) > 2 else "")
    elif action == "policy-get":
        action_policy_get()
    elif action == "policy-set":
        action_policy_set()
    elif action == "service":
        action_service(sys.argv[2] if len(sys.argv) > 2 else "")
    else:
        die(f"unknown action: {action}")


if __name__ == "__main__":
    main()
