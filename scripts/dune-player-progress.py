#!/usr/bin/env python3
# Player progress: per-account character + economy stats for the public portal
# account dashboard. ONE read call gives the vitals card its XP / skill points
# and Solari / Scrip totals without the heavier character-export.
#
# Deployed to lastsietch-dune:/root/dune-player-progress.py, invoked by the relay over
# SSH via the dispatcher's `player-progress <account_id>` token.
#
# Read-only. SELECT statements only; no game-state mutation, no restart.
#
# Data layout:
#   - dune.encrypted_player_state: account_id -> (player_controller_id, player_pawn_id)
#   - dune.fgl_entities: the pawn's DuneCharacter slot carries FLevelComponent[1]
#     with TotalXPEarned / TotalSkillPoints / UnspentSkillPoints /
#     KeystoneBonusSkillPoints (lives on exactly ONE fgl_entity row per actor).
#   - dune.player_virtual_currency_balances: consolidated wallet, keyed by
#     player_controller_id (resolved via dune.player_state). currency_id 0 =
#     Solari, 1 = Scrip. One row per (controller, currency) = the player total.
#
# Output shape:
#   {"available": true, "account_id": N,
#    "character": {"xp": int, "total_sp": int, "unspent_sp": int, "keystone_sp": int},
#    "economy":   {"bank_solari": int|null, "pocket_solari": int, "scrip": int|null,
#                  "solari": int|null (alias of bank_solari)},
#    "faction":   {"faction_id": int|null, "faction_name": str|null, "reputation": int|null}}

import json
import subprocess
import sys


# Identity: confirm the account exists + has a pawn (mirrors progression-state).
# {ctrl_eps} is empty for the default (LIMIT 1 most-recent) pick, or an
# `AND eps.player_controller_id = N` filter when a specific character is
# requested (multi-character accounts, portal character switcher). When a
# requested controller does not resolve (deleted / not owned by the account),
# the caller falls back to the default pick, so a scoped read is fail-safe.
EXISTS_SQL = """
SET search_path TO dune, public;
SELECT coalesce(json_build_object(
  'player_controller_id', eps.player_controller_id,
  'player_pawn_id',       eps.player_pawn_id
), '{{}}'::json)
FROM dune.encrypted_player_state eps
WHERE eps.account_id = {account_id}::bigint
  AND eps.character_state IS DISTINCT FROM 'Deleted'
{ctrl_eps}
ORDER BY eps.last_avatar_activity DESC NULLS LAST, eps.player_controller_id DESC
LIMIT 1;
"""

# List EVERY non-Deleted character on an account (portal character switcher).
# Names are decrypted host-side via dune.decrypt_user_data (game DB stores them
# encrypted); level via the same FLevelComponent read the telemetry stream uses.
# DISTINCT ON keeps one row per controller even if the fgl join fans out.
LIST_SQL = """
SET search_path TO dune, public;
SELECT coalesce(json_agg(row_to_json(t)), '[]'::json)
FROM (
  SELECT DISTINCT ON (eps.player_controller_id)
         eps.player_controller_id AS controller_id,
         eps.player_pawn_id       AS pawn_id,
         dune.decrypt_user_data(eps.encrypted_character_name) AS char_name,
         (eps.online_status = 'Online') AS online,
         dune.ls_xp_to_level(
           COALESCE((fe.components#>>'{{FLevelComponent,1,TotalXPEarned}}')::bigint, 0)) AS lvl,
         EXTRACT(EPOCH FROM eps.last_avatar_activity)::bigint AS last_activity
  FROM dune.encrypted_player_state eps
  LEFT JOIN dune.actor_fgl_entities afe
    ON afe.actor_id = eps.player_pawn_id AND afe.slot_name = 'DuneCharacter'
  LEFT JOIN dune.fgl_entities fe
    ON fe.entity_id = afe.entity_id AND fe.components ? 'FLevelComponent'
  WHERE eps.account_id = {account_id}::bigint
    AND eps.player_pawn_id IS NOT NULL
    AND eps.character_state IS DISTINCT FROM 'Deleted'
  ORDER BY eps.player_controller_id, eps.last_avatar_activity DESC NULLS LAST
) t;
"""

# Character XP + skill points from the pawn's DuneCharacter FLevelComponent[1].
# FLevelComponent lives on only one of the actor's fgl_entity rows, so filter to
# the row that actually carries it.
CHARACTER_SQL = """
SET search_path TO dune, public;
SELECT coalesce(jsonb_build_object(
  'xp',          (fe.components->'FLevelComponent'->1->>'TotalXPEarned')::bigint,
  'total_sp',    (fe.components->'FLevelComponent'->1->>'TotalSkillPoints')::int,
  'unspent_sp',  (fe.components->'FLevelComponent'->1->>'UnspentSkillPoints')::int,
  'keystone_sp', (fe.components->'FLevelComponent'->1->>'KeystoneBonusSkillPoints')::int
), '{{}}'::jsonb)
FROM dune.fgl_entities fe
JOIN dune.actor_fgl_entities afe ON afe.entity_id = fe.entity_id
JOIN dune.encrypted_player_state eps ON eps.player_pawn_id = afe.actor_id
WHERE eps.account_id = {account_id}::bigint
  AND afe.slot_name = 'DuneCharacter'
  AND fe.components->'FLevelComponent'->1 IS NOT NULL
{ctrl_eps}
LIMIT 1;
"""

# Consolidated currency balances. currency_id 0 = BANK Solari ("Solari Credits",
# the in-game top-right wallet number), 1 = House Scrip. Keyed by controller id.
# (Pocket "Solari Coins" are NOT here — they are SolarisCoin inventory items; see
# POCKET_SOLARI_SQL.) Reconciled 2026-06-04, our internal notes*.
ECONOMY_SQL = """
SET search_path TO dune, public;
SELECT coalesce(jsonb_object_agg(t.currency_id::text, t.bal), '{{}}'::jsonb)
FROM (
  SELECT vcb.currency_id, sum(vcb.balance) AS bal
  FROM dune.player_virtual_currency_balances vcb
  JOIN dune.player_state ps ON ps.player_controller_id = vcb.player_controller_id
  WHERE ps.account_id = {account_id}::bigint
    AND vcb.currency_id IN (0, 1)
{ctrl_ps}
  GROUP BY vcb.currency_id
) t;
"""

# Pocket Solari = total of all SolarisCoin inventory-item stacks the player owns,
# summed across every inventory they control: their character inventories
# (backpack etc. on the pawn) PLUS placed containers and vehicle/cargo storage
# they own at rank 1. Mirrors the container-browser ownership chain
# (placeables.owner_entity_id -> actor_fgl_entities -> permission_actor_rank
# rank=1 -> controller). Scoping to the pawn alone misses container/vehicle Solari.
POCKET_SOLARI_SQL = """
SET search_path TO dune, public;
WITH ids AS (
  SELECT player_pawn_id AS pawn, player_controller_id AS ctrl
  FROM dune.encrypted_player_state WHERE account_id = {account_id}::bigint
{ctrl_ids}
),
owned_inv AS (
  -- character inventories (backpack etc. on the pawn)
  SELECT inv.id FROM dune.inventories inv, ids WHERE inv.actor_id = ids.pawn
  UNION
  -- placed containers (placeable chain)
  SELECT inv.id
  FROM dune.placeables p
  JOIN dune.actor_fgl_entities afe ON afe.entity_id = p.owner_entity_id
  JOIN dune.permission_actor_rank par ON par.permission_actor_id = afe.actor_id AND par.rank = 1
  JOIN dune.inventories inv ON inv.actor_id = p.id
  JOIN ids ON par.player_id = ids.ctrl
  UNION
  -- vehicle cargo (vehicle actor owned at rank 1; not a placeable). DD vehicle
  -- cargo is RAM-resident (not in this DB), so only Hagga vehicles contribute.
  SELECT inv.id
  FROM dune.permission_actor_rank par
  JOIN dune.actors a ON a.id = par.permission_actor_id
  JOIN dune.inventories inv ON inv.actor_id = a.id AND inv.inventory_type = 0 AND inv.max_item_count > 0
  JOIN ids ON par.player_id = ids.ctrl
  WHERE par.rank = 1
    AND (a.class ILIKE '%ornithopter%' OR a.class ILIKE '%buggy%'
         OR a.class ILIKE '%sandbike%' OR a.class ILIKE '%crawler%'
         OR a.class ILIKE '%containervehicle%')
)
SELECT coalesce(sum(it.stack_size), 0)::bigint
FROM dune.items it
WHERE it.template_id = 'SolarisCoin' AND it.inventory_id IN (SELECT id FROM owned_inv);
"""


# Faction alignment + numeric standing. player_faction / player_faction_reputation
# are keyed by actor_id = the player CONTROLLER id (resolve via encrypted_player_state).
# factions: 1=Atreides, 2=Harkonnen, 3=None, 4=Smuggler.
FACTION_SQL = """
SET search_path TO dune, public;
SELECT coalesce(jsonb_build_object(
  'faction_id',   pf.faction_id,
  'faction_name', f.name,
  'reputation',   coalesce(pfr.reputation_amount, 0)
), '{{}}'::jsonb)
FROM dune.player_faction pf
JOIN dune.encrypted_player_state eps ON eps.player_controller_id = pf.actor_id
LEFT JOIN dune.player_faction_reputation pfr
  ON pfr.actor_id = pf.actor_id AND pfr.faction_id = pf.faction_id
LEFT JOIN dune.factions f ON f.id = pf.faction_id
WHERE eps.account_id = {account_id}::bigint
{ctrl_eps}
LIMIT 1;
"""


def _query_json(sql, fallback="null"):
    """Run psql via the dq.sh wrapper, strip the SET header, return parsed JSON."""
    out = subprocess.run(
        ["/root/dq.sh", "-tAc", sql],
        capture_output=True, text=True, timeout=45, check=False)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip()[:500])
    raw = ""
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if line and line != "SET":
            raw = line
    if not raw:
        raw = fallback
    return json.loads(raw)


def _ctrl_filters(controller_id):
    """Build the per-alias `AND <alias>.player_controller_id = N` fragments for a
    scoped (single-character) read, or empty strings for the default pick. The
    caller validates controller_id is a positive integer before this point."""
    if controller_id is None:
        return {"ctrl_eps": "", "ctrl_ps": "", "ctrl_ids": ""}
    cid = int(controller_id)
    return {
        "ctrl_eps": "  AND eps.player_controller_id = %d::bigint" % cid,
        "ctrl_ps": "  AND ps.player_controller_id = %d::bigint" % cid,
        "ctrl_ids": "  AND player_controller_id = %d::bigint" % cid,
    }


def list_characters(account_id, query_json):
    """Return every non-Deleted character on an account for the portal switcher:
    [{controller_id, pawn_id, char_name, lvl, online, last_activity}], most
    recently active first, with is_default set on the top (= the resolver's
    default pick). Empty list if the account has no live character."""
    rows = query_json(LIST_SQL.format(account_id=account_id), "[]") or []
    rows.sort(key=lambda r: (r.get("last_activity") is not None,
                             r.get("last_activity") or 0,
                             r.get("controller_id") or 0),
              reverse=True)
    for i, r in enumerate(rows):
        r["controller_id"] = int(r["controller_id"]) if r.get("controller_id") is not None else None
        r["pawn_id"] = int(r["pawn_id"]) if r.get("pawn_id") is not None else None
        r["lvl"] = int(r["lvl"]) if r.get("lvl") is not None else None
        r["online"] = bool(r.get("online"))
        r["is_default"] = (i == 0)
    return {"available": True, "account_id": int(account_id), "characters": rows}


def build(account_id, query_json, controller_id=None):
    """Assemble the player-progress payload. `account_id` is a digit string.
    `query_json(sql, fallback)` runs the SQL and returns parsed JSON (the CLI
    passes the dq.sh runner; the telemetry collector passes a psycopg2 runner).
    `controller_id` (optional) scopes every read to ONE character on the account
    (portal switcher); when it does not resolve to a live character the payload
    falls back to the default most-recently-active pick. Returns the same dict
    the CLI prints. Raises on a DB error (caller wraps)."""
    cf = _ctrl_filters(controller_id)
    ident = query_json(EXISTS_SQL.format(account_id=account_id, **cf), "{}")
    if (not ident or not ident.get("player_pawn_id")) and controller_id is not None:
        # Requested character is gone / not on this account: fail safe to the
        # account's default pick rather than erroring the whole read.
        cf = _ctrl_filters(None)
        ident = query_json(EXISTS_SQL.format(account_id=account_id, **cf), "{}")
    if not ident or not ident.get("player_pawn_id"):
        return {"available": False, "error": "account_not_found",
                "account_id": int(account_id)}

    char = query_json(CHARACTER_SQL.format(account_id=account_id, **cf), "{}") or {}
    econ_raw = query_json(ECONOMY_SQL.format(account_id=account_id, **cf), "{}") or {}
    pocket_raw = query_json(POCKET_SOLARI_SQL.format(account_id=account_id, **cf), "0")
    fac_raw = query_json(FACTION_SQL.format(account_id=account_id, **cf), "{}") or {}

    bank_solari = int(econ_raw["0"]) if "0" in econ_raw else None
    economy = {
        # bank = vcb currency_id 0 = dune.get_solaris_id(). This is what the exchange debits and
        # what the Karum debits.
        # ⚠️ It is NOT necessarily the figure a player sees in game. Measured 2026-07-27 (LT-2):
        # the owner's INVENTORY PANEL header read 100,318, which matched `pocket_solari` exactly
        # (two SolarisCoin stacks, 75,000 + 25,318), while currency 0 held 65,763,675. An earlier
        # version of this comment called currency 0 "the in-game top-right wallet figure"; that is
        # at best ambiguous about which HUD element it means. Keep the portal's "Banked Solari"
        # label, which is accurate either way, and settle which HUD element is which during the
        # two-account self-test rather than restoring the old claim.
        "bank_solari": bank_solari,
        "solari": bank_solari,  # back-compat alias (older portal read 'solari')
        # pocket = SolarisCoin ITEM rows across backpack + owned containers + vehicles. Item rows,
        # so subject to the online-write hazards the wallet is immune to.
        "pocket_solari": int(pocket_raw) if pocket_raw is not None else 0,
        "scrip": int(econ_raw["1"]) if "1" in econ_raw else None,
    }
    character = {
        "xp": int(char["xp"]) if char.get("xp") is not None else None,
        "total_sp": int(char["total_sp"]) if char.get("total_sp") is not None else None,
        "unspent_sp": int(char["unspent_sp"]) if char.get("unspent_sp") is not None else None,
        "keystone_sp": int(char["keystone_sp"]) if char.get("keystone_sp") is not None else None,
    }
    faction = {
        "faction_id": int(fac_raw["faction_id"]) if fac_raw.get("faction_id") is not None else None,
        "faction_name": fac_raw.get("faction_name"),
        "reputation": int(fac_raw["reputation"]) if fac_raw.get("reputation") is not None else None,
    }
    return {
        "available": True,
        "account_id": int(account_id),
        # controller id (already read by EXISTS_SQL) surfaced so server-side
        # consumers can resolve the buyer's controller_id from the authed account
        # (e.g. the portal Market BUY route) without re-querying.
        "player_controller_id": (int(ident["player_controller_id"])
                                 if ident.get("player_controller_id") is not None else None),
        "character": character,
        "economy": economy,
        "faction": faction,
    }


def main():
    # Usage:
    #   dune-player-progress.py <account_id>                  default pick
    #   dune-player-progress.py <account_id> --list           all characters
    #   dune-player-progress.py <account_id> --controller <N> scope to one char
    argv = sys.argv
    usage = ("usage: dune-player-progress.py <account_id> "
             "[--list | --controller <controller_id>]")
    if len(argv) < 2 or not argv[1].isdigit():
        print(json.dumps({"available": False, "error": usage}))
        sys.exit(2)

    account_id = argv[1]
    mode = None
    controller_id = None
    if len(argv) >= 3:
        if argv[2] == "--list" and len(argv) == 3:
            mode = "list"
        elif argv[2] == "--controller" and len(argv) == 4 and argv[3].isdigit():
            controller_id = int(argv[3])
        else:
            print(json.dumps({"available": False, "error": usage}))
            sys.exit(2)

    try:
        if mode == "list":
            result = list_characters(account_id, _query_json)
        else:
            result = build(account_id, _query_json, controller_id=controller_id)
    except Exception as exc:  # noqa: BLE001 - surface any DB error as JSON
        print(json.dumps({"available": False, "error": str(exc)[:500],
                          "account_id": int(account_id)}))
        sys.exit(1)

    print(json.dumps(result))
    sys.exit(0)


if __name__ == "__main__":
    main()
