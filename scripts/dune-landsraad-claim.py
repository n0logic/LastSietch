#!/usr/bin/env python3
"""Portal Landsraad CLAIM writer -- lastsietch-dune-resident psql wrapper.

Deployed to lastsietch-dune:/root/dune-landsraad-claim.py. Invoked ONLY by the forced-command
dispatcher /root/dune-relay-dispatch.sh via the `landsraad-claim` token, or directly
with --print-sql for the throwaway-postgres suite.

A portal player claims every unclaimed Landsraad house reward at once, WHILE OFFLINE,
instead of walking to each house representative.

One transaction per claim:
  * the ledger is decremented by the game's own landsraad_withdraw_house_reward proc,
    so validation and the pg_notify the server listens on are exactly an in-game
    claim's, and the reps stop offering what was taken;
  * items go into free backpack slots first (SLOT_SAFETY slots are left empty);
  * what does not fit becomes a CHOAM Exchange Completed order (same rows as
    /opt/lastsietch-rmq-bridge/deliver-claim.py) the player takes at any terminal;
  * swatches are cosmetic unlocks, appended to the pawn's unlock list (behind their own
    flag until the reward -> unlock id mapping is confirmed against a real rep claim);
  * anything that fits nowhere is NOT withdrawn and stays at the rep.
Any RAISE rolls the whole claim back, so a failure leaves ledger, backpack, exchange and
pawn exactly as they were.

mode=preview runs the identical transaction and ROLLS BACK, so the plan the player is
shown is the plan the writer would execute. It is not offline-gated (rolled back,
nothing persists) and reports `online` so the portal can say "log out first".

Correctness rules (non-negotiable):
  * OFFLINE-GATE (STOP-SHIP): backpack and pawn jsonb are RAM-resident while online,
    and the pawn stays live in its partition through reconnect grace and logoff
    persistence after a logout. An apply refuses unless EVERY character row on the
    account is Offline and past both timers (UTC), checked at the start under a row
    lock on those rows AND again at the end of the transaction.
  * OWNERSHIP: owner_ctrl is resolved SERVER-SIDE by the admin-backend; the writer only
    ever touches that controller's ledger rows, its own pawn's backpack and unlock list.
  * Every reward is q0: no Landsraad reward structure carries a grade (spec §2).
  * STACKS never exceed the PAK max (dune-item-pak-meta.json, deployed beside this
    script) nor the largest stack seen live. A template the catalog does not know is
    not treated as an item: it stays at the rep.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

# Gate files: absent or empty = nobody; account ids one per line = allowlist; a line
# holding just '*' = everyone. landsraad-claim-enabled is also on the admin flag-set
# allowlist, whose "on" is a bare touch: that can only ever create an empty file, so
# the panel can close the claim but never open it.
ENABLED_FLAG = "/etc/lastsietch/landsraad-claim-enabled"
SWATCH_FLAG = "/etc/lastsietch/landsraad-claim-swatches-enabled"
PAK_META = "/root/dune-item-pak-meta.json"

SOLARIS_TEMPLATE = "SolarisCoin"
SOLARIS_STACK_MAX = 100000      # same cap as dune-storage-write.py
STACK_CEILING = 1000            # never trust an observed stack above this for a non-Solari
SLOT_SAFETY = 2                 # backpack slots left empty after a claim
EXCHANGE_INV = 610              # constants verbatim from deliver-claim.py
EXCHANGE_ID = 2
ACCESS_POINT = 1
COMPLETION_TYPE = 3             # renders as CANCELED + "Take item"
EXCHANGE_PRICE = 250
EXCHANGE_EXPIRY_DAYS = 365      # a NULL expiry does not render (deliver-claim.py)
STATS_JSON = '{"FItemStackAndDurabilityStats": [[], {"DecayedMaxDurability": 0.0}]}'
UNLOCK_PATH = "{CustomizationLibraryActorComponent,m_UnlockedCustomizationSerializableList,m_UnlockedCustomizationIds}"

# Reward template -> unlock id prefix (e.g. Ecaz_Placeables_Swatch -> PlaceableDyePackEcaz).
# CONFIRMED by runbook T3 on 2026-10-13: a rep claim of Hagal_Placeables_Swatch added exactly
# PlaceableDyePackHagal to the pawn's unlock list and no item.
SWATCH_RE = r"^([A-Za-z]+)_(Placeables|LightArmor|Stillsuit|HeavyArmor)_Swatch$"
SWATCH_PREFIX = {
    "Placeables": "PlaceableDyePack",
    "LightArmor": "LArmCharDyepack",
    "Stillsuit": "StillSCharDyepack",
    "HeavyArmor": "HArmCharDyepack",
}

# Templates whose game id is not prefix + template house token (the game spells it differently).
SWATCH_OVERRIDES = {
    "Taligari_Stillsuit_Swatch": "StillSCharDyepackTalgari",
}
# Every dye-pack unlock id seen in a real player's unlock list (all player pawns, read-only,
# 2026-10-13) plus the T3 result. A mapped id outside this set is never written: the line stays
# at the rep as swatch_unmapped, so a spelling the game does not use can never become a dead unlock.
KNOWN_UNLOCKS = frozenset({
    "HArmCharDyepackAgrosaz", "HArmCharDyepackAlexin", "HArmCharDyepackAtre", "HArmCharDyepackDyvetz",
    "HArmCharDyepackEcaz", "HArmCharDyepackHagal", "HArmCharDyepackHark", "HArmCharDyepackHurata",
    "HArmCharDyepackImota", "HArmCharDyepackKenola", "HArmCharDyepackLindaren", "HArmCharDyepackMaros",
    "HArmCharDyepackMikarrol", "HArmCharDyepackMoritani", "HArmCharDyepackMutelli",
    "HArmCharDyepackNovebruns", "HArmCharDyepackRichese", "HArmCharDyepackSor",
    "HArmCharDyepackSpinette", "HArmCharDyepackTalgari", "HArmCharDyepackThorvald",
    "HArmCharDyepackTseida", "HArmCharDyepackVarota", "HArmCharDyepackVernius",
    "HArmCharDyepackWallach", "HArmCharDyepackWayku", "HArmCharDyepackWydras", "LArmCharDyepackAgrosaz",
    "LArmCharDyepackAlexin", "LArmCharDyepackAtre", "LArmCharDyepackDyvetz", "LArmCharDyepackEcaz",
    "LArmCharDyepackHagal", "LArmCharDyepackHark", "LArmCharDyepackHurata", "LArmCharDyepackImota",
    "LArmCharDyepackKenola", "LArmCharDyepackLindaren", "LArmCharDyepackMaros",
    "LArmCharDyepackMikarrol", "LArmCharDyepackMoritani", "LArmCharDyepackMutelli",
    "LArmCharDyepackNovebruns", "LArmCharDyepackRichese", "LArmCharDyepackSor",
    "LArmCharDyepackSpinette", "LArmCharDyepackTalgari", "LArmCharDyepackThorvald",
    "LArmCharDyepackTseida", "LArmCharDyepackVarota", "LArmCharDyepackVernius",
    "LArmCharDyepackWallach", "LArmCharDyepackWayku", "LArmCharDyepackWydras", "PlaceableDyePackAlexin",
    "PlaceableDyePackArgosaz", "PlaceableDyePackAtreides", "PlaceableDyePackDyvetz",
    "PlaceableDyePackEcaz", "PlaceableDyePackHagal", "PlaceableDyePackHarkonnen",
    "PlaceableDyePackHurata", "PlaceableDyePackImota", "PlaceableDyePackKenola",
    "PlaceableDyePackLindaren", "PlaceableDyePackMaros", "PlaceableDyePackMikarrol",
    "PlaceableDyePackMoritani", "PlaceableDyePackMutelli", "PlaceableDyePackNovebruns",
    "PlaceableDyePackRichese", "PlaceableDyePackSor", "PlaceableDyePackSpinette",
    "PlaceableDyePackTaligari", "PlaceableDyePackThorvald", "PlaceableDyePackTseida",
    "PlaceableDyePackVarota", "PlaceableDyePackVernius", "PlaceableDyePackWallach",
    "PlaceableDyePackWayku", "PlaceableDyePackWydras", "StillSCharDyepackAgrosaz",
    "StillSCharDyepackAlexin", "StillSCharDyepackAtre", "StillSCharDyepackDyvetz",
    "StillSCharDyepackEcaz", "StillSCharDyepackHagal", "StillSCharDyepackHark",
    "StillSCharDyepackHurata", "StillSCharDyepackImota", "StillSCharDyepackKenola",
    "StillSCharDyepackLindaren", "StillSCharDyepackMaros", "StillSCharDyepackMikarrol",
    "StillSCharDyepackMoritani", "StillSCharDyepackMutelli", "StillSCharDyepackNovebruns",
    "StillSCharDyepackRichese", "StillSCharDyepackSor", "StillSCharDyepackSpinette",
    "StillSCharDyepackTalgari", "StillSCharDyepackThorvald", "StillSCharDyepackTseida",
    "StillSCharDyepackVarota", "StillSCharDyepackVernius", "StillSCharDyepackWallach",
    "StillSCharDyepackWayku", "StillSCharDyepackWydras"
})

TEMPLATE_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
DQ = "/root/dq.sh"
DB_PORT = "15432"
DB_USER = "postgres"
DB_NAME = "dune"

ERROR_TOKENS = (
    "player_online",
    "no_character",
    "no_backpack",
    "unlock_write_failed",
    "cannot withdraw house reward",
    "lock timeout",
    "statement timeout",
    "write_failed",
)
ERROR_ALIASES = {"cannot withdraw house reward": "withdraw_rejected",
                 "lock timeout": "busy", "statement timeout": "busy"}

# Bounds on the write. A claim never queues behind a lock (the ledger LOCK TABLE would
# otherwise hold every in-game claim behind it) and never outlives the writer's own
# psql timeout, so a timed-out run cannot commit after the caller was told it failed.
LOCK_TIMEOUT = "3s"
STATEMENT_TIMEOUT = "20s"
IDLE_TIMEOUT = "15s"

# Any character row on the account still online, logging out, in reconnect grace or in
# logoff persistence. The timers are `timestamp without time zone` holding UTC, so they
# are compared in UTC whatever the session time zone is.
ONLINE_SQL = ("EXISTS (SELECT 1 FROM dune.encrypted_player_state s WHERE s.account_id = v_acct\n"
              "      AND (s.online_status IS DISTINCT FROM 'Offline'\n"
              "        OR s.reconnect_grace_period_end > (clock_timestamp() AT TIME ZONE 'UTC')\n"
              "        OR s.logoff_persistence_end_time > (clock_timestamp() AT TIME ZONE 'UTC')))")


def emit(obj, code=0):
    print(json.dumps(obj))
    sys.exit(code)


def fail(error, code=1):
    emit({"ok": False, "error": str(error)[:300]}, code)


def pos_int(name, val):
    if isinstance(val, bool):
        fail(f"bad_{name}", 2)
    if isinstance(val, int):
        n = val
    elif isinstance(val, str) and re.fullmatch(r"[0-9]+", val.strip()):
        n = int(val.strip())
    else:
        fail(f"bad_{name}", 2)
    if n < 1:
        fail(f"bad_{name}", 2)
    return n


def _dq(sql, timeout=25):
    """One read-only SQL through dq.sh (-tAc). Stripped stdout, or None on failure."""
    try:
        r = subprocess.run([DQ, "-tAc", sql], capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip()


def resolve_db_pod():
    """Namespace + DB pod by name pattern, so a pod-hash rotation never writes to a stale
    pod. Mirrors dune-storage-write.resolve_db_pod."""
    try:
        ns_out = subprocess.run(["sudo", "kubectl", "get", "ns", "-o", "name"],
                                capture_output=True, text=True, timeout=30)
        ns = next((l.strip().removeprefix("namespace/") for l in ns_out.stdout.splitlines()
                   if l.strip().removeprefix("namespace/").startswith("funcom-seabass-")), "")
        if not ns:
            fail("ns_unresolved", 3)
        pod_out = subprocess.run(["sudo", "kubectl", "get", "pods", "-n", ns, "-o", "name"],
                                 capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        fail("timeout resolving DB pod", 3)
    pod = next((l.strip().removeprefix("pod/") for l in pod_out.stdout.splitlines()
                if l.strip().endswith("-db-dbdepl-sts-0")), "")
    if not pod:
        fail("db_pod_unresolved", 3)
    return ns, pod


def run_psql(ns, pod, sql, timeout=30):
    """The ONLY place a write DB connection is opened. SQL arrives on stdin."""
    cmd = ["sudo", "kubectl", "exec", "-i", "-n", ns, pod, "--",
           "sh", "-c", '[ -n "${POSTGRES_PASSWORD:-}" ] || { echo "FATAL: POSTGRES_PASSWORD unavailable in DB pod" >&2; exit 3; }; PGPASSWORD=$POSTGRES_PASSWORD exec psql "$@"', "_",
           "-h", "localhost", "-p", DB_PORT, "-U", DB_USER, "-d", DB_NAME,
           "-tA", "-v", "ON_ERROR_STOP=1"]
    try:
        return subprocess.run(cmd, input=sql, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        fail("psql_timeout", 1)


def parse_psql_error(stderr):
    blob = (stderr or "").lower()
    for tok in ERROR_TOKENS:
        if tok in blob:
            return ERROR_ALIASES.get(tok, tok)
    return "write_failed"


def last_json_line(stdout):
    raw = ""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            raw = line
    return raw


def flag_allows(path, account_id):
    """Fail closed: a missing, unreadable or empty gate file admits nobody. One account
    id per line is an allowlist, and only a line that is exactly '*' admits everyone.
    '#' starts a comment."""
    try:
        with open(path) as fh:
            ids = {l.split("#", 1)[0].strip() for l in fh}
    except OSError:
        return False
    return "*" in ids or str(account_id) in ids


# ---------------------------------------------------------------------------
# Reads (outside the transaction, so the write holds its locks as briefly as possible)
# ---------------------------------------------------------------------------

def read_context(owner):
    """account_id + the pending templates, their largest live stack and exchange
    category. A template that appears between this read and the write has no stack cap
    in the transaction, so it stays at the rep this time."""
    raw = _dq(
        "SET search_path TO dune, public;\n"
        "WITH p AS (SELECT DISTINCT template_id FROM dune.landsraad_house_rewards\n"
        f"           WHERE player_id = {owner} AND amount > 0)\n"
        "SELECT json_build_object(\n"
        "  'templates', (SELECT COALESCE(json_agg(template_id), '[]'::json) FROM p),\n"
        f"  'account_id', (SELECT account_id FROM dune.encrypted_player_state\n"
        f"                  WHERE player_controller_id = {owner}\n"
        "                    AND character_state IS DISTINCT FROM 'Deleted' LIMIT 1),\n"
        # one pass over dune.items (514k rows, no template_id index), not one per template
        "  'stacks', (SELECT COALESCE(json_object_agg(s.template_id, s.mx), '{}'::json)\n"
        "             FROM (SELECT template_id, max(stack_size) AS mx FROM dune.items\n"
        "                    WHERE template_id IN (SELECT template_id FROM p)\n"
        "                    GROUP BY template_id) s),\n"
        "  'cats', (SELECT COALESCE(json_object_agg(p.template_id,\n"
        "                    json_build_array(c.category_mask, c.category_depth)), '{}'::json) FROM p\n"
        "           JOIN LATERAL (SELECT category_mask, category_depth FROM dune.dune_exchange_orders o\n"
        "                          WHERE o.template_id = p.template_id LIMIT 1) c ON true)\n"
        ")")
    if not raw:
        fail("read_failed", 1)
    try:
        ctx = json.loads(raw.splitlines()[-1])
    except (ValueError, IndexError):
        fail("read_failed", 1)
    return ctx


def load_pak_caps(path):
    """lowercased template -> PAK max_stack. The catalog is required: without it the
    writer cannot tell an item from anything else, so it refuses rather than guess."""
    try:
        with open(path) as fh:
            items = json.load(fh)["items"]
        caps = {k: int(v["max_stack"]) for k, v in items.items()
                if isinstance(v, dict) and isinstance(v.get("max_stack"), int)}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        fail("stack_catalog_missing", 1)
    if not caps:
        fail("stack_catalog_missing", 1)
    return caps


def safe_stacks(templates, stacks, pak):
    """template -> stack cap for every pending template the PAK knows: the PAK max,
    lowered to the largest stack seen live when one exists (a live config can differ,
    and an oversized stack written by a grant must not raise the cap), never above
    STACK_CEILING. Unknown templates are left out, so the transaction keeps them at
    the rep."""
    observed = {}
    for tmpl, mx in (stacks or {}).items():
        try:
            observed[tmpl] = int(mx)
        except (TypeError, ValueError):
            continue
    out = {}
    for tmpl in set(templates or []) | set(observed):
        if not isinstance(tmpl, str) or not TEMPLATE_RE.match(tmpl) or tmpl == SOLARIS_TEMPLATE:
            continue
        cap = pak.get(tmpl.lower())
        if cap is None:
            continue
        if observed.get(tmpl, 0) > 0:
            cap = min(cap, observed[tmpl])
        out[tmpl] = max(1, min(cap, STACK_CEILING))
    return out


def safe_cats(cats):
    out = {}
    for tmpl, pair in (cats or {}).items():
        if not TEMPLATE_RE.match(tmpl) or tmpl == SOLARIS_TEMPLATE:
            continue
        try:
            out[tmpl] = (int(pair[0]), int(pair[1]))
        except (TypeError, ValueError, IndexError):
            continue
    return out


# ---------------------------------------------------------------------------
# The transaction
# ---------------------------------------------------------------------------

def _values(rows, cols):
    if not rows:
        return f"SELECT {', '.join('NULL::' + c for c in cols)} WHERE false"
    return "VALUES " + ", ".join(rows)


def build_claim_sql(owner, stacks, cats, swatches_on, preview, run_token, operator):
    """owner is a validated int; every template in stacks/cats matched TEMPLATE_RE and
    run_token/operator are charset-checked, so every interpolation is injection-safe."""
    stack_rows = [f"('{t}', {n})" for t, n in sorted(stacks.items())]
    cat_rows = [f"('{t}', {m}, {d})" for t, (m, d) in sorted(cats.items())]
    prefix_case = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in SWATCH_PREFIX.items())
    override_rows = [f"('{t}', '{u}')" for t, u in sorted(SWATCH_OVERRIDES.items())]
    known_rows = [f"('{u}')" for u in sorted(KNOWN_UNLOCKS)]
    end = "ROLLBACK;" if preview else "COMMIT;"
    return f"""\\set ON_ERROR_STOP on
SET search_path TO dune, public;
BEGIN;
SET LOCAL lock_timeout = '{LOCK_TIMEOUT}';
SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}';
SET LOCAL idle_in_transaction_session_timeout = '{IDLE_TIMEOUT}';
CREATE TEMP TABLE _lc_stack(template text PRIMARY KEY, max_stack bigint) ON COMMIT DROP;
INSERT INTO _lc_stack {_values(stack_rows, ['text', 'bigint'])};
CREATE TEMP TABLE _lc_cat(template text PRIMARY KEY, mask bigint, depth bigint) ON COMMIT DROP;
INSERT INTO _lc_cat {_values(cat_rows, ['text', 'bigint', 'bigint'])};
CREATE TEMP TABLE _lc_swatch_override(template text PRIMARY KEY, unlock text) ON COMMIT DROP;
INSERT INTO _lc_swatch_override {_values(override_rows, ['text', 'text'])};
CREATE TEMP TABLE _lc_known_unlock(unlock text PRIMARY KEY) ON COMMIT DROP;
INSERT INTO _lc_known_unlock {_values(known_rows, ['text'])};
CREATE TEMP TABLE _lc_out(seq serial, lane text, house text, template text, qty bigint,
                          item_id bigint, order_id bigint, note text) ON COMMIT DROP;
CREATE TEMP TABLE _lc_meta(online bool, withdrawn int) ON COMMIT DROP;
DO $claim$
DECLARE
  v_preview bool := {'true' if preview else 'false'};
  v_swatches bool := {'true' if swatches_on else 'false'};
  v_status text; v_acct bigint; v_pawn bigint; v_fls text; v_online bool; v_online_end bool;
  v_bp bigint; v_bp_max bigint; v_free bigint[]; v_free_i int := 1;
  v_line record; v_left bigint; v_chunk bigint; v_max bigint; v_done bigint;
  v_mask bigint; v_depth bigint; v_expiry bigint; v_item bigint; v_order bigint;
  v_coin bigint; v_coin_stack bigint; v_unlock text; v_has bool;
  v_withdraw dune.landsraadplayerhousereward[] := '{{}}';
BEGIN
  -- 1. OFFLINE-GATE (start). Backpack + pawn jsonb are RAM-resident while the pawn is
  --    live: online, logging out, in reconnect grace or in logoff persistence. Checked
  --    across EVERY character row on the account. An apply locks those rows first, so a
  --    login's own status write waits for this claim instead of racing it.
  SELECT online_status::text, account_id, player_pawn_id
    INTO v_status, v_acct, v_pawn
    FROM dune.encrypted_player_state
   WHERE player_controller_id = {owner} AND character_state IS DISTINCT FROM 'Deleted'
   ORDER BY last_login_time DESC NULLS LAST LIMIT 1;
  IF v_pawn IS NULL THEN RAISE EXCEPTION 'no_character'; END IF;
  IF NOT v_preview THEN
    PERFORM 1 FROM dune.encrypted_player_state WHERE account_id = v_acct FOR UPDATE;
  END IF;
  v_online := {ONLINE_SQL};
  IF v_online AND NOT v_preview THEN
    RAISE EXCEPTION 'player_online (status=% account_live=true)', COALESCE(v_status, '?');
  END IF;
  SELECT "user" INTO v_fls FROM dune.accounts WHERE id = v_acct;

  -- 2. the pawn's backpack, locked, and its free slots (SLOT_SAFETY kept empty below)
  SELECT id, max_item_count INTO v_bp, v_bp_max
    FROM dune.inventories WHERE actor_id = v_pawn AND inventory_type = 0 LIMIT 1;
  IF v_bp IS NULL THEN RAISE EXCEPTION 'no_backpack'; END IF;
  PERFORM 1 FROM dune.inventories WHERE id = v_bp FOR UPDATE;
  PERFORM 1 FROM dune.actors WHERE id = v_pawn FOR UPDATE;
  SELECT COALESCE(array_agg(s.idx ORDER BY s.idx), '{{}}') INTO v_free
    FROM generate_series(0, GREATEST(COALESCE(v_bp_max, 0), 0) - 1) AS s(idx)
   WHERE NOT EXISTS (SELECT 1 FROM dune.items WHERE inventory_id = v_bp AND position_index = s.idx);

  -- 3. freeze the ledger the same way the native proc does (re-entrant in this txn)
  LOCK TABLE dune.landsraad_house_rewards IN EXCLUSIVE MODE;

  -- 4. exchange expiry on the game clock (deliver-claim.py game_now). No clock -> no
  --    exchange lane this claim; overflow then stays at the rep instead of guessing.
  SELECT (EXTRACT(EPOCH FROM ((now() AT TIME ZONE 'UTC') - universe_time_timestamp))
          - COALESCE(down_time_accumulation, 0) / 1000000.0)::bigint + {EXCHANGE_EXPIRY_DAYS} * 86400
    INTO v_expiry
    FROM dune.farm_variables WHERE universe_time_timestamp IS NOT NULL
   ORDER BY universe_time_timestamp LIMIT 1;
  PERFORM pg_advisory_xact_lock(hashtext('lastsietch-exchange-inv-{EXCHANGE_INV}'));

  -- 5. every pending line. Solari first so it gets the backpack room the exchange cannot
  --    give it; swatches last because they need no slot at all.
  FOR v_line IN
    SELECT house_name, template_id, amount FROM dune.landsraad_house_rewards
     WHERE player_id = {owner} AND amount > 0
     ORDER BY CASE WHEN template_id = '{SOLARIS_TEMPLATE}' THEN 0
                   WHEN template_id ~ '_Swatch$' THEN 3
                   WHEN NOT EXISTS (SELECT 1 FROM _lc_cat c WHERE c.template = template_id) THEN 1
                   ELSE 2 END,
              template_id, house_name
  LOOP
    v_done := 0;
    IF v_line.template_id ~ '_Swatch$' THEN
      IF NOT v_swatches THEN
        INSERT INTO _lc_out(lane, house, template, qty, note)
          VALUES ('rep', v_line.house_name, v_line.template_id, v_line.amount, 'swatch_at_rep');
        CONTINUE;
      END IF;
      v_unlock := COALESCE(
        (SELECT o.unlock FROM _lc_swatch_override o WHERE o.template = v_line.template_id),
        CASE (regexp_match(v_line.template_id, '{SWATCH_RE}'))[2] {prefix_case} END
          || (regexp_match(v_line.template_id, '{SWATCH_RE}'))[1]);
      IF v_unlock IS NULL OR NOT EXISTS (SELECT 1 FROM _lc_known_unlock k WHERE k.unlock = v_unlock) THEN
        INSERT INTO _lc_out(lane, house, template, qty, note)
          VALUES ('rep', v_line.house_name, v_line.template_id, v_line.amount, 'swatch_unmapped');
        CONTINUE;
      END IF;
      SELECT EXISTS (SELECT 1 FROM dune.actors a,
                       jsonb_array_elements(COALESCE(a.properties #> '{UNLOCK_PATH}', '[]'::jsonb)) e
                      WHERE a.id = v_pawn AND e->>'m_CustomizationId' = v_unlock) INTO v_has;
      IF NOT v_has THEN
        UPDATE dune.actors
           SET properties = jsonb_set(properties, '{UNLOCK_PATH}',
                 COALESCE(properties #> '{UNLOCK_PATH}', '[]'::jsonb)
                   || jsonb_build_array(jsonb_build_object('m_CustomizationId', v_unlock)), true)
         WHERE id = v_pawn;
        -- jsonb_set silently does nothing when a parent key is missing; prove it landed.
        SELECT EXISTS (SELECT 1 FROM dune.actors a,
                         jsonb_array_elements(COALESCE(a.properties #> '{UNLOCK_PATH}', '[]'::jsonb)) e
                        WHERE a.id = v_pawn AND e->>'m_CustomizationId' = v_unlock) INTO v_has;
        IF NOT v_has THEN RAISE EXCEPTION 'unlock_write_failed %', v_unlock; END IF;
        INSERT INTO _lc_out(lane, house, template, qty, note)
          VALUES ('unlock', v_line.house_name, v_line.template_id, v_line.amount, v_unlock);
      ELSE
        INSERT INTO _lc_out(lane, house, template, qty, note)
          VALUES ('unlock', v_line.house_name, v_line.template_id, v_line.amount, 'already_unlocked');
      END IF;
      v_done := v_line.amount;
    ELSE
      v_left := v_line.amount;
      IF v_line.template_id = '{SOLARIS_TEMPLATE}' THEN
        v_max := {SOLARIS_STACK_MAX};
        -- merge into a Solari stack already in the bag before spending a slot
        SELECT id, stack_size INTO v_coin, v_coin_stack FROM dune.items
         WHERE inventory_id = v_bp AND template_id = '{SOLARIS_TEMPLATE}' AND stack_size < {SOLARIS_STACK_MAX}
         ORDER BY position_index, id LIMIT 1 FOR UPDATE;
        IF FOUND THEN
          v_chunk := LEAST(v_left, {SOLARIS_STACK_MAX} - v_coin_stack);
          UPDATE dune.items SET stack_size = stack_size + v_chunk WHERE id = v_coin;
          INSERT INTO _lc_out(lane, house, template, qty, item_id, note)
            VALUES ('backpack', v_line.house_name, v_line.template_id, v_chunk, v_coin, 'merged');
          v_left := v_left - v_chunk; v_done := v_done + v_chunk;
        END IF;
      ELSE
        SELECT max_stack INTO v_max FROM _lc_stack WHERE template = v_line.template_id;
        IF v_max IS NULL THEN
          -- not in the item catalog (or new since the pre-read): never guess an item row
          INSERT INTO _lc_out(lane, house, template, qty, note)
            VALUES ('rep', v_line.house_name, v_line.template_id, v_line.amount, 'unknown_item');
          CONTINUE;
        END IF;
      END IF;
      SELECT mask, depth INTO v_mask, v_depth FROM _lc_cat WHERE template = v_line.template_id;
      WHILE v_left > 0 LOOP
        v_chunk := LEAST(v_left, v_max);
        IF cardinality(v_free) - v_free_i + 1 > {SLOT_SAFETY} THEN
          INSERT INTO dune.items (inventory_id, stack_size, position_index, template_id, stats,
                                  quality_level, acquisition_time, is_new)
            VALUES (v_bp, v_chunk, v_free[v_free_i], v_line.template_id,
                    CASE WHEN v_line.template_id = '{SOLARIS_TEMPLATE}' THEN '{STATS_JSON}'::jsonb
                         ELSE '{{}}'::jsonb END, 0, 0, true)
            RETURNING id INTO v_item;
          v_free_i := v_free_i + 1;
          INSERT INTO _lc_out(lane, house, template, qty, item_id)
            VALUES ('backpack', v_line.house_name, v_line.template_id, v_chunk, v_item);
        ELSIF v_mask IS NOT NULL AND v_expiry IS NOT NULL THEN
          -- the three rows deliver-claim.py writes, in the same order
          INSERT INTO dune.items (inventory_id, stack_size, position_index, template_id, stats,
                                  quality_level, acquisition_time, is_new)
            SELECT {EXCHANGE_INV}, v_chunk, COALESCE(MAX(position_index), 0) + 1, v_line.template_id,
                   '{{}}'::jsonb, 0, 0, true
              FROM dune.items WHERE inventory_id = {EXCHANGE_INV}
            RETURNING id INTO v_item;
          INSERT INTO dune.dune_exchange_orders
            (revision, exchange_id, owner_id, item_id, template_id, category_mask, category_depth,
             access_point_id, is_npc_order, item_price, expiration_time,
             durability_cur, durability_max, quality_level)
            VALUES (1, {EXCHANGE_ID}, {owner}, v_item, v_line.template_id, v_mask, v_depth,
                    {ACCESS_POINT}, false, {EXCHANGE_PRICE}, v_expiry, 0, 0, 0)
            RETURNING id INTO v_order;
          INSERT INTO dune.dune_exchange_fulfilled_orders
            (order_id, completion_type, stack_size, source_order_id, original_order_id)
            VALUES (v_order, {COMPLETION_TYPE}, v_chunk, NULL, v_order);
          INSERT INTO _lc_out(lane, house, template, qty, item_id, order_id)
            VALUES ('exchange', v_line.house_name, v_line.template_id, v_chunk, v_item, v_order);
        ELSE
          EXIT;
        END IF;
        v_left := v_left - v_chunk; v_done := v_done + v_chunk;
      END LOOP;
      IF v_left > 0 THEN
        INSERT INTO _lc_out(lane, house, template, qty, note)
          VALUES ('rep', v_line.house_name, v_line.template_id, v_left, 'no_room');
      END IF;
    END IF;
    IF v_done > 0 THEN
      v_withdraw := v_withdraw
        || ROW(v_line.house_name, v_line.template_id, v_done)::dune.landsraadplayerhousereward;
    END IF;
  END LOOP;

  -- 6. the native withdraw: validates every line against the ledger and decrements it,
  --    firing the same house_rewards_changed notify an in-game claim does.
  IF cardinality(v_withdraw) > 0 THEN
    PERFORM dune.landsraad_withdraw_house_reward({owner}, v_withdraw);
  END IF;

  -- 7. trace the item lanes alongside every other delivery (exchange rows stay pending
  --    until confirm-claims.py sees the item leave inv {EXCHANGE_INV}).
  INSERT INTO dune.ls_item_delivery_log
    (correlation_id, source, lane, account_id, fls_id, actor_id, template_id, quantity,
     quality_level, status, delivered_at, observed_item_id, operator)
  SELECT '{run_token}-' || seq, 'landsraad_claim',
         CASE lane WHEN 'backpack' THEN 'db' ELSE 'exchange' END,
         v_acct, v_fls, v_pawn, template, qty, 0,
         CASE lane WHEN 'backpack' THEN 'delivered' ELSE 'pending' END,
         CASE lane WHEN 'backpack' THEN now() END, item_id, '{operator}'
    FROM _lc_out WHERE lane IN ('backpack', 'exchange');

  -- 8. OFFLINE-GATE (end). A login that raced the claim rolls everything back.
  IF NOT v_preview THEN
    v_online_end := {ONLINE_SQL};
    IF v_online_end THEN
      SELECT online_status::text INTO v_status FROM dune.encrypted_player_state
       WHERE player_controller_id = {owner} AND character_state IS DISTINCT FROM 'Deleted'
       ORDER BY last_login_time DESC NULLS LAST LIMIT 1;
      RAISE EXCEPTION 'player_online at commit (status=%)', COALESCE(v_status, '?');
    END IF;
  END IF;
  INSERT INTO _lc_meta VALUES (v_online, cardinality(v_withdraw));
END $claim$;
SELECT json_build_object(
  'ok', true,
  'mode', '{'preview' if preview else 'apply'}',
  'online', (SELECT online FROM _lc_meta),
  'withdrawn_lines', (SELECT withdrawn FROM _lc_meta),
  'lines', (SELECT COALESCE(json_agg(json_build_object(
              'lane', lane, 'house', house, 'template', template, 'qty', qty,
              'order_id', order_id, 'note', note) ORDER BY seq), '[]'::json) FROM _lc_out)
);
{end}
"""


def gather_args():
    if "--stdin-json" in sys.argv[1:]:
        try:
            payload = json.loads(sys.stdin.read())
        except (ValueError, json.JSONDecodeError):
            fail("bad_json", 2)
        if not isinstance(payload, dict):
            fail("bad_json", 2)
        owner = pos_int("owner_ctrl", payload.get("owner_ctrl"))
        mode = payload.get("mode")
        operator = payload.get("operator") or "portal"
        print_sql = False
    else:
        ap = argparse.ArgumentParser(description="Portal Landsraad claim writer")
        ap.add_argument("--owner-ctrl", required=True)
        ap.add_argument("--mode", default="preview", choices=["preview", "apply"])
        ap.add_argument("--operator", default="cli")
        ap.add_argument("--print-sql", action="store_true",
                        help="print the transaction instead of running it (test suite)")
        ap.add_argument("--context-json", default=None,
                        help="with --print-sql: the read_context() result to build from")
        ap.add_argument("--swatches", action="store_true", help="with --print-sql only")
        ap.add_argument("--pak-meta", default=PAK_META, help="with --print-sql: the item catalog")
        a = ap.parse_args()
        owner = pos_int("owner_ctrl", a.owner_ctrl)
        mode, operator, print_sql = a.mode, a.operator, a.print_sql
        if print_sql:
            ctx = json.loads(a.context_json or "{}")
            stacks = safe_stacks(ctx.get("templates"), ctx.get("stacks"), load_pak_caps(a.pak_meta))
            sql = build_claim_sql(owner, stacks, safe_cats(ctx.get("cats")),
                                  a.swatches, mode == "preview", f"lastsietch-ls-claim-{owner}-test", "test")
            print(sql)
            sys.exit(0)
    if mode not in ("preview", "apply"):
        fail("bad_mode", 2)
    operator = re.sub(r"[^A-Za-z0-9:_#.-]", "", str(operator))[:64] or "portal"
    return owner, mode, operator


def main():
    owner, mode, operator = gather_args()
    pak = load_pak_caps(PAK_META)
    ctx = read_context(owner)
    account_id = ctx.get("account_id")
    if not account_id:
        fail("no_character", 1)
    if not flag_allows(ENABLED_FLAG, account_id):
        emit({"ok": False, "error": "disabled"}, 0)
    swatches_on = flag_allows(SWATCH_FLAG, account_id)
    run_token = f"lastsietch-ls-claim-{owner}-{int(time.time() * 1000)}"
    sql = build_claim_sql(owner, safe_stacks(ctx.get("templates"), ctx.get("stacks"), pak),
                          safe_cats(ctx.get("cats")), swatches_on, mode == "preview", run_token, operator)
    ns, pod = resolve_db_pod()
    out = run_psql(ns, pod, sql)
    if out.returncode != 0:
        fail(parse_psql_error(out.stderr or out.stdout), 1)
    raw = last_json_line(out.stdout)
    try:
        result = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        # psql exited 0, so the script ran to its COMMIT: the claim may well have landed
        fail("result_unreadable", 1)
    result["swatches_enabled"] = swatches_on
    emit(result, 0)


if __name__ == "__main__":
    main()
