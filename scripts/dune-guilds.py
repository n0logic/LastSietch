#!/usr/bin/env python3
"""Guild directory for the public player portal Guild/Recruitment section.

Read-only. Emits one JSON blob with every guild, its members (resolved to
in-game character names), and Landsraad contribution totals both overall
(all weekly terms) and for the current term ("this session"). The portal
merges this with the portal-side recruiting metadata (open flag + blurb +
contact note) and computes the rankings / contact display.

Data model (see memory our internal notes):
  dune.guilds(guild_id, guild_name, guild_description, guild_faction)
  dune.guild_members(player_id = player_controller_id, guild_id, role_id)
    role_id: 100=Leader, 50=Officer, 1=Member (higher = higher rank)
  member -> name: dune.player_state.player_controller_id -> character_name
  dune.factions: 1 Atreides, 2 Harkonnen, 3 None, 4 Smuggler
  ranking: landsraad_task_guild_contributions(guild_id, task_id, amount)
    task_id -> landsraad_tasks.id -> term_id ; current term via
    landsraad_decree_term WHERE now() BETWEEN start_time AND end_time.

Usage:
  dune-guilds.py                        global guild directory (default)
  dune-guilds.py pending-invites <acct> a player's own pending guild invites
  dune-guilds.py member-census <guild>  online-state census of one guild's roster

The pending-invites / member-census subcommands are P0 read helpers backing the
portal Guilds surface. Both are READ-ONLY (proc calls that only SELECT):
  - pending-invites resolves the caller's controller_id from account_id with the
    same tombstone-safe query the grant/market paths use (excludes 'Deleted'
    characters, deterministic ordering) then calls dune.get_player_guild_invites.
  - member-census calls dune.get_all_player_in_guild_online_state(guild_id). The
    admin-backend enforces caller-must-be-a-member BEFORE invoking this.
"""
import json
import subprocess
import sys
from datetime import datetime, timezone

NS = "funcom-seabass-sh-<your-hostid>-<random>"
DB_POD = "sh-<your-hostid>-<random>-db-dbdepl-sts-0"
DB_PORT = "15432"


def psql(sql):
    pw = subprocess.run(
        ["sudo", "kubectl", "exec", "-n", NS, DB_POD, "--", "printenv", "POSTGRES_PASSWORD"],
        capture_output=True, text=True, timeout=60,
    ).stdout.strip()
    r = subprocess.run(
        ["sudo", "kubectl", "exec", "-i", "-n", NS, DB_POD, "--", "env", f"PGPASSWORD={pw}",
         "psql", "-h", "localhost", "-p", DB_PORT, "-U", "postgres", "-d", "dune",
         "-t", "-A", "-F|", "-v", "ON_ERROR_STOP=1"],
        input=sql, capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(f"psql failed: {r.stderr.strip()}")
    return [ln.split("|") for ln in r.stdout.splitlines() if ln.strip()]


def psql_json(sql, fallback="null"):
    """Run SQL that returns a single JSON cell; return the parsed value. Used by
    the proc-backed read subcommands (they aggregate rows into one json cell)."""
    rows = psql(sql)
    raw = ""
    for row in rows:
        cell = "|".join(row).strip()
        if cell:
            raw = cell
    if not raw:
        raw = fallback
    return json.loads(raw)


def emit_directory():
    # Factions lookup
    factions = {}
    for fid, name in psql("SELECT id, name FROM dune.factions ORDER BY id;"):
        factions[fid] = name

    # Current Landsraad term (the "session"). May be empty between cycles.
    current_term = None
    term_rows = psql(
        "SELECT term_id, start_time, end_time FROM dune.landsraad_decree_term "
        "WHERE now() BETWEEN start_time AND end_time ORDER BY term_id DESC LIMIT 1;"
    )
    if term_rows:
        tid, start, end = term_rows[0]
        current_term = {"term_id": int(tid), "start_time": start, "end_time": end}

    # Guilds
    guilds = {}
    for gid, gname, gdesc, gfac in psql(
        "SELECT guild_id, guild_name, COALESCE(guild_description, ''), guild_faction "
        "FROM dune.guilds ORDER BY guild_id;"
    ):
        guilds[gid] = {
            "guild_id": int(gid),
            "guild_name": gname,
            "guild_description": gdesc,
            "guild_faction": int(gfac) if gfac else 0,
            "members": [],
            "member_count": 0,
            "contrib_overall": 0.0,
            "contrib_session": 0.0,
        }

    # Members (resolved to character names + account_id for portal authz).
    # player_id = player_controller_id; account_id lets the portal match the
    # logged-in session (keyed on account_id) to a Leader/Officer role.
    for gid, pid, role, cname, acct in psql(
        "SELECT m.guild_id, m.player_id, m.role_id, "
        "COALESCE(ps.character_name, ''), COALESCE(ps.account_id::text, '') "
        "FROM dune.guild_members m "
        "LEFT JOIN dune.player_state ps ON ps.player_controller_id = m.player_id "
        "ORDER BY m.guild_id, m.role_id, ps.character_name;"
    ):
        g = guilds.get(gid)
        if not g:
            continue
        g["members"].append({
            "player_id": int(pid),
            "account_id": int(acct) if acct else None,
            "char_name": cname,
            # Default a missing role_id to 1 (Member), the lowest rank. The old
            # default of 100 dated from the reversed role-code assumption; under
            # the correct mapping 100 = Leader, so defaulting to it would wrongly
            # promote a member with a null role.
            "role_id": int(role) if role else 1,
        })
        g["member_count"] += 1

    # Overall contributions (all non-test terms)
    for gid, amt in psql(
        "SELECT c.guild_id, SUM(c.amount) "
        "FROM dune.landsraad_task_guild_contributions c "
        "JOIN dune.landsraad_tasks t ON t.id = c.task_id "
        "JOIN dune.landsraad_decree_term dt ON dt.term_id = t.term_id "
        "WHERE dt.test_term = false GROUP BY c.guild_id;"
    ):
        if gid in guilds:
            guilds[gid]["contrib_overall"] = float(amt) if amt else 0.0

    # Session contributions (current term only)
    if current_term:
        for gid, amt in psql(
            "SELECT c.guild_id, SUM(c.amount) "
            "FROM dune.landsraad_task_guild_contributions c "
            "JOIN dune.landsraad_tasks t ON t.id = c.task_id "
            f"WHERE t.term_id = {current_term['term_id']} GROUP BY c.guild_id;"
        ):
            if gid in guilds:
                guilds[gid]["contrib_session"] = float(amt) if amt else 0.0

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "current_term": current_term,
        "factions": factions,
        "guilds": list(guilds.values()),
    }
    json.dump(out, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")


# Tombstone-safe controller resolver: mirrors the grant/market read path — the
# most-recent NON-Deleted character for the account, deterministic tie-break.
def resolve_controller(account_id):
    rows = psql(
        "SELECT player_controller_id FROM dune.encrypted_player_state "
        f"WHERE account_id = {account_id}::bigint "
        "AND character_state IS DISTINCT FROM 'Deleted' "
        "ORDER BY last_avatar_activity DESC NULLS LAST, "
        "player_controller_id DESC LIMIT 1;"
    )
    for row in rows:
        cell = "|".join(row).strip()
        if cell:
            return int(cell)
    return None


def emit_pending_invites(account_id):
    """A player's own pending guild invites (dune.get_player_guild_invites).
    account_id is resolved to controller_id server-side (tombstone-safe)."""
    ctrl = resolve_controller(account_id)
    if ctrl is None:
        json.dump({"available": False, "error": "account_not_found",
                   "account_id": int(account_id), "invites": []}, sys.stdout)
        sys.stdout.write("\n")
        return
    invites = psql_json(
        "SET search_path TO dune, public; "
        "SELECT coalesce(json_agg(json_build_object("
        "'invite_id', invite_id, 'guild_id', guild_id, 'guild_name', guild_name, "
        "'guild_description', guild_description, 'sender_player_id', sender_player_id, "
        "'invite_sent_timespan', invite_sent_timespan, 'character_name', character_name, "
        "'sender_character_name', sender_character_name)), '[]'::json) "
        f"FROM dune.get_player_guild_invites({ctrl}::bigint);",
        fallback="[]")
    json.dump({"available": True, "account_id": int(account_id),
               "player_controller_id": ctrl, "invites": invites},
              sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")


def emit_member_census(guild_id):
    """Online-state census of one guild's roster (dune.get_all_player_in_guild_
    online_state). The proc returns a named composite; the alias list renames its
    columns positionally so the JSON shape is stable regardless of the type's
    attribute names. account_id is added by joining encrypted_player_state so the
    admin-backend can flag the session player's own row (is_self)."""
    members = psql_json(
        "SET search_path TO dune, public; "
        "SELECT coalesce(json_agg(json_build_object("
        "'player_controller_id', c.player_controller_id, "
        "'account_id', eps.account_id, "
        "'character_name', c.character_name, "
        "'server_info', to_jsonb(c.server_info), "
        "'last_activity', c.last_activity, "
        "'online_status', c.online_status) ORDER BY c.character_name), '[]'::json) "
        f"FROM dune.get_all_player_in_guild_online_state({guild_id}::bigint) "
        "AS c(player_controller_id, character_name, server_info, last_activity, online_status) "
        "LEFT JOIN dune.encrypted_player_state eps "
        "ON eps.player_controller_id = c.player_controller_id "
        "AND eps.character_state IS DISTINCT FROM 'Deleted';",
        fallback="[]")
    json.dump({"available": True, "guild_id": int(guild_id), "members": members},
              sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")


def main():
    argv = sys.argv[1:]
    if not argv:
        emit_directory()
        return
    sub = argv[0]
    if sub == "pending-invites":
        if len(argv) != 2 or not argv[1].isdigit():
            raise SystemExit("usage: dune-guilds.py pending-invites <account_id>")
        emit_pending_invites(argv[1])
    elif sub == "member-census":
        if len(argv) != 2 or not argv[1].isdigit():
            raise SystemExit("usage: dune-guilds.py member-census <guild_id>")
        emit_member_census(argv[1])
    else:
        raise SystemExit(f"unknown subcommand: {sub}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        json.dump({"error": str(exc)}, sys.stdout)
        sys.stdout.write("\n")
        sys.exit(1)
