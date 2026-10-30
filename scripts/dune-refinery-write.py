#!/usr/bin/env python3
"""Ingot Refinery WRITE writer -- lastsietch-dune-resident psql wrapper.

Deployed to lastsietch-dune:/root/dune-refinery-write.py. Invoked ONLY by the forced-command
dispatcher /root/dune-relay-dispatch.sh via the `refinery-op` token, or directly with
op=exchange + dry_run for read-only QA validation.

A confirmed portal player, LOGGED OUT of the game, hands in base refined ingots plus
Spice Melange and receives the matching Spice-infused metal dust:
  * catalog  (read-only): the rate table shipped beside this writer, plus the resolved
        pawn's per-source holdings (CHOAM bank inv 30, backpack inv 0, toolbar inv 15),
        the bank's slot headroom, and the game-side weekly dust totals. Opens NO write
        session; while the kill switch is off it answers with enabled=false and does not
        touch the database at all, so the page can render "not open yet".
  * exchange (the mutation): consume batches x input_per_batch ingots then
        batches x spice_per_batch Melange from bank, then backpack, then toolbar in that
        fixed order, and mint batches x output_per_batch dust into the CHOAM bank. One
        transaction, one ledger row, idempotent on a caller UUID. dry_run plans and
        validates under the SAME locks and then ROLLBACKs, so a probe can never mutate.

The dusts are loot-only in vanilla -- nothing in the game turns an ingot into one -- so
this is an owner-set service rate, not a bench recipe.

Correctness rules (non-negotiable):
  * OFFLINE-GATE (STOP-SHIP): pawn-side inventories are RAM-backed while the player is
    loaded; deleting or decrementing a row the engine holds in RAM is NOT durable, and an
    online source removal produces real persistent duplication (trace :100-135). The gate
    is the ACCOUNT-keyed form from scripts/lib/dune-take-item.sh:326-361: every live
    encrypted_player_state row for the account is locked FOR SHARE first, Deleted rows are
    excluded, and undetermined (no rows, NULL status, NULL grace) is LOCKED, never allowed.
  * OWNERSHIP (STOP-SHIP): never trust the caller. account_id and owner_ctrl are resolved
    SERVER-SIDE by the admin-backend from the session. The source set is EXACTLY the
    selected pawn's own inventory_type 0 / 15 / 30, reached through the
    encrypted_player_state pawn join. 🔴 inventory_type 0 is ALSO vehicle cargo and base
    container storage; the eps pawn join is what keeps this to the player's own backpack.
    Do not relax that join. No inventory id ever arrives from the caller.
  * CONSERVATION: ingots consumed = batches x input_per_batch, Melange consumed =
    batches x spice_per_batch, dust granted = batches x output_per_batch. Checked against
    what the transaction actually applied, before COMMIT; a mismatch RAISEs
    conservation_failed and the whole thing rolls back. A passing invariant is not proof a
    path is correct (AGENTS.md), which is why this compares applied totals and not intent.
  * CONSUME BEFORE MINT, always, inside one transaction: an insufficient input must never
    be able to leave a minted dust behind.
  * Output lands in the CHOAM bank only. It is pawn-side, so it re-hydrates from the DB at
    login and does not inherit BUG-015 (a placed container does not re-hydrate).
  * Every numeric field is validated as a positive int in Python and then folded into the
    DO block as an integer literal (psql -v cannot reach inside dollar-quoted DO blocks,
    so int() coercion is the injection barrier). Templates come from the rate table, never
    from the caller; the caller's echoed expectations are compared, not trusted.
  * RATE AUTHORITY: refinery-rates.json beside this writer is the ONE rate table, read
    FRESH on every call. A missing or malformed file is refinery_disabled -- never a
    default table. Every exchange echoes the rate it was shown; any mismatch is
    rate_changed and nothing is consumed.
  * WEEKLY CAP: the portal's SQLite counter (keyed on Discord identity) is the primary
    cap. The check here is the game-side BACKSTOP: SUM(dust_units) over the rolling window
    from dune.ls_refinery_exchanges for this account_id and output_template. Keyed on
    account_id, so it is per account and does not see linked alts; that is deliberate, it
    is the second line and not the first.

HARD CONSTRAINTS (mirror dune-storage-write.py):
  * NEVER restart/reboot any game pod, the BGD, or k3s. Only opens a psql session into the
    ALREADY-RUNNING DB pod.
  * Re-validate every field here (defence in depth on top of the dispatcher and the relay
    field whitelist; the relay rebuilds requests and can silently drop a field).

Kill switch: /etc/lastsietch/refinery-enabled. The file gate is the authority -- the dispatcher's
ssh shell inherits no profile, so an env default can never light this (the 2026-08-14
dune-karum-op.sh fail-open incident). While the file is absent:
  * a live exchange refuses with refinery_disabled WITHOUT opening a DB session;
  * catalog answers {ok, enabled:false} from the rate table alone, no DB session;
  * an explicitly requested dry_run exchange still runs and rolls back, because that is
    the round-trip the dark deploy lane proves the shape with. It carries enabled:false
    and dry_run:true, so it can never read as a live success.

Modes:
  --op catalog  --account-id N --owner-ctrl N
  --op exchange --account-id N --owner-ctrl N --output-template T --batches N
                --idempotency-key UUID [--expected-input-template T]
                [--expected-spice-template T] [--expected-input-per-batch N]
                [--expected-spice-per-batch N] [--expected-output-per-batch N]
                [--expected-rate-version V] [--dry-run]
  --stdin-json  read the same fields as one JSON object on stdin (dispatcher path);
                fields {op, account_id, owner_ctrl, output_template?, batches?,
                idempotency_key?, expected_input_template?, expected_spice_template?,
                expected_input_per_batch?, expected_spice_per_batch?,
                expected_output_per_batch?, expected_rate_version?, dry_run?}
"""

import argparse
import json
import os
import re
import subprocess
import sys

# The game-host authority. Portal environment state is advisory only and can never open
# this writer.
REFINERY_FLAG = "/etc/lastsietch/refinery-enabled"

# Pawn-side sources, in the fixed drain order the UI states before the confirm.
INV_BANK = 30
INV_BACKPACK = 0
INV_TOOLBAR = 15
SOURCE_PRIORITY = (("bank", INV_BANK), ("backpack", INV_BACKPACK), ("toolbar", INV_TOOLBAR))

VALID_OPS = ("catalog", "exchange")

# The rate table, shipped beside the writer the way template_volume.json is. Read fresh on
# every call so the owner can retune without a deploy. Absent or malformed => disabled.
_RATES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "refinery-rates.json")

# Pak-derived per-unit volume map {template_id: volume}, the SAME file the storage writer
# reads and ships beside itself. Refresh on each Funcom content update. An absent file is
# an empty map, which makes every volume UNKNOWN -> volume_unverified in the envelope,
# never a hard block: a missing sidecar must not be able to refuse a legitimate trade.
_VMAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "template_volume.json")

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")
# Template ids are CamelCase in dune.items.template_id and compared case-sensitively.
TEMPLATE_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")
RATE_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")

# Stats jsonb for a NEW dust stack. The welcome-pack precedent for plain stackable
# resources (weldingmaterial, Ammo) is an empty object; SolarisCoin's DecayedMaxDurability
# shape is a currency thing. The writer prefers to copy the stats off a live row of the
# same template in the player's own pawn inventories and only falls back to this.
DEFAULT_STATS_JSON = "{}"

DQ = "/root/dq.sh"             # read-only wrapper (resolves the DB pod itself)
DB_PORT = "15432"
DB_USER = "postgres"
DB_NAME = "dune"

# Clean error tokens the relay/portal switch on. parse_psql_error matches these as WHOLE
# WORDS, not as substrings, so a token that is a prefix of another cannot swallow it. That
# is also why there is no bank_full_volume here: a prefix pair like bank_full /
# bank_full_volume is exactly the shape that misroutes, and the slot refusal is no_space.
# Ordering is therefore not load-bearing. APPEND new tokens at the END of this tuple;
# never insert one mid-tuple, because the order is what a reviewer diffs against.
ERROR_TOKENS = (
    "player_online",
    "idempotency_conflict",
    "conservation_failed",
    "insufficient_input",
    "insufficient_spice",
    "no_pawn_storage",
    "no_bank",
    "no_space",
    "weekly_cap",
    "recipe_disabled",
    "refinery_disabled",
    "unknown_recipe",
    "rate_changed",
    "bad_request",
    "write_failed",
    # Appended 2026-09-04 (reviewer M2, position corrected in round 2). Deliberately NOT
    # bank_full_volume: bank_full would be a prefix of that, which is the exact pair the
    # whole-word matching exists to make harmless, and no prefix pair is worth relying on
    # it. APPEND here, at the end, and nowhere else.
    "bank_volume",
)


def emit(obj, code=0):
    print(json.dumps(obj))
    sys.exit(code)


def fail(error, code=1):
    emit({"ok": False, "error": str(error)[:300]}, code)


def pos_int(name, val):
    """Coerce val (int or all-digit str) to a positive int, or fail closed."""
    if isinstance(val, bool):
        fail("bad_request", 2)
    if isinstance(val, int):
        n = val
    elif isinstance(val, str) and re.fullmatch(r"[0-9]+", val.strip()):
        n = int(val.strip())
    else:
        fail("bad_request", 2)
    if n < 1:
        fail("bad_request", 2)
    return n


def refinery_enabled():
    """Fresh stat on every call. The FILE is the switch, never an env default."""
    return os.path.isfile(REFINERY_FLAG)


def _valid_recipe(row):
    if not isinstance(row, dict):
        return False
    for key in ("input_template", "spice_template", "output_template"):
        val = row.get(key)
        if not isinstance(val, str) or not TEMPLATE_RE.fullmatch(val):
            return False
    for key in ("tier", "input_per_batch", "spice_per_batch", "output_per_batch",
                "max_stack"):
        val = row.get(key)
        if isinstance(val, bool) or not isinstance(val, int) or val < 1:
            return False
    return isinstance(row.get("enabled"), bool)


def load_rates():
    """The rate table, read fresh from disk. Returns the validated dict or None.

    None means refinery_disabled: a missing, unreadable or malformed table is a closed
    refinery, NEVER a built-in default. Shipping a fallback rate here would let a truncated
    file trade at a rate nobody set.
    """
    try:
        with open(_RATES_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    version = data.get("rate_version")
    if not isinstance(version, str) or not RATE_VERSION_RE.fullmatch(version):
        return None
    recipes = data.get("recipes")
    if not isinstance(recipes, list) or not recipes:
        return None
    for row in recipes:
        if not _valid_recipe(row):
            return None
    outputs = [r["output_template"] for r in recipes]
    if len(set(outputs)) != len(outputs):
        return None
    for key in ("max_batches_per_request", "weekly_dust_cap", "window_days"):
        val = data.get(key)
        if isinstance(val, bool) or not isinstance(val, int) or val < 1:
            return None
    return data


def load_vmap():
    try:
        with open(_VMAP_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def unit_volume(template, vmap):
    """Per-unit volume for the minted dust, or None when the map does not know it. None is
    the UNVERIFIED branch, not a refusal."""
    if template in vmap:
        try:
            return float(vmap[template])
        except (TypeError, ValueError):
            return None
    return None


def find_recipe(rates, output_template):
    for row in rates["recipes"]:
        if row["output_template"] == output_template:
            return row
    return None


def _dq(sql, timeout=25):
    """Run one read-only SQL through dq.sh (-tAc). Returns stripped stdout or ""."""
    try:
        r = subprocess.run([DQ, "-tAc", sql], capture_output=True,
                           text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


def _read_json(sql):
    raw = _dq(sql)
    if not raw:
        fail("read_failed", 1)
    # psql may echo "SET" before the JSON; take the last non-empty non-"SET" line. Every
    # aggregate below is jsonb, which serialises on ONE line (json_agg does not).
    last = ""
    for line in raw.splitlines():
        line = line.strip()
        if line and line != "SET":
            last = line
    try:
        return json.loads(last)
    except json.JSONDecodeError:
        fail("read_failed", 1)


def resolve_db_pod():
    """Resolve namespace + DB pod by label so a Funcom redeploy that rotates the pod hash
    never writes to a stale pod. Mirrors dune-storage-write.resolve_db_pod."""
    try:
        ns_out = subprocess.run(["sudo", "kubectl", "get", "ns", "-o", "name"],
                                capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        fail("timeout resolving namespace", 3)
    ns = ""
    for line in (ns_out.stdout or "").splitlines():
        name = line.strip().removeprefix("namespace/")
        if name.startswith("funcom-seabass-"):
            ns = name
            break
    if not ns:
        fail("ns_unresolved", 3)
    try:
        pod_out = subprocess.run(["sudo", "kubectl", "get", "pods", "-n", ns, "-o", "name"],
                                 capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        fail("timeout resolving DB pod", 3)
    pod = ""
    for line in (pod_out.stdout or "").splitlines():
        name = line.strip().removeprefix("pod/")
        if name.endswith("-db-dbdepl-sts-0"):
            pod = name
            break
    if not pod:
        fail("db_pod_unresolved", 3)
    return ns, pod


def run_psql(ns, pod, sql, timeout=20):
    """The ONLY place a write DB connection is opened. Uses POSTGRES_PASSWORD inside the pod shell, which execs psql inside the already-running pod. SQL arrives on stdin."""
    try:
        auth_out = subprocess.run(
            ["sudo", "kubectl", "exec", "-n", ns, pod, "--",
             "sh", "-c", 'test -n "${POSTGRES_PASSWORD:-}"'],
            capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        fail("timeout reading POSTGRES_PASSWORD", 3)
    if auth_out.returncode != 0:
        fail("no_pgpass", 3)
    cmd = ["sudo", "kubectl", "exec", "-i", "-n", ns, pod, "--",
           "sh", "-c", '[ -n "${POSTGRES_PASSWORD:-}" ] || { echo "FATAL: POSTGRES_PASSWORD unavailable in DB pod" >&2; exit 3; }; PGPASSWORD=$POSTGRES_PASSWORD exec psql "$@"', "_",
           "-h", "localhost", "-p", DB_PORT, "-U", DB_USER, "-d", DB_NAME,
           "-tA", "-v", "ON_ERROR_STOP=1"]
    try:
        return subprocess.run(cmd, input=sql, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        fail("psql_timeout", 1)


def error_line(stderr):
    """The FIRST line psql reports as ERROR:, lowercased, or "".

    🔴 Scanning the whole stderr blob is not safe. On a syntax failure psql echoes the
    offending DO-block fragment back, and that fragment contains our own RAISE literals, so
    a broken statement would confidently route to player_online or no_space and be reported
    as a clean refusal. The ERROR: line is the only part psql is asserting about what went
    wrong; everything after it (DETAIL, CONTEXT, the echoed source) is evidence, not verdict.
    """
    for line in (stderr or "").splitlines():
        line = line.strip()
        if line.lower().startswith("error:"):
            return line.lower()
    return ""


def parse_psql_error(stderr):
    """Map the ERROR: line onto exactly one clean token, matching whole words. A substring
    scan is first-hit-wins, so any token that is a prefix of another misroutes the refusal;
    the word boundaries close that off regardless of the order the tuple is in."""
    line = error_line(stderr)
    for tok in ERROR_TOKENS:
        if re.search(r"\b" + re.escape(tok) + r"\b", line):
            return tok
    return "write_failed"


# The numbers a refusal carries. The RAISE messages are operator-facing and name
# inventory ids, so the portal never shows them; these two patterns lift out the ONLY
# parts a player-facing sentence needs ("you have N, this needs M", "you have used N of
# your M this week") so lane B does not have to parse a message it is not allowed to
# print.
_HAVE_NEED_RE = re.compile(r"have=(\d+) need=(\d+)")
_CAP_RE = re.compile(r"used=(\d+) add=(\d+) cap=(\d+)")


def refusal_detail(token, stderr):
    blob = error_line(stderr)
    if token in ("insufficient_input", "insufficient_spice"):
        m = _HAVE_NEED_RE.search(blob)
        if m:
            return {"held": int(m.group(1)), "need": int(m.group(2))}
    elif token == "weekly_cap":
        m = _CAP_RE.search(blob)
        if m:
            return {"weekly_dust_used_account": int(m.group(1)),
                    "requested": int(m.group(2)), "weekly_dust_cap": int(m.group(3))}
    return {}


def last_json_line(stdout):
    raw = ""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line in ("SET", "BEGIN", "COMMIT", "ROLLBACK", "CREATE TABLE",
                    "DROP TABLE", "DO"):
            continue
        if line.startswith(("INSERT ", "UPDATE ", "SELECT ", "NOTICE", "PERFORM")):
            continue
        raw = line
    return raw


# ---------------------------------------------------------------------------
# Shared SQL fragments. owner is a validated int and every template comes from the
# rate table (never from the caller), so both fold in as literals safely.
# ---------------------------------------------------------------------------

def pawn_sources_sql(account, owner):
    """EXACTLY the selected pawn's CHOAM bank (30), backpack (0) and toolbar (15).

    🔴 BOTH keys, always. The offline gate keys on account_id and every source resolve
    keys on player_controller_id, so if the two are never bound to each other a mismatched
    pair gates one account and drains another, possibly while that other player is online
    and holding the rows in RAM. Binding them here means a pair that does not belong
    together resolves to NOTHING and the caller is refused, rather than resolving to
    someone else's bank. This is the shape dune-take-item.sh's _take_bank_sql uses.

    🔴 Reached through the encrypted_player_state pawn join and nothing wider:
    inventory_type 0 is also vehicle cargo and base container storage, which hang off a
    vehicle or placeable actor. Deleted characters are excluded here and everywhere else
    this writer reads eps -- an account can hold several eps rows, and a character deleted
    during creation can stay Online forever.
    """
    return f"""
  SELECT inv.id AS inv_id, inv.inventory_type AS itype, inv.max_item_count AS mic
    FROM dune.inventories inv
    JOIN dune.encrypted_player_state eps ON eps.player_pawn_id = inv.actor_id
   WHERE eps.account_id = {account}
     AND eps.player_controller_id = {owner}
     AND eps.character_state IS DISTINCT FROM 'Deleted'
     AND inv.inventory_type IN ({INV_BACKPACK}, {INV_TOOLBAR}, {INV_BANK})
"""


def holdings_after_expr(account, owner, input_tpl, spice_tpl, output_tpl):
    """One self-contained scalar subquery returning the post-trade holdings. Self-contained
    on purpose: the replay branch republishes without resolving anything, and still needs
    to answer with live holdings."""
    return f"""(
    WITH pawn AS ({pawn_sources_sql(account, owner)}    ),
    bank AS (SELECT inv_id, mic FROM pawn WHERE itype = {INV_BANK} LIMIT 1)
    SELECT jsonb_build_object(
      'input', (SELECT COALESCE(SUM(it.stack_size), 0) FROM dune.items it
                  JOIN pawn p ON p.inv_id = it.inventory_id
                 WHERE it.template_id = '{input_tpl}'),
      'spice', (SELECT COALESCE(SUM(it.stack_size), 0) FROM dune.items it
                  JOIN pawn p ON p.inv_id = it.inventory_id
                 WHERE it.template_id = '{spice_tpl}'),
      'output', (SELECT COALESCE(SUM(it.stack_size), 0) FROM dune.items it
                   JOIN bank b ON b.inv_id = it.inventory_id
                  WHERE it.template_id = '{output_tpl}'),
      'bank_free_slots', (SELECT CASE WHEN b.mic IS NULL OR b.mic <= 0 THEN NULL
                                      ELSE b.mic - (SELECT COUNT(*) FROM dune.items it
                                                     WHERE it.inventory_id = b.inv_id)
                                 END FROM bank b))
  )"""


def weekly_used_sql(account, output_tpl, window_days):
    """The game-side weekly backstop window. account is a validated int; window_days comes
    from the validated rate table."""
    return (f"SELECT COALESCE(SUM(dust_units), 0) FROM dune.ls_refinery_exchanges\n"
            f"     WHERE account_id = {account} AND output_template = '{output_tpl}'\n"
            f"       AND created_at >= now() - interval '{window_days} days'")


# ---------------------------------------------------------------------------
# catalog (read-only)
# ---------------------------------------------------------------------------

def read_catalog(account, owner, rates):
    """One read: the account offline state, the resolved pawn sources, the bank's slot
    headroom, per-source holdings for every template in the table, and the game-side
    weekly dust totals. Never opens a write session."""
    templates = set()
    outputs = []
    for row in rates["recipes"]:
        templates.add(row["input_template"])
        templates.add(row["spice_template"])
        templates.add(row["output_template"])
        outputs.append(row["output_template"])
    tpl_list = ", ".join(f"'{t}'" for t in sorted(templates))
    out_list = ", ".join(f"'{t}'" for t in sorted(set(outputs)))
    window = int(rates["window_days"])
    sql = (
        "SET search_path TO dune, public;\n"
        "WITH pawn AS (" + pawn_sources_sql(account, owner) + "),\n"
        f"bank AS (SELECT inv_id, mic FROM pawn WHERE itype = {INV_BANK} LIMIT 1),\n"
        "live AS (SELECT online_status, reconnect_grace_period_end\n"
        "           FROM dune.encrypted_player_state\n"
        f"          WHERE account_id = {account}\n"
        "            AND character_state IS DISTINCT FROM 'Deleted')\n"
        "SELECT jsonb_build_object(\n"
        "  'chars', (SELECT COUNT(*) FROM live),\n"
        "  'offline_chars', (SELECT COUNT(*) FROM live\n"
        "      WHERE online_status = 'Offline'\n"
        "        AND NOT COALESCE(reconnect_grace_period_end > now(), false)),\n"
        f"  'bank_count', (SELECT COUNT(*) FROM pawn WHERE itype = {INV_BANK}),\n"
        f"  'backpack_count', (SELECT COUNT(*) FROM pawn WHERE itype = {INV_BACKPACK}),\n"
        f"  'toolbar_count', (SELECT COUNT(*) FROM pawn WHERE itype = {INV_TOOLBAR}),\n"
        "  'bank_inv', (SELECT inv_id FROM bank),\n"
        "  'bank_mic', (SELECT mic FROM bank),\n"
        "  'bank_used', (SELECT COUNT(*) FROM dune.items\n"
        "      WHERE inventory_id = (SELECT inv_id FROM bank)),\n"
        f"  'backpack_inv', (SELECT inv_id FROM pawn WHERE itype = {INV_BACKPACK} LIMIT 1),\n"
        f"  'toolbar_inv', (SELECT inv_id FROM pawn WHERE itype = {INV_TOOLBAR} LIMIT 1),\n"
        "  'holdings', (SELECT COALESCE(jsonb_agg(jsonb_build_object(\n"
        "        'template_id', h.template_id, 'itype', h.itype, 'units', h.units)),\n"
        "        '[]'::jsonb) FROM (\n"
        "      SELECT it.template_id, p.itype, SUM(it.stack_size)::bigint AS units\n"
        "        FROM dune.items it JOIN pawn p ON p.inv_id = it.inventory_id\n"
        f"       WHERE it.template_id IN ({tpl_list})\n"
        "       GROUP BY it.template_id, p.itype) h),\n"
        "  'bank_dust_stacks', (SELECT COALESCE(jsonb_agg(jsonb_build_object(\n"
        "        'template_id', it.template_id, 'stack_size', it.stack_size)),\n"
        "        '[]'::jsonb)\n"
        "      FROM dune.items it JOIN bank b ON b.inv_id = it.inventory_id\n"
        f"     WHERE it.template_id IN ({out_list})),\n"
        "  'weekly', (SELECT COALESCE(jsonb_agg(jsonb_build_object(\n"
        "        'output_template', w.output_template, 'dust_units', w.dust_units)),\n"
        "        '[]'::jsonb) FROM (\n"
        "      SELECT output_template, COALESCE(SUM(dust_units), 0)::bigint AS dust_units\n"
        "        FROM dune.ls_refinery_exchanges\n"
        f"       WHERE account_id = {account}\n"
        f"         AND output_template IN ({out_list})\n"
        f"         AND created_at >= now() - interval '{window} days'\n"
        "       GROUP BY output_template) w)\n"
        ")"
    )
    return _read_json(sql)


def _pivot_holdings(rows):
    """[{template_id, itype, units}] -> {template: {bank, backpack, toolbar, total}}."""
    by_type = {INV_BANK: "bank", INV_BACKPACK: "backpack", INV_TOOLBAR: "toolbar"}
    out = {}
    for row in rows or []:
        tpl = row.get("template_id")
        kind = by_type.get(row.get("itype"))
        if tpl is None or kind is None:
            continue
        held = out.setdefault(tpl, {"bank": 0, "backpack": 0, "toolbar": 0, "total": 0})
        units = int(row.get("units") or 0)
        held[kind] += units
        held["total"] += units
    return out


def _held(holdings, template):
    return holdings.get(template, {"bank": 0, "backpack": 0, "toolbar": 0, "total": 0})


def do_catalog(account, owner, rates):
    enabled = refinery_enabled()
    caps = {
        "max_batches_per_request": int(rates["max_batches_per_request"]),
        "weekly_dust_cap": int(rates["weekly_dust_cap"]),
        "window_days": int(rates["window_days"]),
    }
    if not enabled:
        # Dark: answer from the rate table alone so the page can render "not open yet".
        # No DB session while the switch is off.
        emit({
            "ok": True, "op": "catalog", "enabled": False,
            "rate_version": rates["rate_version"], "caps": caps,
            "holdings_read": False, "pawn_refusal": None,
            "recipes": [dict(r) for r in rates["recipes"]],
        }, 0)

    plan = read_catalog(account, owner, rates)
    holdings = _pivot_holdings(plan.get("holdings"))
    weekly = {}
    for row in plan.get("weekly") or []:
        weekly[row.get("output_template")] = int(row.get("dust_units") or 0)

    chars = int(plan.get("chars") or 0)
    offline_chars = int(plan.get("offline_chars") or 0)
    offline_ok = chars > 0 and offline_chars == chars

    bank_mic = plan.get("bank_mic")
    bank_used = int(plan.get("bank_used") or 0)
    # Same three cases the mint uses (L3): 0 slots means no room, negative or NULL means no
    # cap, anything else is a real cap. 0 must not read as unbounded on the page when the
    # writer would refuse it as no_space.
    if bank_mic is None or int(bank_mic) < 0:
        bank_free = None                      # no slot cap on this bank
    elif int(bank_mic) == 0:
        bank_free = 0
    else:
        bank_free = max(int(bank_mic) - bank_used, 0)

    # 🔴 Did the pawn resolve the way an exchange requires? A held_* of 0 has two utterly
    # different meanings: "this player holds none" and "we never found the inventories to
    # look in". The sum is 0 either way, so the number cannot tell them apart and a page
    # rendering it says "you hold 0 Copper Ingot" about a character it could not read.
    # This is the shape AGENTS.md warns about: a true answer to a question nobody asked,
    # produced by a working instrument. So holdings_read means "the numbers below are
    # real", and when the pawn does not resolve the numbers are omitted rather than zeroed.
    bank_count = int(plan.get("bank_count") or 0)
    backpack_count = int(plan.get("backpack_count") or 0)
    toolbar_count = int(plan.get("toolbar_count") or 0)
    pawn_ok = bank_count == 1 and backpack_count == 1 and toolbar_count <= 1
    if bank_count != 1:
        pawn_refusal = "no_bank"
    elif not pawn_ok:
        pawn_refusal = "no_pawn_storage"
    else:
        pawn_refusal = None

    bank_stacks = {}
    for row in plan.get("bank_dust_stacks") or []:
        bank_stacks.setdefault(row.get("template_id"), []).append(
            int(row.get("stack_size") or 0))

    recipes = []
    for row in rates["recipes"]:
        held_in = _held(holdings, row["input_template"])
        held_sp = _held(holdings, row["spice_template"])
        held_out = _held(holdings, row["output_template"])
        used = weekly.get(row["output_template"], 0)
        left = max(caps["weekly_dust_cap"] - used, 0)
        # How many more dust units the bank could actually take: the headroom left in the
        # stacks already there, plus a full stack per free slot. None means no slot cap on
        # this bank, which is unbounded rather than zero. Advisory only; the writer
        # recomputes the same thing under locks.
        cap_stack = int(row["max_stack"])
        merge_room = sum(max(cap_stack - n, 0)
                         for n in bank_stacks.get(row["output_template"], []))
        if bank_free is None:
            room = None
        else:
            room = merge_room + bank_free * cap_stack
        limits = [
            held_in["total"] // row["input_per_batch"],
            held_sp["total"] // row["spice_per_batch"],
            left // row["output_per_batch"],
            caps["max_batches_per_request"],
        ]
        if room is not None:
            limits.append(room // row["output_per_batch"])
        entry = dict(row)
        entry.update({
            # null, never 0, when the pawn did not resolve: a reader that coerces null to 0
            # has made its own choice, but it can no longer be handed a fabricated count as
            # though we had counted it.
            "held_input": held_in if pawn_ok else None,
            "held_spice": held_sp if pawn_ok else None,
            "held_output": held_out if pawn_ok else None,
            # Also flat, because held_input and held_spice are objects and a reader that
            # defaults defensively would otherwise render a dict as a count.
            "held_output_units": held_out["total"] if pawn_ok else None,
            "output_room_units": room if pawn_ok else None,
            # Named _account, not weekly_dust_used: this is the GAME-SIDE backstop keyed on
            # account_id. The portal's SQLite counter, keyed on Discord identity, is the
            # primary cap and is the number a player is shown.
            "weekly_dust_used_account": used,
            "weekly_dust_left_account": left,
            "weekly_dust_cap": caps["weekly_dust_cap"],
            "max_batches_affordable": max(min(limits), 0) if pawn_ok else None,
        })
        recipes.append(entry)

    emit({
        "ok": True, "op": "catalog", "enabled": True,
        "rate_version": rates["rate_version"], "caps": caps,
        # False here means the session opened and the pawn did not resolve, NOT that the
        # refinery is dark. enabled says whether the service is open; holdings_read says
        # whether the numbers are real. They are independent.
        "holdings_read": pawn_ok,
        "pawn_refusal": pawn_refusal,
        "online_status": None if chars == 0 else ("Offline" if offline_ok else "Online"),
        "chars": chars, "offline_chars": offline_chars, "offline_ok": offline_ok,
        # reachable is the exchange's own rule restated as a read: exactly one resolved
        # inventory of that type. A toolbar that resolves to none is simply not a source;
        # a bank or backpack that does not resolve to exactly one is what the exchange
        # refuses as no_bank / no_pawn_storage.
        "sources": {
            "bank": {"inv_id": plan.get("bank_inv"),
                     "count": int(plan.get("bank_count") or 0),
                     "reachable": int(plan.get("bank_count") or 0) == 1},
            "backpack": {"inv_id": plan.get("backpack_inv"),
                         "count": int(plan.get("backpack_count") or 0),
                         "reachable": int(plan.get("backpack_count") or 0) == 1},
            "toolbar": {"inv_id": plan.get("toolbar_inv"),
                        "count": int(plan.get("toolbar_count") or 0),
                        "reachable": int(plan.get("toolbar_count") or 0) == 1},
        },
        "bank": {"inv_id": plan.get("bank_inv"), "mic": bank_mic,
                 "used_slots": bank_used, "free_slots": bank_free},
        "recipes": recipes,
    }, 0)


# ---------------------------------------------------------------------------
# exchange (the mutation)
# ---------------------------------------------------------------------------

def consume_block(tag, template, need, token):
    """One template's CONSUME half: lock nothing new (the rows were already locked in id
    order above), gather them in drain order bank -> backpack -> toolbar, then walk the
    partial-stack loop with dune.delete_inventory_item.

    tag suffixes the variables so ingots and Melange cannot collide; token is the refusal
    the caller switches on. need and the inventory ids are validated ints."""
    return f"""
  SELECT array_agg(t.id ORDER BY t.pri, t.id),
         array_agg(t.stack_size ORDER BY t.pri, t.id),
         array_agg(t.pri ORDER BY t.pri, t.id),
         COALESCE(SUM(t.stack_size), 0)
    INTO v_ids_{tag}, v_stacks_{tag}, v_pris_{tag}, v_avail_{tag}
    FROM (
      SELECT it.id, it.stack_size, s.pri
        FROM dune.items it
        JOIN (VALUES (v_bank_inv, 0), (v_backpack_inv, 1), (v_toolbar_inv, 2))
             AS s(inv_id, pri) ON s.inv_id = it.inventory_id
       -- The inventory_id predicate is redundant with the join and stated anyway: it is
       -- the indexed one, and dune.items is far too large to risk a plan that filters on
       -- template_id first while this transaction holds locks.
       WHERE it.inventory_id IN (v_bank_inv, v_backpack_inv, v_toolbar_inv)
         AND it.template_id = '{template}'
    ) t;
  IF COALESCE(v_avail_{tag}, 0) < {need} THEN
    RAISE EXCEPTION '{token} (have=% need=%)', COALESCE(v_avail_{tag}, 0), {need};
  END IF;
  v_remaining := {need};
  FOR v_idx IN 1 .. COALESCE(array_length(v_ids_{tag}, 1), 0) LOOP
    EXIT WHEN v_remaining <= 0;
    v_take := LEAST(v_stacks_{tag}[v_idx], v_remaining);
    IF v_take > 0 THEN
      PERFORM dune.delete_inventory_item(v_ids_{tag}[v_idx], v_take);
      v_remaining := v_remaining - v_take;
      v_used_{tag} := v_used_{tag} + v_take;
      IF v_pris_{tag}[v_idx] = 0 THEN
        v_bank_{tag} := v_bank_{tag} + v_take;
      ELSIF v_pris_{tag}[v_idx] = 1 THEN
        v_back_{tag} := v_back_{tag} + v_take;
      ELSE
        v_tool_{tag} := v_tool_{tag} + v_take;
      END IF;
    END IF;
  END LOOP;
  IF v_remaining > 0 THEN
    RAISE EXCEPTION '{token} (short by % after draining every source)', v_remaining;
  END IF;
"""


def build_exchange_sql(account, owner, recipe, batches, idempotency_key, rates, dry_run,
                       unit_vol=None):
    """One idempotent transaction: replay, offline gate, weekly backstop, source resolve,
    consume ingots then Melange, mint the dust into the bank, ledger row, conservation
    check. dry_run runs the identical plan under the identical locks and ROLLBACKs."""
    input_tpl = recipe["input_template"]
    spice_tpl = recipe["spice_template"]
    output_tpl = recipe["output_template"]
    input_units = batches * int(recipe["input_per_batch"])
    spice_units = batches * int(recipe["spice_per_batch"])
    dust_units = batches * int(recipe["output_per_batch"])
    stack_max = int(recipe["max_stack"])
    tier = int(recipe["tier"])
    cap = int(rates["weekly_dust_cap"])
    window = int(rates["window_days"])
    version = rates["rate_version"]
    finish = "ROLLBACK;" if dry_run else "COMMIT;"
    dry_lit = "true" if dry_run else "false"
    enabled_lit = "true" if refinery_enabled() else "false"
    unit_vol_lit = "NULL" if unit_vol is None else repr(float(unit_vol))

    consumed_expr = (
        "jsonb_build_object(\n"
        f"      '{input_tpl}', jsonb_build_object('units', v_used_in, 'from',\n"
        "          jsonb_build_object('bank', v_bank_in, 'backpack', v_back_in,\n"
        "                             'toolbar', v_tool_in)),\n"
        f"      '{spice_tpl}', jsonb_build_object('units', v_used_sp, 'from',\n"
        "          jsonb_build_object('bank', v_bank_sp, 'backpack', v_back_sp,\n"
        "                             'toolbar', v_tool_sp)))")

    return f"""\\set ON_ERROR_STOP on
SET search_path TO dune, public;
BEGIN;
DROP TABLE IF EXISTS _rf_result;
CREATE TEMP TABLE _rf_result(
  output_template text, tier int, batches bigint, dust_units bigint,
  input_units bigint, spice_units bigint,
  consumed jsonb, granted jsonb, holdings_after jsonb,
  weekly_dust_used bigint, weekly_dust_cap bigint, volume_unverified bool, replay bool
) ON COMMIT PRESERVE ROWS;
DO $refinery$
DECLARE
  v_e_account bigint; v_e_owner bigint; v_e_out text; v_e_tier int;
  v_e_batches bigint; v_e_dust bigint; v_e_in_tpl text;
  v_e_in_units bigint; v_e_sp_units bigint;
  v_e_consumed jsonb; v_e_granted jsonb;
  v_chars int; v_offline int;
  v_week bigint; v_week_after bigint;
  v_bank_count int; v_backpack_count int; v_toolbar_count int;
  v_bank_inv bigint; v_backpack_inv bigint; v_toolbar_inv bigint; v_bank_mic bigint;
  v_ids_in bigint[]; v_stacks_in bigint[]; v_pris_in int[]; v_avail_in bigint;
  v_ids_sp bigint[]; v_stacks_sp bigint[]; v_pris_sp int[]; v_avail_sp bigint;
  v_used_in bigint := 0; v_bank_in bigint := 0; v_back_in bigint := 0; v_tool_in bigint := 0;
  v_used_sp bigint := 0; v_bank_sp bigint := 0; v_back_sp bigint := 0; v_tool_sp bigint := 0;
  v_remaining bigint; v_take bigint; v_idx int;
  v_stats jsonb; v_dust_id bigint; v_dust_stack bigint; v_dust_pos bigint;
  v_slot bigint; v_mint_left bigint; v_minted bigint := 0; v_guard int := 0;
  v_stacks jsonb := '[]'::jsonb;
  v_bank_miv double precision; v_used_vol double precision;
  v_unit_vol double precision := {unit_vol_lit};
  v_mint_vol double precision; v_vol_unverified bool := false;
  v_e_vol bool;
BEGIN
  -- LOCK ORDER, followed exactly below and never reordered. Every refining transaction
  -- takes the same locks in the same sequence, which is what makes two of them serialise
  -- instead of deadlocking:
  --   1. the idempotency uuid           (advisory xact lock)
  --   2. the ledger replay row          (FOR UPDATE)
  --   3. the account's live eps rows    (FOR SHARE, the offline gate)
  --   4. the three pawn inventory rows  (FOR UPDATE, ascending id)
  --   5. the ingot item rows            (FOR UPDATE, ascending id)
  --   6. the melange item rows          (FOR UPDATE, ascending id)
  --   7. the dust item rows             (FOR UPDATE, taken by the mint, last of all)
  -- and the work follows the same order: consume ingots, then melange, then mint, then
  -- the ledger insert and the conservation check, both before COMMIT.
  --
  -- The weekly cap is read AFTER step 4, because the inventory lock is what serialises two
  -- requests carrying different uuids; a cap read above it sees a stale total.
  --
  -- 0. Serialize identical UUIDs for the length of this transaction, so a retry that
  --    arrives while the first attempt is still open waits instead of racing it.
  PERFORM pg_advisory_xact_lock(hashtextextended('{idempotency_key}', 0));

  -- 1. REPLAY: an already-applied intent republishes its original result and touches NO
  --    inventory. Any changed parameter under the same key is a conflict, never a second
  --    trade.
  SELECT account_id, owner_ctrl, output_template, tier, batches, dust_units,
         input_template, input_units, spice_units, consumed_json, granted_json,
         volume_unverified
    INTO v_e_account, v_e_owner, v_e_out, v_e_tier, v_e_batches, v_e_dust,
         v_e_in_tpl, v_e_in_units, v_e_sp_units, v_e_consumed, v_e_granted,
         v_e_vol
    FROM dune.ls_refinery_exchanges
   WHERE idempotency_key = '{idempotency_key}'::uuid
   FOR UPDATE;
  IF FOUND THEN
    IF v_e_account <> {account}
       OR v_e_owner <> {owner}
       OR v_e_out IS DISTINCT FROM '{output_tpl}'
       OR v_e_batches <> {batches}
       OR v_e_dust <> {dust_units}
       OR v_e_in_tpl IS DISTINCT FROM '{input_tpl}' THEN
      RAISE EXCEPTION 'idempotency_conflict';
    END IF;
    SELECT ({weekly_used_sql(account, output_tpl, window)}) INTO v_week_after;
    INSERT INTO _rf_result VALUES (
      v_e_out, v_e_tier, v_e_batches, v_e_dust, v_e_in_units, v_e_sp_units,
      v_e_consumed, v_e_granted,
      {holdings_after_expr(account, owner, input_tpl, spice_tpl, output_tpl)},
      v_week_after, {cap}, COALESCE(v_e_vol, false), true);
    RETURN;
  END IF;

  -- 2. OFFLINE GATE, ACCOUNT-keyed, under a row lock, fail-closed.
  --    Locked FOR SHARE first so a login cannot flip online_status between the check and
  --    the consume. It blocks the engine's own login write to these rows for the length of
  --    this transaction; a deadlock against the engine rolls US back, the safe direction.
  --    Deleted rows are excluded: an account can hold several eps rows, and a character
  --    deleted during creation can stay Online forever.
  PERFORM 1 FROM dune.encrypted_player_state
   WHERE account_id = {account}
     AND character_state IS DISTINCT FROM 'Deleted'
     FOR SHARE;
  SELECT count(*),
         count(*) FILTER (
           WHERE online_status = 'Offline'
             AND NOT COALESCE(reconnect_grace_period_end > now(), false))
    INTO v_chars, v_offline
    FROM dune.encrypted_player_state
   WHERE account_id = {account}
     AND character_state IS DISTINCT FROM 'Deleted';
  -- No rows, a NULL status, or a NULL character_state all land here: undetermined is
  -- LOCKED, never allowed.
  IF v_chars = 0 THEN
    RAISE EXCEPTION 'player_online (account % has no live character row, status undetermined)', {account};
  END IF;
  IF v_offline <> v_chars THEN
    RAISE EXCEPTION 'player_online (% of % characters on account % are online or in reconnect grace)',
                    v_chars - v_offline, v_chars, {account};
  END IF;

  -- 3. SOURCES: exactly the selected pawn's bank, backpack and toolbar. Ambiguity is
  --    refused rather than resolved with LIMIT 1 -- an account can present more than one
  --    character slot, and picking one of them silently is how the wrong bank gets drained.
  SELECT COUNT(*) FILTER (WHERE inv.inventory_type = {INV_BANK}),
         COUNT(*) FILTER (WHERE inv.inventory_type = {INV_BACKPACK}),
         COUNT(*) FILTER (WHERE inv.inventory_type = {INV_TOOLBAR}),
         MIN(inv.id) FILTER (WHERE inv.inventory_type = {INV_BANK}),
         MIN(inv.id) FILTER (WHERE inv.inventory_type = {INV_BACKPACK}),
         MIN(inv.id) FILTER (WHERE inv.inventory_type = {INV_TOOLBAR})
    INTO v_bank_count, v_backpack_count, v_toolbar_count,
         v_bank_inv, v_backpack_inv, v_toolbar_inv
    FROM dune.inventories inv
    JOIN dune.encrypted_player_state eps ON eps.player_pawn_id = inv.actor_id
   WHERE eps.account_id = {account}
     AND eps.player_controller_id = {owner}
     AND eps.character_state IS DISTINCT FROM 'Deleted'
     AND inv.inventory_type IN ({INV_BACKPACK}, {INV_TOOLBAR}, {INV_BANK});
  -- Both keys were required above, so these refusals also cover the mismatched pair: a
  -- controller that does not belong to this account resolves to zero inventories and is
  -- refused here, instead of gating account % and draining someone else.
  IF v_bank_count <> 1 THEN RAISE EXCEPTION 'no_bank (bank inventories resolved: % for account % ctrl %)', v_bank_count, {account}, {owner}; END IF;
  IF v_backpack_count <> 1 OR v_toolbar_count > 1 THEN
    RAISE EXCEPTION 'no_pawn_storage (backpack=% toolbar=% for account % ctrl %)', v_backpack_count, v_toolbar_count, {account}, {owner};
  END IF;
  SELECT max_item_count INTO v_bank_mic FROM dune.inventories WHERE id = v_bank_inv;

  -- 4. Take lock steps 4, 5 and 6 in that order: the three inventory rows by ascending id,
  --    then the ingot rows by ascending id, then the melange rows by ascending id. The
  --    dust rows are deliberately NOT locked here -- the mint takes them last, so every
  --    transaction agrees that dust comes after melange.
  PERFORM 1 FROM dune.inventories
   WHERE id IN (v_bank_inv, v_backpack_inv, v_toolbar_inv)
   ORDER BY id FOR UPDATE;
  PERFORM it.id FROM dune.items it
   WHERE it.inventory_id IN (v_bank_inv, v_backpack_inv, v_toolbar_inv)
     AND it.template_id = '{input_tpl}'
   ORDER BY it.id FOR UPDATE;
  PERFORM it.id FROM dune.items it
   WHERE it.inventory_id IN (v_bank_inv, v_backpack_inv, v_toolbar_inv)
     AND it.template_id = '{spice_tpl}'
   ORDER BY it.id FOR UPDATE;

  -- 5. WEEKLY CAP backstop (game side, keyed on account_id). The portal's SQLite counter
  --    keyed on Discord identity is the primary cap; this is the second line.
  --
  --    🔴 Read AFTER the inventory lock above, never before it. Two requests with
  --    different uuids do not serialise on the advisory lock, so read first and both see
  --    the same total, both decide there is room, and both mint: the cap is breached by
  --    exactly the concurrency it exists to bound. Below the lock, the second waits for
  --    the first to commit and then reads a total that already includes it.
  SELECT ({weekly_used_sql(account, output_tpl, window)}) INTO v_week;
  IF v_week + {dust_units} > {cap} THEN
    RAISE EXCEPTION 'weekly_cap (used=% add=% cap=%)', v_week, {dust_units}, {cap};
  END IF;

  -- 6. CONSUME the ingots, then the Melange. Both before anything is minted: an
  --    insufficient input must never be able to leave a dust behind.
{consume_block('in', input_tpl, input_units, 'insufficient_input')}
{consume_block('sp', spice_tpl, spice_units, 'insufficient_spice')}
  -- 7. MINT the dust into the CHOAM bank: merge into a stack under the cap first, then
  --    spill into the first empty slot. Bank max_item_count -1/0/NULL means no slot cap
  --    (the bank's cap varies and may be either), matching the storage writer's mint.
  -- Scoped to the bank and ORDERed, so two runs on the same bank copy the SAME row's
  -- stats. Unscoped and unordered this was a coin flip between inventories.
  SELECT it.stats INTO v_stats
    FROM dune.items it
   WHERE it.inventory_id = v_bank_inv
     AND it.template_id = '{output_tpl}'
   ORDER BY it.id ASC
   LIMIT 1;
  IF v_stats IS NULL THEN v_stats := '{DEFAULT_STATS_JSON}'::jsonb; END IF;

  -- 7a. BANK VOLUME GATE, the same shape the storage pawn-move enforces. Checked ONCE for
  --     the whole minted amount before any of it is written, because the trade is
  --     all-or-nothing. Three outcomes, and the two permissive ones matter as much as the
  --     refusal: a bank with no positive volume cap is not volume-gated at all, and an
  --     UNKNOWN unit volume (template absent from template_volume.json, or the sidecar
  --     missing entirely) sets volume_unverified and ALLOWS. A missing sidecar must never
  --     be able to refuse a legitimate trade; it only means we could not prove the fit.
  SELECT max_item_volume INTO v_bank_miv FROM dune.inventories WHERE id = v_bank_inv;
  IF COALESCE(v_bank_miv, 0) > 0 THEN
    IF v_unit_vol IS NULL THEN
      v_vol_unverified := true;
    ELSE
      SELECT COALESCE(SUM(COALESCE(volume_override, 0) * stack_size), 0)
        INTO v_used_vol
        FROM dune.items WHERE inventory_id = v_bank_inv;
      v_mint_vol := v_unit_vol * {dust_units};
      IF v_used_vol + v_mint_vol > v_bank_miv THEN
        RAISE EXCEPTION 'bank_volume (used=% adding=% cap=%)',
                        v_used_vol, v_mint_vol, v_bank_miv;
      END IF;
    END IF;
  END IF;
  -- The mint is a LOOP from the start. One request cannot exceed 50 dust at today's
  -- max_batches_per_request, so a single merge or a single insert covers every case that
  -- exists right now -- which is exactly why the loop is here: the day the owner retunes
  -- the cap past a stack, straight-line code would silently mint one stack and lose the
  -- rest. Each pass mints at least one unit, so the guard is the arithmetic bound and can
  -- never trip on a request that is genuinely still making progress.
  v_mint_left := {dust_units};
  WHILE v_mint_left > 0 LOOP
    v_guard := v_guard + 1;
    IF v_guard > {dust_units} + 2 THEN
      RAISE EXCEPTION 'no_space (the mint loop did not converge on % dust)', {dust_units};
    END IF;
    SELECT it.id, it.stack_size, it.position_index
      INTO v_dust_id, v_dust_stack, v_dust_pos
      FROM dune.items it
     WHERE it.inventory_id = v_bank_inv
       AND it.template_id = '{output_tpl}'
       AND it.quality_level = 0
       AND it.stack_size < {stack_max}
     ORDER BY it.position_index ASC, it.id ASC
     LIMIT 1 FOR UPDATE;
    IF FOUND THEN
      v_take := LEAST({stack_max} - v_dust_stack, v_mint_left);
      UPDATE dune.items SET stack_size = stack_size + v_take WHERE id = v_dust_id;
      v_stacks := v_stacks || jsonb_build_array(jsonb_build_object(
        'item_id', v_dust_id, 'slot', v_dust_pos,
        'stack_size', v_dust_stack + v_take, 'merged', true));
    ELSE
      -- Ruled from live data 2026-09-04: every bank on this server reports
      -- max_item_count 500, toolbars 8, backpacks 35 to 150, and none reports 0 or -1. So
      -- 0 is not "uncapped" here, it is a bank with nowhere to put anything, and it is
      -- split out the pawn-move way rather than folded in with the negative case.
      IF v_bank_mic = 0 THEN
        RAISE EXCEPTION 'no_space (the CHOAM bank reports zero slots)';
      ELSIF v_bank_mic IS NULL OR v_bank_mic < 0 THEN
        SELECT COALESCE(MAX(position_index) + 1, 0) INTO v_slot
          FROM dune.items WHERE inventory_id = v_bank_inv;
      ELSE
        SELECT MIN(s.idx) INTO v_slot
          FROM generate_series(0, v_bank_mic - 1) AS s(idx)
         WHERE NOT EXISTS (SELECT 1 FROM dune.items
                            WHERE inventory_id = v_bank_inv AND position_index = s.idx);
      END IF;
      IF v_slot IS NULL THEN
        RAISE EXCEPTION 'no_space (the CHOAM bank has no free slot for the dust)';
      END IF;
      v_take := LEAST({stack_max}, v_mint_left);
      INSERT INTO dune.items
        (inventory_id, stack_size, position_index, template_id, stats,
         quality_level, acquisition_time, is_new)
      VALUES
        (v_bank_inv, v_take, v_slot, '{output_tpl}', v_stats, 0, 0, true)
      RETURNING id INTO v_dust_id;
      v_stacks := v_stacks || jsonb_build_array(jsonb_build_object(
        'item_id', v_dust_id, 'slot', v_slot,
        'stack_size', v_take, 'merged', false));
    END IF;
    v_mint_left := v_mint_left - v_take;
    v_minted := v_minted + v_take;
  END LOOP;

  -- 8. LEDGER in the same transaction: idempotency, audit and the weekly window all read
  --    from this one row, so it can never be written by a second statement that might not
  --    run.
  INSERT INTO dune.ls_refinery_exchanges (
    idempotency_key, account_id, owner_ctrl, tier, output_template, batches,
    dust_units, input_template, input_units, spice_units,
    consumed_json, granted_json, rate_version, volume_unverified
  ) VALUES (
    '{idempotency_key}'::uuid, {account}, {owner}, {tier}, '{output_tpl}', {batches},
    v_minted, '{input_tpl}', v_used_in, v_used_sp,
    {consumed_expr},
    jsonb_build_object('template', '{output_tpl}', 'units', v_minted, 'stacks', v_stacks),
    '{version}', v_vol_unverified
  );

  -- 9. CONSERVATION, before COMMIT, against what the transaction ACTUALLY applied rather
  --    than against what it intended. A passing invariant is not proof a path is correct,
  --    so this compares the accumulated deletes and mints to the ruled rate.
  IF v_used_in <> {input_units} OR v_used_sp <> {spice_units} OR v_minted <> {dust_units} THEN
    RAISE EXCEPTION 'conservation_failed (ingots=%/% spice=%/% dust=%/%)',
                    v_used_in, {input_units}, v_used_sp, {spice_units}, v_minted, {dust_units};
  END IF;

  SELECT ({weekly_used_sql(account, output_tpl, window)}) INTO v_week_after;
  INSERT INTO _rf_result VALUES (
    '{output_tpl}', {tier}, {batches}, v_minted, v_used_in, v_used_sp,
    {consumed_expr},
    jsonb_build_object('template', '{output_tpl}', 'units', v_minted, 'stacks', v_stacks),
    {holdings_after_expr(account, owner, input_tpl, spice_tpl, output_tpl)},
    v_week_after, {cap}, v_vol_unverified, false);
END $refinery$;
SELECT jsonb_build_object(
  'ok', true, 'op', 'exchange', 'dry_run', {dry_lit},
  'enabled', {enabled_lit},
  'rate_version', '{version}',
  'output_template', output_template, 'tier', tier, 'batches', batches,
  'dust_units', dust_units, 'input_units', input_units, 'spice_units', spice_units,
  'consumed', consumed, 'granted', granted, 'holdings_after', holdings_after,
  'weekly_dust_used_account', weekly_dust_used, 'weekly_dust_cap', weekly_dust_cap,
  'volume_unverified', volume_unverified, 'replay', replay
) FROM _rf_result;
{finish}
"""


def do_exchange(account, owner, rates, output_template, batches, idempotency_key,
                dry_run):
    recipe = find_recipe(rates, output_template)
    if recipe is None:
        fail("unknown_recipe", 1)
    if not recipe["enabled"]:
        emit({"ok": False, "error": "recipe_disabled",
              "output_template": output_template}, 0)
    if batches > int(rates["max_batches_per_request"]):
        fail("bad_request", 2)
    ns, pod = resolve_db_pod()
    unit_vol = unit_volume(recipe["output_template"], load_vmap())
    sql = build_exchange_sql(account, owner, recipe, batches, idempotency_key, rates,
                             dry_run, unit_vol)
    out = run_psql(ns, pod, sql, timeout=30)
    if out.returncode != 0:
        blob = out.stderr or out.stdout
        token = parse_psql_error(blob)
        emit({"ok": False, "error": token, **refusal_detail(token, blob)}, 1)
    raw = last_json_line(out.stdout)
    if not raw:
        fail("write_failed", 1)
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        fail("write_failed", 1)
    emit(result, 0)


# ---------------------------------------------------------------------------
# Argument handling. Every field is re-validated here, on top of the dispatcher gate
# and the relay's field whitelist -- a whitelist-rebuild transport fails OPEN toward
# the destructive default, so this layer cannot assume anything arrived intact.
# ---------------------------------------------------------------------------

def _template_arg(val):
    if val is None:
        return None
    val = str(val).strip()
    if not val:
        return None
    if not TEMPLATE_RE.fullmatch(val):
        fail("bad_request", 2)
    return val


def gather_args():
    # Which path the job arrived on. The relay/dispatcher path is --stdin-json; the CLI
    # path is hand-driven ops and QA. They differ in exactly one place: the rate-version
    # guard below is mandatory on the wire and optional by hand.
    via_stdin = "--stdin-json" in sys.argv[1:]
    if via_stdin:
        try:
            payload = json.loads(sys.stdin.read())
        except (ValueError, json.JSONDecodeError):
            fail("bad_request", 2)
        if not isinstance(payload, dict):
            fail("bad_request", 2)
        req = {
            "op": payload.get("op"),
            "account_id": payload.get("account_id"),
            "owner_ctrl": payload.get("owner_ctrl"),
            "output_template": payload.get("output_template"),
            "batches": payload.get("batches"),
            "idempotency_key": payload.get("idempotency_key"),
            "uuid": payload.get("uuid"),
            "expected_input_template": payload.get("expected_input_template"),
            "expected_spice_template": payload.get("expected_spice_template"),
            "expected_input_per_batch": payload.get("expected_input_per_batch"),
            "expected_spice_per_batch": payload.get("expected_spice_per_batch"),
            "expected_output_per_batch": payload.get("expected_output_per_batch"),
            "expected_rate_version": payload.get("expected_rate_version"),
            "dry_run": bool(payload.get("dry_run", False)),
        }
    else:
        ap = argparse.ArgumentParser(description="Ingot Refinery catalog/exchange writer")
        ap.add_argument("--op", required=True, choices=list(VALID_OPS))
        ap.add_argument("--account-id", required=True)
        ap.add_argument("--owner-ctrl", required=True)
        ap.add_argument("--output-template", default=None)
        ap.add_argument("--batches", default=None)
        ap.add_argument("--idempotency-key", default=None)
        ap.add_argument("--expected-input-template", default=None)
        ap.add_argument("--expected-spice-template", default=None)
        ap.add_argument("--expected-input-per-batch", default=None)
        ap.add_argument("--expected-spice-per-batch", default=None)
        ap.add_argument("--expected-output-per-batch", default=None)
        ap.add_argument("--expected-rate-version", default=None)
        ap.add_argument("--dry-run", action="store_true")
        a = ap.parse_args()
        req = {
            "op": a.op,
            "account_id": a.account_id,
            "owner_ctrl": a.owner_ctrl,
            "output_template": a.output_template,
            "batches": a.batches,
            "idempotency_key": a.idempotency_key,
            "uuid": None,
            "expected_input_template": a.expected_input_template,
            "expected_spice_template": a.expected_spice_template,
            "expected_input_per_batch": a.expected_input_per_batch,
            "expected_spice_per_batch": a.expected_spice_per_batch,
            "expected_output_per_batch": a.expected_output_per_batch,
            "expected_rate_version": a.expected_rate_version,
            "dry_run": a.dry_run,
        }

    if req["op"] not in VALID_OPS:
        fail("bad_request", 2)
    if req["op"] != "exchange":
        req.pop("uuid", None)
    req["account_id"] = pos_int("account_id", req["account_id"])
    req["owner_ctrl"] = pos_int("owner_ctrl", req["owner_ctrl"])

    if req["op"] == "exchange":
        req["output_template"] = _template_arg(req["output_template"])
        if req["output_template"] is None:
            fail("bad_request", 2)
        req["batches"] = pos_int("batches", req["batches"])
        # The relay whitelist calls this field `uuid` (the plan's request shape); the
        # ledger column and the CLI flag call it idempotency_key. Both names are accepted
        # so neither side can be wrong, and two DIFFERENT values under the two names fail
        # closed rather than letting one of them silently win.
        wire = req.pop("uuid", None)
        named = req["idempotency_key"]
        if wire is not None and str(wire).strip() != "":
            wire = str(wire).strip().lower()
            if named is not None and str(named).strip() != "" \
                    and str(named).strip().lower() != wire:
                fail("bad_request", 2)
            named = wire
        uuid_key = str(named or "").strip().lower()
        if not UUID_RE.fullmatch(uuid_key):
            fail("bad_request", 2)
        req["idempotency_key"] = uuid_key
        req["expected_input_template"] = _template_arg(req["expected_input_template"])
        req["expected_spice_template"] = _template_arg(req["expected_spice_template"])
        for field in ("expected_input_per_batch", "expected_spice_per_batch",
                      "expected_output_per_batch"):
            if req[field] is not None and str(req[field]).strip() != "":
                req[field] = pos_int(field, req[field])
            else:
                req[field] = None
        version = req["expected_rate_version"]
        if version is not None and str(version).strip() != "":
            version = str(version).strip()
            if not RATE_VERSION_RE.fullmatch(version):
                fail("bad_request", 2)
            req["expected_rate_version"] = version
        elif via_stdin:
            # 🔴 REQUIRED over the wire, dry runs included. Optional it fails OPEN: a field
            # dropped in transit is indistinguishable from one never sent, so the whole
            # rate-table guard would silently not run and the trade would go through
            # unchecked. That is the 2026-08-14 dropped-dry_run shape one layer in, and a
            # guard is at its worst when it is invisible exactly as it fails. Absent here
            # is a loud bad_request instead.
            #
            # It is the ONLY guard for a retune that moves max_stack, weekly_dust_cap or
            # window_days: the five expected_* fields pin what a batch costs, and every one
            # of them still matches across those three changes.
            #
            # Still optional on the CLI path, so hand-driven QA and the ops dry-run smoke
            # can run without first reading a catalog.
            #
            # This ONE refusal names the field it wanted. Every other bad_request stays
            # bare, so the discriminator means exactly one thing: the field never arrived.
            # Without it the host lane cannot tell "the deployed relay strips this" from
            # any other malformed job, because every bad_request is otherwise byte
            # identical. The name is already public in the plan, so it leaks nothing, and
            # the relay returns this JSON verbatim.
            emit({"ok": False, "error": "bad_request",
                  "missing": "expected_rate_version"}, 2)
        else:
            req["expected_rate_version"] = None
    return req


def check_rate_echo(req, rates, recipe):
    """The caller echoes the rate it was shown. Anything that no longer matches the live
    table is rate_changed and nothing is consumed -- the owner can retune mid-session and a
    stale page must not trade at the old rate."""
    if req["expected_rate_version"] is not None \
            and req["expected_rate_version"] != rates["rate_version"]:
        return True
    pairs = (
        (req["expected_input_template"], recipe["input_template"]),
        (req["expected_spice_template"], recipe["spice_template"]),
        (req["expected_input_per_batch"], recipe["input_per_batch"]),
        (req["expected_spice_per_batch"], recipe["spice_per_batch"]),
        (req["expected_output_per_batch"], recipe["output_per_batch"]),
    )
    return any(want is not None and want != live for want, live in pairs)


def main():
    req = gather_args()
    rates = load_rates()
    if rates is None:
        # A missing or malformed rate table is a closed refinery, for catalog too: with no
        # table there is nothing to render and nothing to trade at.
        emit({"ok": False, "error": "refinery_disabled"}, 0)

    if req["op"] == "catalog":
        do_catalog(req["account_id"], req["owner_ctrl"], rates)

    if not req["dry_run"] and not refinery_enabled():
        # Kill-switch off: the FIRST refusal, before the rate table is consulted at all.
        # Refuses WITHOUT opening a DB session, and without telling a caller which
        # templates exist: answering unknown_recipe for one output and rate_changed for
        # another would let a dark probe enumerate the rate table one guess at a time.
        # While dark every live exchange gets the same three words. A dry run is still
        # allowed past here: it rolls back, and it is the round trip the dark deploy lane
        # proves the shape with.
        emit({"ok": False, "error": "refinery_disabled"}, 0)
    recipe = find_recipe(rates, req["output_template"])
    if recipe is None:
        fail("unknown_recipe", 1)
    if check_rate_echo(req, rates, recipe):
        fail("rate_changed", 1)
    do_exchange(req["account_id"], req["owner_ctrl"], rates, req["output_template"],
                req["batches"], req["idempotency_key"], req["dry_run"])


if __name__ == "__main__":
    main()
