#!/usr/bin/env python3
"""Public Landsraad live term board for the player portal (term-global).

Read-only. Emits ONE structured JSON blob covering the in-game LANDSRAAD tab:
the term header, the 25 minor-house tiles (claimed/winning state + goal +
sysselraad), per-tile per-faction progress, the great-house score, the top
guild contributors per great house, and each house's reward ladder.

This is TERM-GLOBAL, one row per board, NOT per account. Collect once, cache,
serve to everyone (the admin-backend wraps this in a 30s ttl_cache). The only
per-player layer (a player's voting power + per-tile contribution) is served
separately by the existing per-account landsraad-rewards path, NOT here.

Data model (verified live, term 4 -
LANDSRAAD-BOARD-SPEC-2026-06-12.md):
  dune.landsraad_decree_term , term header (term_id, factions, start/end, test)
  dune.landsraad_tasks       , the 25 tiles (board_index 0..24, house_name,
                                completed, winning_faction_id, sysselraad, goal)
  dune.landsraad_task_faction_contributions , per-tile per-faction progress
  dune.landsraad_task_guild_contributions    , top-guild contributors
  dune.landsraad_task_rewards                 , reward ladders (threshold->item)
  dune.factions , 1 Atreides, 2 Harkonnen, 3 None, 4 Smuggler
  dune.guilds   , guild_id -> guild_name

The portal resolves friendly house + item names (name_lookups / house_crests);
this script emits data only (raw house_name, template_id), matching the
data/presentation split used by dune-landsraad-rewards.py.

Usage: dune-landsraad-board.py   (no args; global board)
"""
import json
import subprocess
import sys
from datetime import datetime, timezone

NS = "funcom-seabass-sh-<your-hostid>-<random>"
DB_POD = "sh-<your-hostid>-<random>-db-dbdepl-sts-0"
DB_PORT = "15432"

# Top-N guild contributors surfaced per great house (in-game
# m_NumberOfGuildsInHighscoreList = 5).
TOP_GUILDS_PER_FACTION = 5


# Player-facing label and description per decree.
#
# The KEY is the game's internal decree_name. Anything not listed here still
# renders -- it falls back to the internal name with no description -- so an
# unrecognised decree from a future build is visibly unknown rather than absent.
#
# MAGNITUDES ARE DELIBERATELY OMITTED until confirmed from a source. A wrong
# number here is worse than no number: this renders on a player-facing page and
# people plan crafting and combat around it.
# confidence values:
#   "confirmed"   - stated in official Funcom material (patch notes / dev post)
#   "community"   - consistent across community sources but NOT in any official
#                   Funcom source. The sources agree verbatim, which suggests one
#                   tooltip transcription and copies rather than independent
#                   confirmation. Treat the number as probable, not verified.
#   "unknown"     - no documented effect
#
# Upgrade path to "confirmed": screenshot the decree tooltip in game while it is
# on the ballot, and replace the confidence value here.
DECREE_INFO = {
    "ExperienceRateIncrease": (
        "Scholarly Collaboration",
        "+50% XP gain faction-wide. Specialization XP is excluded.",
        "community"),
    "RangedDamageIncreased": (
        "In Honor of the Fallen (Ranged)",
        "+33% ranged weapon damage.",
        "community"),
    "MeleeDamageIncreased": (
        "In Honor of the Fallen (Melee)",
        "+33% melee weapon damage.",
        "community"),
    "CraftingCostReduced": (
        "CHOAM Production Contract",
        "-25% crafting and refining material costs, water included. No effect on recipes costing under 4.",
        "community"),
    "DropInventoryOnDefeatActive": (
        "The Right of Salvage",
        "Full-loot PvP: defeated players drop their inventory. Scoped to PvP areas, not the whole Deep Desert.",
        "confirmed"),
    "RepairAndRefiningTimes": (
        "CHOAM Refining Contract",
        "-75% refining duration and repair cost.",
        "community"),
    "SpecialVendorActive": (
        None,
        "Legacy combined vendor decree. Disabled on this server and unreachable in game.",
        "unknown"),
    "SpecialVendorActive_Vehicles": (
        "Exclusive Vehicle Import Rights",
        "Unlocks the Landsraad Vehicle Vendor. One purchase per player per week from an Mk6 pool.",
        "community"),
    "SpecialVendorActive_Weapons": (
        "Exclusive Weapon Import Rights",
        "Unlocks the Landsraad Weapons Vendor. One purchase per player per week.",
        "community"),
    "SpecialVendorActive_Armor": (
        "Exclusive Armor Import Rights",
        "Unlocks the Landsraad Armor Vendor. One purchase per player per week.",
        "community"),
    "SpecialVendorActive_Utilities": (
        "Exclusive Tool Import Rights",
        "Unlocks the Landsraad Tool Vendor. One purchase per player per week from an Mk6 pool.",
        "community"),
}


def _decree(did, decrees):
    """Resolve a decree id to a display object, or None."""
    i = _i(did) if did else None
    if not i:
        return None
    internal = decrees.get(i)
    label, desc, conf = DECREE_INFO.get(internal, (None, None, "unknown"))
    return {
        "id": i,
        "internal_name": internal,
        "label": label or internal,
        "description": desc,
        "confidence": conf,
    }



def psql(sql):
    r = subprocess.run(
        ["sudo", "kubectl", "exec", "-i", "-n", NS, DB_POD, "--", "sh", "-c", '[ -n "${POSTGRES_PASSWORD:-}" ] || { echo "FATAL: POSTGRES_PASSWORD unavailable in DB pod" >&2; exit 3; }; PGPASSWORD=$POSTGRES_PASSWORD exec psql "$@"', "_", "-h", "localhost", "-p", DB_PORT, "-U", "postgres", "-d", "dune",
         "-t", "-A", "-F|", "-v", "ON_ERROR_STOP=1"],
        input=sql, capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(f"psql failed: {r.stderr.strip()}")
    return [ln.split("|") for ln in r.stdout.splitlines() if ln.strip()]


def _i(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def build(run):
    """Assemble the term-global board payload. `run(sql)` runs SQL and returns
    positional rows. Returns the dict the CLI prints (generated_utc is wall-clock,
    excluded from any byte-parity comparison)."""
    # Factions id -> name (1 Atreides, 2 Harkonnen, 3 None, 4 Smuggler).
    factions = {}
    for fid, name in run("SELECT id, name FROM dune.factions ORDER BY id;"):
        factions[str(_i(fid))] = name

    # Decree id -> internal name, straight from the game's own table so a new
    # decree in a future build shows up rather than silently rendering blank.
    decrees = {}
    for did, dname in run("SELECT id, decree_name FROM dune.landsraad_decrees ORDER BY id;"):
        decrees[_i(did)] = dname

    # Current term: the one live now, else the most recent. Epochs are emitted so
    # the portal can render a client-side ticking countdown without re-fetching.
    term_rows = run(
        "SELECT term_id, reigning_faction_id, winning_faction_id, "
        "extract(epoch from start_time)::bigint, extract(epoch from end_time)::bigint, "
        "start_time AT TIME ZONE 'UTC', end_time AT TIME ZONE 'UTC', "
        "COALESCE(test_term, false), active_decree_id, elected_decree_id "
        "FROM dune.landsraad_decree_term "
        "ORDER BY (now() BETWEEN start_time AND end_time) DESC, term_id DESC LIMIT 1;"
    )
    if not term_rows:
        return {"available": True, "term": None, "factions": factions,
                "tiles": [], "score": {}, "top_guilds": {}, "winner_history": [],
                "generated_utc": datetime.now(timezone.utc).isoformat()}
    (tid, reign_fid, win_fid, start_ep, end_ep, start_iso, end_iso, test_term,
     active_did, elected_did) = term_rows[0]
    term_id = _i(tid)
    term = {
        "term_id": term_id,
        "reigning_faction_id": _i(reign_fid) if reign_fid else None,
        "winning_faction_id": _i(win_fid) if win_fid else None,
        "start_epoch": _i(start_ep),
        "end_epoch": _i(end_ep),
        # ISO without zone designator, the portal's time-local.js treats bare
        # timestamps as UTC, which is what these are.
        "start_utc": (start_iso or "").replace(" ", "T"),
        "end_utc": (end_iso or "").replace(" ", "T"),
        "test_term": str(test_term).lower() in ("t", "true"),
        "active_decree": _decree(active_did, decrees),
        "elected_decree": _decree(elected_did, decrees),
    }

    # The 25 tiles. board_index 0..24 -> row = idx//5, col = idx%5.
    tiles = {}
    order = []
    for row in run(
        "SELECT board_index, house_name, completed, winning_faction_id, "
        "COALESCE(sysselraad, false), goal_amount "
        "FROM dune.landsraad_tasks "
        f"WHERE term_id = {term_id} ORDER BY board_index;"
    ):
        bidx, house, completed, win, syss, goal = row
        bi = _i(bidx)
        tiles[bi] = {
            "board_index": bi,
            "house_name": house,
            "completed": str(completed).lower() in ("t", "true"),
            "winning_faction_id": _i(win) if win else None,
            "sysselraad": str(syss).lower() in ("t", "true"),
            "goal_amount": _i(goal),
            "progress": {},   # faction_id(str) -> summed contribution
            "rewards": [],    # {threshold, template_id, amount}
        }
        order.append(bi)

    # Per-tile per-faction progress (both factions race to goal_amount).
    for bidx, fid, amt in run(
        "SELECT t.board_index, fc.faction_id, SUM(fc.amount) "
        "FROM dune.landsraad_tasks t "
        "JOIN dune.landsraad_task_faction_contributions fc ON fc.task_id = t.id "
        f"WHERE t.term_id = {term_id} "
        "GROUP BY t.board_index, fc.faction_id;"
    ):
        ti = tiles.get(_i(bidx))
        if ti is not None and fid:
            ti["progress"][str(_i(fid))] = _i(amt)

    # Reward ladders per tile (threshold -> reward).
    for bidx, threshold, tpl, amount in run(
        "SELECT t.board_index, tr.threshold, tr.template_id, tr.amount "
        "FROM dune.landsraad_tasks t "
        "JOIN dune.landsraad_task_rewards tr ON tr.task_id = t.id "
        f"WHERE t.term_id = {term_id} "
        "ORDER BY t.board_index, tr.threshold;"
    ):
        ti = tiles.get(_i(bidx))
        if ti is not None:
            ti["rewards"].append({
                "threshold": _i(threshold),
                "template_id": tpl,
                "amount": _i(amount),
            })

    # Great-house score: decided tiles grouped by winning faction.
    score = {}
    for fid, cnt in run(
        "SELECT winning_faction_id, count(*) FROM dune.landsraad_tasks "
        f"WHERE term_id = {term_id} AND winning_faction_id IS NOT NULL "
        "GROUP BY winning_faction_id;"
    ):
        if fid:
            score[str(_i(fid))] = _i(cnt)

    # Top guild contributors per great house (summed over the term, top-N each).
    by_faction = {}
    for fid, gname, total in run(
        "SELECT gc.faction_id, g.guild_name, SUM(gc.amount) AS total "
        "FROM dune.landsraad_tasks t "
        "JOIN dune.landsraad_task_guild_contributions gc ON gc.task_id = t.id "
        "JOIN dune.guilds g ON g.guild_id = gc.guild_id "
        f"WHERE t.term_id = {term_id} "
        "GROUP BY gc.faction_id, g.guild_name "
        "ORDER BY gc.faction_id, total DESC;"
    ):
        if not fid:
            continue
        by_faction.setdefault(str(_i(fid)), []).append(
            {"guild": gname, "amount": _i(total)})
    top_guilds = {k: v[:TOP_GUILDS_PER_FACTION] for k, v in by_faction.items()}

    # Recent term winners (newest first), a small "great houses past terms" strip.
    winner_history = []
    for (fid,) in run(
        "SELECT winning_faction_id FROM dune.landsraad_decree_term "
        "WHERE winning_faction_id IS NOT NULL ORDER BY term_id DESC LIMIT 8;"
    ):
        winner_history.append(_i(fid))

    return {
        "available": True,
        "term": term,
        "factions": factions,
        "tiles": [tiles[i] for i in order],
        "score": score,
        "top_guilds": top_guilds,
        "winner_history": winner_history,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
    }


def main():
    try:
        json.dump(build(psql), sys.stdout, separators=(",", ":"))
        sys.stdout.write("\n")
        return 0
    except Exception as exc:
        json.dump({"available": False, "error": str(exc)}, sys.stdout)
        sys.stdout.write("\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
