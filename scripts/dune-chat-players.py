#!/usr/bin/env python3
# Read-only online-player list WITH funcom_id, for the admin chat whisper picker.
# Deployed to lastsietch-dune:/root/dune-chat-players.py — invoked by the relay over SSH
# (dispatcher action `chat-players`). No writes.
#
# PII rule: emits character names + FuncomIds. The relay endpoint that proxies
# this is admin-auth-gated; never expose on a public surface.
#
# dune.player_state is the decrypting VIEW carrying account_id + online_status.
# It exposes ALL characters (not online-only, despite older comments), so filter
# online_status='Online' explicitly. Whisper only delivers to ONLINE recipients,
# so an online-only picker is exactly right. Join dune.accounts (acc.id =
# ps.account_id) for funcom_id.
import json
import subprocess

PLAYERS_SQL = """
SELECT coalesce(json_agg(json_build_object(
         'name', ps.character_name,
         'funcom_id', acc.funcom_id,
         'faction', f.name
       ) ORDER BY ps.character_name), '[]'::json)
FROM dune.player_state ps
JOIN dune.accounts acc ON acc.id = ps.account_id
LEFT JOIN dune.player_faction pf ON pf.actor_id = ps.player_pawn_id
LEFT JOIN dune.factions f ON f.id = pf.faction_id
WHERE ps.online_status = 'Online'
  AND acc.funcom_id IS NOT NULL AND acc.funcom_id <> '';
"""


def main():
    out = subprocess.run(["/root/dq.sh", "-tAc", PLAYERS_SQL],
                         capture_output=True, text=True, timeout=45)
    if out.returncode != 0:
        print(json.dumps({"available": False, "players": [],
                          "error": (out.stderr or out.stdout).strip()[:300]}))
        return
    try:
        players = json.loads(out.stdout.strip())
    except json.JSONDecodeError:
        print(json.dumps({"available": False, "players": [],
                          "error": out.stdout.strip()[:300]}))
        return
    print(json.dumps({"available": True, "players": players}))


if __name__ == "__main__":
    main()
