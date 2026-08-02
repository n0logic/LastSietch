#!/usr/bin/env python3
"""Per-player unclaimed Landsraad rewards for the public player portal.

Read-only. Emits a structured JSON blob (data only; the portal resolves
friendly item names + house labels via name_lookups). For one account_id:
  account_id -> dune.player_state.player_controller_id -> the player_id used
  in dune.landsraad_house_rewards (FK to dune.actors, = the controller actor).

A row in landsraad_house_rewards is unique on (player_id, house_name,
template_id). Collecting at the rep does NOT delete the row: the game's
landsraad_withdraw_house_reward proc UPDATEs amount = amount - withdrawn,
so a fully-collected reward becomes an amount-0 row that stays in the table.
The in-game rep only shows amount > 0 (landsraad_load_house_rewards filters
amount > 0). So we mirror that exactly:
  amount  > 0 -> awaiting pickup  (what the rep shows; houses + summary)
  amount == 0 -> already collected (history; the claimed bucket)
SolarisCoin = Solari currency (template_id).

Usage: dune-landsraad-rewards.py <account_id>
"""
import json
import subprocess
import sys
from datetime import datetime, timezone

NS = "funcom-seabass-sh-<your-hostid>-<random>"
DB_POD = "sh-<your-hostid>-<random>-db-dbdepl-sts-0"

# The Solari currency template; counted separately from item rewards.
SOLARI_TEMPLATE = "SolarisCoin"


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


def resolve_controllers(account_id, run):
    """account_id -> its player_controller_id(s) (usually one)."""
    rows = run(f"SELECT player_controller_id FROM dune.player_state WHERE account_id = {account_id};")
    out = []
    for r in rows:
        try:
            out.append(int(r[0]))
        except (ValueError, IndexError, TypeError):
            pass
    return out


def get_ladders(controllers, run):
    """Current-term Landsraad reward ladder per house (the in-game board view):
    each house's goal + this player's total contribution + the threshold->reward
    ladder with reliable per-reward amounts (from the catalog, unlike the
    awaiting-pickup queue which stores 0 for many stackables)."""
    ctrl_csv = ",".join(str(c) for c in controllers) if controllers else "NULL"
    ladders = {}
    try:
        goal_rows = run(
            "SELECT t.house_name, t.goal_amount, "
            "COALESCE((SELECT SUM(pc.amount) FROM dune.landsraad_task_player_contributions pc "
            f"WHERE pc.task_id = t.id AND pc.player_id IN ({ctrl_csv})), 0) "
            "FROM dune.landsraad_tasks t "
            "WHERE t.term_id = (SELECT max(term_id) FROM dune.landsraad_tasks);"
        )
        for house, goal, contrib in goal_rows:
            ladders[house] = {
                "goal": int(goal or 0),
                "my_contribution": int(float(contrib or 0)),
                "rewards": [],
            }
        reward_rows = run(
            "SELECT t.house_name, tr.threshold, tr.template_id, tr.amount "
            "FROM dune.landsraad_tasks t "
            "JOIN dune.landsraad_task_rewards tr ON tr.task_id = t.id "
            "WHERE t.term_id = (SELECT max(term_id) FROM dune.landsraad_tasks) "
            "ORDER BY t.house_name, tr.threshold;"
        )
        for house, threshold, tid, amount in reward_rows:
            if house in ladders:
                ladders[house]["rewards"].append({
                    "threshold": int(threshold),
                    "template_id": tid,
                    "amount": int(amount),
                })
    except Exception as e:
        print(f"ladder query failed: {e}", file=sys.stderr)
    return ladders


def get_rewards(account_id, run):
    """Return (houses, summary, claimed). houses + summary cover only rewards
    still awaiting pickup (amount > 0, matching what the in-game rep shows);
    claimed is the history bucket of fully-collected rewards (amount == 0),
    grouped by house. Splitting on amount mirrors the game's own filter so the
    portal stays in sync with the rep."""
    rows = run(
        "SELECT lhr.house_name, lhr.template_id, lhr.amount, "
        "       extract(epoch from lhr.last_updated)::bigint "
        "FROM dune.landsraad_house_rewards lhr "
        "JOIN dune.player_state ps ON ps.player_controller_id = lhr.player_id "
        f"WHERE ps.account_id = {account_id} "
        "ORDER BY lhr.house_name ASC, lhr.amount DESC, lhr.last_updated DESC;"
    )

    houses = {}
    claimed = {}
    total_lines = 0
    total_solari = 0
    oldest_epoch = None
    for house_name, template_id, amount, lu in rows:
        amount = int(amount)
        lu = int(lu) if lu else None
        if amount <= 0:
            # already collected at the rep -> history bucket (newest first)
            c = claimed.setdefault(house_name, {
                "house_name": house_name, "items": [],
            })
            c["items"].append({
                "template_id": template_id,
                "claimed_epoch": lu,
            })
            continue
        h = houses.setdefault(house_name, {
            "house_name": house_name, "solari": 0, "line_count": 0, "items": [],
        })
        h["line_count"] += 1
        total_lines += 1
        if oldest_epoch is None or (lu is not None and lu < oldest_epoch):
            oldest_epoch = lu
        if template_id == SOLARI_TEMPLATE:
            h["solari"] += amount
            total_solari += amount
        else:
            h["items"].append({
                "template_id": template_id,
                "amount": amount,
                "last_updated_epoch": lu,
            })

    house_list = sorted(
        houses.values(),
        key=lambda h: (-h["solari"], -h["line_count"], h["house_name"]),
    )
    for c in claimed.values():
        c["items"].sort(key=lambda it: -(it["claimed_epoch"] or 0))
    claimed_list = sorted(
        claimed.values(),
        key=lambda c: (-len(c["items"]), c["house_name"]),
    )
    summary = {
        "total_lines": total_lines,
        "total_solari": total_solari,
        "houses_with_rewards": len(houses),
        "oldest_epoch": oldest_epoch,
        "claimed_lines": sum(len(c["items"]) for c in claimed.values()),
        "claimed_houses": len(claimed),
    }
    return house_list, summary, claimed_list


def build(account_id, run):
    """Assemble the landsraad-rewards payload. `account_id` is an int. `run(sql)`
    runs the SQL and returns positional rows (CLI passes the kubectl `psql`
    runner; the collector passes a psycopg2 rows runner). Returns the same dict
    the CLI prints. `generated_utc` is wall-clock and intentionally excluded from
    any byte-parity comparison."""
    out = {
        "available": True,
        "account_id": account_id,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
    }
    try:
        houses, summary, claimed = get_rewards(account_id, run)
        out["houses"] = houses
        out["summary"] = summary
        out["claimed"] = claimed
        out["ladders"] = get_ladders(resolve_controllers(account_id, run), run)
    except Exception as e:
        print(f"landsraad rewards query failed: {e}", file=sys.stderr)
        out["available"] = False
        out["error"] = "query_failed"
        out["houses"] = []
        out["summary"] = {"total_lines": 0, "total_solari": 0,
                          "houses_with_rewards": 0, "oldest_epoch": None,
                          "claimed_lines": 0, "claimed_houses": 0}
        out["claimed"] = []
        out["ladders"] = {}
    return out


def main():
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        print(json.dumps({"available": False, "error": "bad_account_id"}))
        return 2
    account_id = int(sys.argv[1])
    print(json.dumps(build(account_id, psql)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
