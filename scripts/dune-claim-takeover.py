#!/usr/bin/env python3
"""Adopt an ORPHANED land claim onto an admin's character. DARK BY DEFAULT.

Deployed to <game-host>:/root/dune-claim-takeover.py.

WHAT AN ORPHAN IS
-----------------
A base still standing whose ownership record is gone. dune.permission_actor_rank
.player_id FKs to actors ON DELETE CASCADE, so deleting the owner's character
erases who owned the base while the base stays up. Nobody can be contacted about
them, and nobody can dismantle them, because dismantling needs an owner. 9 exist
as of 2026-07-27, 890 pieces between them.

WHY THIS USES FUNCOM'S OWN PROC
-------------------------------
`dune.permission_actor_takeover(entry, owner_rank)` is the engine's takeover
path, and it is the right primitive for three reasons:

  * It REFUSES when a rank-1 owner already exists. Funcom's own comment says
    "Check if the actor is already owned to avoid exploits". Stealing a live
    player's base is therefore impossible at the database, not merely
    discouraged by this script.
  * It clears stale rank rows before inserting, so leftover co-holders on an
    orphan do not survive the adoption.
  * It ends with pg_notify('permission_notify_channel', 'takeover#{...}'). The
    running server listens on that channel, so this is the engine being told,
    not a synthetic write it may ignore. No restart, no RAM-clobber question.

🔴 DO NOT SUBSTITUTE `permission_set_player_rank`. It looks like the obvious
choice and it is a trap: it has NO ownership check, so pointing it at an owned
totem inserts a SECOND rank-1 row and breaks the uniqueness invariant that the
entire ownership lookup rests on (dune-bases.py resolves an owner by taking the
rank-1 row). Its pg_notify also formats `"Map" : %s` unquoted, so any non-numeric
map id emits invalid JSON.

KNOWN FUNCOM BUG, recorded so nobody re-derives it: both procs resolve the
guild with `SELECT guild_id FROM guild_members WHERE player_id = <ACTOR id>`,
passing the totem's id where a player id belongs. PlayerGuildId is therefore
always 0 and a takeover grants the individual, not their guild. For admin
cleanup that is arguably correct, but it means the new owner's guildmates get
nothing.

SAFETY
------
  * Dark by default. Set LASTSIETCH_CLAIM_TAKEOVER_ENABLED=1 to arm.
  * Orphan-only, checked here as well as in the proc. Belt and braces, because
    the two checks fail differently: ours refuses loudly, the proc's returns
    silently after RAISE NOTICE, which would look like success.
  * Every action writes dune.ls_claim_takeover_log BEFORE the proc call and
    marks it applied after, so a crash in between leaves evidence.
  * Reversible: --revert calls permission_remove_player_rank and logs a NEW row.
  * SET LOCAL search_path TO dune, public is mandatory. Funcom procs use
    unqualified table names in their bodies and error out without it.

Usage:
  dune-claim-takeover.py --check    <totem_id>
  dune-claim-takeover.py --take     <totem_id> --account <account_id> [--operator name]
  dune-claim-takeover.py --register <totem_id> --account <account_id> [--operator name]
  dune-claim-takeover.py --revert   <totem_id> --account <account_id> [--operator name]

--check is always allowed and never writes. The rest require the flag.

--register: A SECOND, DIFFERENT ORPHAN SHAPE (added 2026-08-18)
--------------------------------------------------------------
--take handles a claim that IS in the permission system but has lost its rank-1
owner. There is a second, MORE COMMON shape this tool used to refuse outright: a
totem with NO dune.permission_actor row at all, so it was never registered in the
permission system. Counted 2026-08-18 on live: 39 totems in this state against 6
of the --take kind. Every one of the 137 working, owned totems HAS that row, so
its absence is a real fault and not a variant.

Reported by Drew: base built, standing, full of his storage, and the game told
him he had no permission to build in his own base. Client reboot changed nothing,
because nothing on the client could.

WHY --take CANNOT DO THIS. permission_actor_takeover reads:

    SELECT actor_id FROM permission_actor WHERE actor_id = in_entry.actor_id
      INTO found_actor_id;
    IF NOT FOUND THEN
        PERFORM permission_actor_register(in_entry, in_owner_rank);
        RETURN;                     <-- returns BEFORE its pg_notify
    END IF;

So pointing --take at one of these writes correct rows and then silently skips
telling the running server. The refusal that used to block it was right.

WHAT --register DOES INSTEAD, and why it is two procs:

  1. permission_actor_register(entry, rank) creates the permission_actor row and,
     because is_child is false, the rank-1 row with it. It does NOT notify: its
     own body says so ("there is no pg_notify here as the use cases where it may
     be needed are very low"). Data correct, server unaware.
  2. permission_set_player_rank(totem, player, 1, map_id) then notifies. It is
     safe to call second because it keys on (actor, player): it finds the row
     step 1 just wrote and UPDATEs it rather than inserting a duplicate. It also
     calls permission_actor_create_or_update_base_marker itself, so the base
     appears on the owner's map exactly as a normally-placed one does.

     🔴 The warning elsewhere in this file about permission_set_player_rank
     inserting a SECOND rank-1 row applies to pointing it at a totem owned by a
     DIFFERENT player. For the same player it is an upsert. Do not "fix" that
     warning away, and do not use this proc alone on someone else's claim.

The entry's actor_name / actor_type / access_level are NOT invented: they are
read from whatever registered, owned totems of the SAME actor class use, so a
repaired totem is shaped like its healthy siblings. Falls back loudly rather
than guessing when the class has no registered example.

SAFETY: --register refuses whenever a permission_actor row EXISTS, which is the
exact inverse of --take. It therefore cannot be pointed at a healthy base at all,
and the two modes can never both apply to the same totem.
"""
import json
import os
import subprocess
import sys

FLAG = "LASTSIETCH_CLAIM_TAKEOVER_ENABLED"

# Taking a claim from a player who STILL EXISTS is a different act from adopting
# an orphan, and it gets its own flag so it can never happen as a side effect of
# arming the orphan path. An orphan has nobody to contact; an owned claim has a
# person who may be one Discord message away. Requiring --force-owned AND a
# second environment variable means an override is always three deliberate
# decisions: arm, allow, and name the base.
#
# permission_actor_takeover refuses outright when a rank-1 row exists ("to avoid
# exploits"), so the override composes Funcom's own procs rather than defeating
# them: remove the incumbent's rank, then take over the now-unowned actor. The
# prior owner is recorded first, because the game DB keeps NO ownership history
# and after the swap nothing anywhere remembers who held it.
FLAG_OWNED = "LASTSIETCH_CLAIM_TAKEOVER_ALLOW_OWNED"

# --keep-as-coholder is the gentler override: the admin takes rank 1 so the base
# can be managed, and the previous owner is re-added at rank 2 so they keep build
# and access rights instead of being locked out of their own base. Two notes:
#
#   * takeover DELETES every rank row before inserting, so the co-holder has to
#     be re-added AFTER, not before.
#   * re-adding uses permission_set_player_rank, whose pg_notify formats
#     `"Map" : %s` UNQUOTED. Pass the NUMERIC map_name_id (Hagga = 11), never
#     the map name, or the engine receives invalid JSON.
MAP_NAME_IDS = {"HaggaBasin": 11, "DeepDesert": 7, "Arrakeen": 1,
                "HarkoVillage": 9}

# The game caps a player at 3 land claims. Verified across 96 holders: 66 hold 1,
# 20 hold 2, 10 hold 3, nobody holds 4. The DB does not enforce it, so an admin
# adopting past the cap would be the first over-cap state this server has seen.
CLAIM_CAP = 3

# Who a claim may be adopted ONTO. Deliberately a short allowlist rather than
# "any account": adopting is a write against a real player's base, and the set of
# people entitled to hold one is small and stable. Edit here to change it.
#
# Cielago (the support bot's own character) is included on purpose. It holds no
# bases of its own, so it is the natural neutral custodian for a reclaimed plot:
# a base parked there is visibly the server's, not any one admin's.
OPERATORS = [
    # (account_id, display_name) of accounts a claim may be adopted onto.
    # Empty by default: fill in your own. A bot or service character that
    # holds no bases makes a good neutral custodian, since a base parked
    # there is visibly the server's rather than any one admin's.
]

# Live headroom per operator. The cap is what makes this worth showing in a
# picker at all: an admin at 3 of 3 cannot adopt, and finding that out after
# clicking is a worse experience than seeing it in the list.
OPERATORS_SQL = """
SELECT coalesce(json_agg(json_build_object(
         'account_id', ps.account_id,
         'name', ps.character_name,
         'claims', (SELECT count(*) FROM dune.permission_actor_rank r
                      JOIN dune.totems t ON t.id = r.permission_actor_id
                     WHERE r.rank = 1 AND r.player_id = ps.player_controller_id),
         'backups', (SELECT count(*) FROM dune.base_backups bb
                      WHERE bb.player_id = ps.player_controller_id),
         'cap', {cap}
       ) ORDER BY ps.character_name), '[]'::json)
FROM dune.player_state ps
WHERE ps.account_id IN ({ids});
"""

# Everything the eligibility decision needs, in one round trip. The rank-1
# probe is the orphan test: absence of a rank-1 row IS what orphaned means.
CHECK_SQL = """
SELECT json_build_object(
  'totem_id', t.id,
  'is_totem', true,
  'label', coalesce(pa.actor_name, ''),
  'actor_class', a.class,
  'actor_type', pa.actor_type,
  'access_level', pa.access_level,
  'is_child', pa.is_child,
  'has_permission_actor', (pa.actor_id IS NOT NULL),
  'map', a.map,
  'dimension_index', a.dimension_index,
  'x', round((((a.transform).location).x)::numeric, 0),
  'y', round((((a.transform).location).y)::numeric, 0),
  'actor_state', a.state::text,
  'rank_rows', (SELECT count(*) FROM dune.permission_actor_rank r
                 WHERE r.permission_actor_id = t.id),
  'owner_player_id', (SELECT r.player_id FROM dune.permission_actor_rank r
                       WHERE r.permission_actor_id = t.id AND r.rank = 1 LIMIT 1),
  'pieces', (SELECT count(*) FROM dune.actor_fgl_entities afe
               JOIN dune.building_instances bi ON bi.owner_entity_id = afe.entity_id
              WHERE afe.actor_id = t.id AND afe.slot_name = 'Actor'),
  'placeables', (SELECT count(*) FROM dune.actor_fgl_entities afe
                   JOIN dune.placeables p ON p.owner_entity_id = afe.entity_id
                  WHERE afe.actor_id = t.id AND afe.slot_name = 'Actor')
)
FROM dune.totems t
JOIN dune.actors a ON a.id = t.id
LEFT JOIN dune.permission_actor pa ON pa.actor_id = t.id
WHERE t.id = %(totem)s;
"""

ACCOUNT_SQL = """
SELECT json_build_object(
  'account_id', ps.account_id,
  'character_name', ps.character_name,
  'player_controller_id', ps.player_controller_id,
  'online', ps.online_status = 'Online')
FROM dune.player_state ps WHERE ps.account_id = %(acct)s;
"""


def run_sql(sql, timeout=45):
    out = subprocess.run(["/root/dq.sh", "-tAc", sql],
                         capture_output=True, text=True, timeout=timeout)
    text = (out.stdout or "")
    if text.startswith("SET\n"):
        text = text[4:]
    if out.returncode != 0:
        return None, (out.stderr or out.stdout).strip()[:400]
    return text.strip(), None


def query_json(sql, **subs):
    for k, v in subs.items():
        sql = sql.replace("%(" + k + ")s", str(v))
    raw, err = run_sql(sql)
    if err:
        return None, err
    if not raw:
        return None, "no rows"
    try:
        return json.loads(raw), None
    except json.JSONDecodeError:
        return None, "bad json: " + raw[:200]


def emit(obj, code=0):
    print(json.dumps(obj, indent=2))
    return code


def sq(s):
    """Single-quote a literal for inline SQL. dq.sh is psql -c, not a driver."""
    return "'" + str(s).replace("'", "''") + "'"


# The permission_actor entry for a repaired totem is COPIED from healthy
# siblings of the same actor class rather than invented. Live shapes 2026-08-18:
# BP_Totem_C -> ('##Totem_Placeable', type 3, access 3) on 99 of 112; the small
# variant -> ('##Totem_Small_Placeable', type 4). Guessing type would be the one
# field with teeth: permission_actor_create_or_update_base_marker only emits a
# HomeBase marker for actor_type 3 or 4, so a wrong type yields a silently
# marker-less base.
SHAPE_SQL = """
SELECT json_build_object('actor_name', pa.actor_name, 'actor_type', pa.actor_type,
                         'access_level', pa.access_level, 'examples', count(*))
  FROM dune.actors a
  JOIN dune.permission_actor pa ON pa.actor_id = a.id
  JOIN dune.permission_actor_rank r ON r.permission_actor_id = a.id AND r.rank = 1
 WHERE a.class = (SELECT class FROM dune.actors WHERE id = %(totem)s)
 GROUP BY pa.actor_name, pa.actor_type, pa.access_level
 ORDER BY count(*) DESC
 LIMIT 1;
"""

# Resolved from the ACTOR's own map, keyed on the numeric totem id, rather than
# by passing the map NAME as a parameter. query_json() substitutes with a bare
# str(v) and no quoting, so a string parameter lands unquoted and Postgres reads
# it as a column reference. Every other caller passes numbers, which is why that
# had never bitten; this was the first string one and it cost a failed run.
# Joining also means the map can never disagree with the actor row.
MAP_ID_SQL = """
SELECT json_build_object('map_name_id', mn.map_name_id)
  FROM dune.actors a
  JOIN dune.map_names mn ON mn.map_name = a.map
 WHERE a.id = %(totem)s;
"""


def register_eligibility(claim):
    """Refusal string, or None when this claim may be REGISTERED.

    Deliberately the inverse of eligibility(): --register requires the
    permission_actor row to be ABSENT, --take requires it PRESENT. The two can
    never both apply, so there is no case where an operator has to choose.
    """
    if claim.get("actor_state") == "BaseBackup":
        return ("this is a STORED BACKUP, not a claim on the ground.")
    if not claim.get("is_totem"):
        return ("not a totem. Registering arbitrary actors is a wider operation "
                "than this was reasoned about.")
    if claim.get("has_permission_actor"):
        return ("a permission_actor row already EXISTS. This claim is in the "
                "permission system; if it has no owner use --take, and if it has "
                "one it is healthy and needs nothing.")
    if claim.get("owner_player_id") is not None:
        return ("this claim already has a rank-1 owner (player_id %s)."
                % claim["owner_player_id"])
    return None


def eligibility(claim):
    """Returns a refusal string, or None when the claim may be adopted."""
    if claim.get("actor_state") == "BaseBackup":
        return ("this is a STORED BACKUP, not a claim on the ground. It sits in "
                "the reconstruction tool and nothing is built there.")
    if not claim.get("has_permission_actor"):
        return ("no permission_actor row: takeover would fall through to "
                "permission_actor_register, which is a different operation than "
                "this tool was reasoned about. Refusing.")
    if claim.get("owner_player_id") is not None:
        return ("this claim is OWNED (rank-1 player_id %s). Only orphans may be "
                "adopted. The proc would refuse too, but silently."
                % claim["owner_player_id"])
    return None


def main():
    args = sys.argv[1:]
    if not args:
        return emit({"ok": False, "error": __doc__.strip().splitlines()[-4].strip()}, 2)

    def val(flag):
        return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else None

    if "--operators" in args:
        sql = OPERATORS_SQL.format(
            cap=CLAIM_CAP, ids=", ".join(str(a) for a, _ in OPERATORS))
        raw, err = run_sql(sql)
        if err:
            return emit({"ok": False, "error": err}, 1)
        try:
            rows = json.loads(raw or "[]")
        except json.JSONDecodeError:
            rows = []
        for r in rows:
            r["free_slots"] = max(0, CLAIM_CAP - (r.get("claims") or 0))
        return emit({"ok": True, "operators": rows})

    mode = None
    for m in ("--check", "--take", "--register", "--revert"):
        if m in args:
            mode = m
            break
    if mode is None:
        return emit({"ok": False, "error":
                     "one of --check / --take / --register / --revert is required"}, 2)

    try:
        totem = int(val(mode))
    except (TypeError, ValueError):
        return emit({"ok": False, "error": "%s needs a numeric totem id" % mode}, 2)

    operator = val("--operator") or os.environ.get("SUDO_USER") or "admin"

    claim, err = query_json(CHECK_SQL, totem=totem)
    if err:
        return emit({"ok": False, "error": "claim lookup failed: " + err}, 1)

    refusal = (register_eligibility(claim) if mode == "--register"
               else eligibility(claim))

    if mode == "--check":
        # Report BOTH paths. The two are mutually exclusive by construction, so
        # this tells an operator which one applies instead of making them infer
        # it from a refusal string.
        take_refusal = eligibility(claim)
        reg_refusal = register_eligibility(claim)
        return emit({"ok": True, "mode": "check", "claim": claim,
                     "eligible": take_refusal is None,
                     "refusal": take_refusal,
                     "register_eligible": reg_refusal is None,
                     "register_refusal": reg_refusal,
                     "armed": os.environ.get(FLAG) == "1"})

    # --- writes from here ---------------------------------------------------

    if os.environ.get(FLAG) != "1":
        return emit({"ok": False, "dark": True,
                     "error": "%s is not set to 1; refusing to write. "
                              "--check works without it." % FLAG}, 3)

    try:
        acct = int(val("--account"))
    except (TypeError, ValueError):
        return emit({"ok": False, "error": "--account <account_id> is required"}, 2)

    who, err = query_json(ACCOUNT_SQL, acct=acct)
    if err:
        return emit({"ok": False, "error": "account lookup failed: " + err}, 1)
    pcid = who.get("player_controller_id")
    if not pcid:
        return emit({"ok": False, "error":
                     "account %s has no player_controller_id" % acct}, 1)

    if mode == "--register":
        if refusal:
            return emit({"ok": False, "error": refusal, "claim": claim}, 1)
        held, _ = query_json(
            "SELECT json_build_object('n', count(*)) FROM dune.permission_actor_rank r"
            " JOIN dune.totems t ON t.id = r.permission_actor_id"
            " WHERE r.rank = 1 AND r.player_id = %(pid)s;", pid=pcid)
        n_held = (held or {}).get("n", 0)
        if n_held >= CLAIM_CAP:
            return emit({"ok": False, "error":
                         "account %s already holds %d of %d land claims."
                         % (acct, n_held, CLAIM_CAP)}, 1)

    shape_used = None
    force_owned = "--force-owned" in args
    prior_pcid = claim.get("owner_player_id")
    prior_name = None

    if mode == "--take" and refusal:
        owned_only = (prior_pcid is not None
                      and claim.get("actor_state") != "BaseBackup"
                      and claim.get("has_permission_actor"))
        if not (owned_only and force_owned):
            return emit({"ok": False, "error": refusal, "claim": claim,
                         "hint": ("pass --force-owned and set %s=1 to override on an "
                                  "OWNED claim" % FLAG_OWNED) if owned_only else None}, 1)
        if os.environ.get(FLAG_OWNED) != "1":
            return emit({"ok": False, "error":
                         "--force-owned given but %s is not 1. Taking a live "
                         "player's claim needs both." % FLAG_OWNED,
                         "claim": claim}, 3)
        if prior_pcid == pcid:
            return emit({"ok": False, "error":
                         "account %s already owns this claim" % acct}, 1)
        held, _ = query_json(
            "SELECT json_build_object('n', count(*)) FROM dune.permission_actor_rank r"
            " JOIN dune.totems t ON t.id = r.permission_actor_id"
            " WHERE r.rank = 1 AND r.player_id = %(pid)s;", pid=pcid)
        n_held = (held or {}).get("n", 0)
        if n_held >= CLAIM_CAP:
            return emit({"ok": False, "error":
                         "account %s already holds %d of %d land claims. Free a "
                         "slot before adopting another; the engine has never "
                         "seen an over-cap holder and this is not the place to "
                         "find out what it does." % (acct, n_held, CLAIM_CAP)}, 1)
        who_prior, err = query_json(
            "SELECT json_build_object('character_name', ps.character_name,"
            " 'account_id', ps.account_id, 'days_away',"
            " extract(day FROM (now()-ps.last_login_time))::int)"
            " FROM dune.player_state ps WHERE ps.player_controller_id = %(pid)s;",
            pid=prior_pcid)
        prior_name = (who_prior or {}).get("character_name")

    # A revert on a claim we took FROM someone restores them rather than leaving
    # it orphaned. Removing our rank alone would strand a real player's base in
    # the state we created for bases whose owners no longer exist.
    restore_to = None
    if mode == "--revert":
        prev, err = query_json(
            "SELECT json_build_object('pid', prior_owner_player_id,"
            " 'name', prior_owner_name) FROM dune.ls_claim_takeover_log"
            " WHERE totem_id = %(tid)s AND action = 'takeover'"
            "   AND prior_owner_player_id IS NOT NULL"
            " ORDER BY id DESC LIMIT 1;", tid=totem)
        if prev and prev.get("pid"):
            restore_to = int(prev["pid"])
            prior_name = prev.get("name")

    # One transaction: the audit row and the proc call live or die together, and
    # search_path is set inside it because Funcom proc bodies use unqualified
    # table names and error out without it.
    if mode == "--take":
        # Clear the incumbent first: takeover refuses outright while a rank-1
        # row exists, and it refuses SILENTLY (RAISE NOTICE then return), which
        # would look exactly like success.
        pre = ("PERFORM dune.permission_remove_player_rank({tid}, {old});".format(
            tid=totem, old=prior_pcid)) if (force_owned and prior_pcid) else ""
        call = pre + ("PERFORM dune.permission_actor_takeover("
                "ROW({tid}, {name}, {cls}, {atype}::smallint, {acc}::smallint, {child})"
                "::dune.actorpermissionentry, "
                "ROW(1::smallint, {pcid})::dune.actorpermissionrankdata);").format(
            tid=totem, name=sq(claim.get("label") or ""),
            cls=sq(claim.get("actor_class") or ""),
            atype=claim.get("actor_type"), acc=claim.get("access_level"),
            child="true" if claim.get("is_child") else "false", pcid=pcid)
        # takeover does NOT create the base marker; permission_set_player_rank is
        # the proc that does. Call it explicitly so an adopted claim behaves like
        # a normally-owned one and shows up on the new owner's map.
        marker = ("PERFORM dune.permission_actor_create_or_update_base_marker("
                  "{tid}, {pcid}, 1::smallint);".format(tid=totem, pcid=pcid))
        if "--keep-as-coholder" in args and prior_pcid:
            marker += ("PERFORM dune.permission_set_player_rank({tid}, {old},"
                       " 2::smallint, {mid});").format(
                tid=totem, old=prior_pcid,
                mid=sq(str(MAP_NAME_IDS.get(claim.get("map"), 11))))
        verify = ("SELECT player_id FROM dune.permission_actor_rank "
                  "WHERE permission_actor_id = %d AND rank = 1" % totem)
    elif mode == "--register":
        # Shape copied from healthy siblings of the same actor class, never
        # invented. Refuse loudly if the class has no registered example: a
        # guessed actor_type silently costs the base its map marker.
        shape, err = query_json(SHAPE_SQL, totem=totem)
        if err or not shape or shape.get("actor_type") is None:
            return emit({"ok": False, "error":
                         "no registered totem of class %r to copy a permission "
                         "entry shape from; refusing to invent one."
                         % (claim.get("actor_class") or "?"),
                         "detail": err}, 1)
        mapinfo, _ = query_json(MAP_ID_SQL, totem=totem)
        map_id = (mapinfo or {}).get("map_name_id")
        if map_id is None:
            return emit({"ok": False, "error":
                         "no map_name_id for map %r; permission_set_player_rank "
                         "formats \"Map\" UNQUOTED, so a non-numeric value emits "
                         "invalid JSON to the engine." % claim.get("map")}, 1)
        shape_used = shape
        # 1. register: creates permission_actor AND (is_child false) the rank-1
        #    row. Does not notify.
        # dune.actorpermissionentry is SIX fields and the third is actor_class:
        #   (actor_id, actor_name, actor_class, actor_type, access_level, is_child)
        # The permission_actor TABLE has only five columns and no actor_class, so
        # building this ROW from the table's shape silently omits one and Postgres
        # reports it as "cannot cast type boolean to smallint in column 5" -- an
        # error that points at the wrong field entirely. Mirror the --take call
        # above, which already has this right.
        call = ("PERFORM dune.permission_actor_register("
                "ROW({tid}, {name}, {cls}, {atype}::smallint, {acc}::smallint, false)"
                "::dune.actorpermissionentry, "
                "ROW(1::smallint, {pcid})::dune.actorpermissionrankdata);").format(
            tid=totem, name=sq(shape.get("actor_name") or ""),
            cls=sq(claim.get("actor_class") or ""),
            atype=int(shape["actor_type"]), acc=int(shape["access_level"]),
            pcid=pcid)
        # 2. set_player_rank: upserts the SAME (actor, player) row written above,
        #    builds the base marker, and pg_notifies the running server. This is
        #    the step register lacks, and the reason a bare register would leave
        #    the DB correct and the live server none the wiser.
        marker = ("PERFORM dune.permission_set_player_rank("
                  "{tid}, {pcid}, 1::smallint, {mid});").format(
            tid=totem, pcid=pcid, mid=sq(str(int(map_id))))
        verify = ("SELECT player_id FROM dune.permission_actor_rank "
                  "WHERE permission_actor_id = %d AND rank = 1" % totem)
    else:
        call = ("PERFORM dune.permission_remove_player_rank({tid}, {pcid});"
                .format(tid=totem, pcid=pcid))
        if restore_to:
            call += ("PERFORM dune.permission_actor_takeover("
                     "ROW({tid}, {name}, {cls}, {atype}::smallint, {acc}::smallint, {child})"
                     "::dune.actorpermissionentry, "
                     "ROW(1::smallint, {old})::dune.actorpermissionrankdata);"
                     "PERFORM dune.permission_actor_create_or_update_base_marker("
                     "{tid}, {old}, 1::smallint);").format(
                tid=totem, name=sq(claim.get("label") or ""),
                cls=sq(claim.get("actor_class") or ""),
                atype=claim.get("actor_type"), acc=claim.get("access_level"),
                child="true" if claim.get("is_child") else "false", old=restore_to)
        marker = ""
        verify = ("SELECT player_id FROM dune.permission_actor_rank "
                  "WHERE permission_actor_id = %d AND rank = 1" % totem)

    action = {"--take": "takeover", "--register": "register"}.get(mode, "revert")
    sql = """
DO $$
DECLARE v_log_id bigint; v_after bigint;
BEGIN
  SET LOCAL search_path TO dune, public;

  INSERT INTO dune.ls_claim_takeover_log
    (totem_id, action, account_id, player_id, character_name, map,
     dimension_index, world_x, world_y, pieces, placeables, prior_ownership, operator,
     prior_owner_player_id, prior_owner_name)
  VALUES ({tid}, {action}, {acct}, {pcid}, {cname}, {map}, {dim}, {wx}, {wy},
          {pieces}, {placeables}, {prior}, {op}, {ppid}, {pname})
  RETURNING id INTO v_log_id;

  {call}
  {marker}

  {verify} INTO v_after;

  UPDATE dune.ls_claim_takeover_log
     SET applied_at = now(),
         outcome = CASE WHEN {expect} THEN 'ok' ELSE 'no_effect' END,
         detail = 'rank1_after=' || coalesce(v_after::text, 'none')
   WHERE id = v_log_id;

  RAISE NOTICE 'log_id=% rank1_after=%', v_log_id, coalesce(v_after::text, 'none');
END $$;
""".format(
        tid=totem, action=sq(action), acct=acct, pcid=pcid,
        cname=sq(who.get("character_name") or ""), map=sq(claim.get("map") or ""),
        dim=claim.get("dimension_index"), wx=claim.get("x"), wy=claim.get("y"),
        pieces=claim.get("pieces"), placeables=claim.get("placeables"),
        prior=sq("orphaned" if claim.get("owner_player_id") is None else "owned"),
        op=sq(operator), call=call, marker=marker, verify=verify,
        ppid=(prior_pcid if (mode == "--take" and force_owned and prior_pcid) else "NULL"),
        pname=(sq(prior_name) if prior_name else "NULL"),
        expect=(("v_after = %d" % pcid) if mode in ("--take", "--register")
                else ("v_after = %d" % restore_to) if restore_to else "v_after IS NULL"))

    out = subprocess.run(["/root/dq.sh", "-c", sql],
                         capture_output=True, text=True, timeout=60)
    combined = ((out.stdout or "") + (out.stderr or "")).strip()
    if out.returncode != 0:
        return emit({"ok": False, "error": "proc call failed", "detail": combined[:600]}, 1)

    after, err = query_json(CHECK_SQL, totem=totem)
    if mode in ("--take", "--register"):
        ok = after.get("owner_player_id") == pcid
    elif restore_to:
        ok = after.get("owner_player_id") == restore_to
    else:
        ok = after.get("owner_player_id") is None
    return emit({"ok": ok, "mode": action, "totem_id": totem,
                 "account_id": acct, "player_controller_id": pcid,
                 "character_name": who.get("character_name"),
                 "forced_owned": bool(force_owned and mode == "--take"),
                 "prior_owner": prior_name, "restored_to_player_id": restore_to,
                 "permission_entry_used": shape_used,
                 "owner_player_id_after": after.get("owner_player_id"),
                 "rank_rows_after": after.get("rank_rows"),
                 "db_notice": combined[:400]}, 0 if ok else 1)


if __name__ == "__main__":
    sys.exit(main())
