"""
Stream D - progression snapshots: char level / XP / SP / intel.

Polls dune.encrypted_player_state JOIN actor_fgl_entities ('DuneCharacter' slot)
JOIN fgl_entities for the FLevelComponent JSONB, plus a LEFT JOIN on actors for
TechKnowledgePlayerComponent.m_TechKnowledgePoints. Uses dune.ls_xp_to_level()
(installed with G11 char_xp) to derive the player's level from TotalXPEarned.

Insert-on-change via UNIQUE(account_id, sample_hash). The hash covers
xp|total_sp|unspent_sp|keystone_sp|intel only - char_name and online_status are
volatile and would force snapshot churn without progression signal.

Level-up events are derived in the same sweep: when a player's lvl crosses a
curve boundary between samples, emit one row per crossed level. Negative deltas
(level drop) log a warning and emit no event.

Passive read-only against dune.*. Writes only to telemetry SQLite.
"""
from __future__ import annotations

import hashlib
import logging
import time

import db

log = logging.getLogger("telemetry.progression")

HARVEST_QUERY = """
SELECT eps.account_id,
       dune.decrypt_user_data(eps.encrypted_character_name) AS char_name,
       eps.online_status,
       COALESCE((fe.components#>>'{FLevelComponent,1,TotalXPEarned}')::bigint, 0)               AS xp,
       dune.ls_xp_to_level(
         COALESCE((fe.components#>>'{FLevelComponent,1,TotalXPEarned}')::bigint, 0))            AS lvl,
       COALESCE((fe.components#>>'{FLevelComponent,1,TotalSkillPoints}')::int, 0)               AS total_sp,
       COALESCE((fe.components#>>'{FLevelComponent,1,UnspentSkillPoints}')::int, 0)             AS unspent_sp,
       COALESCE((fe.components#>>'{FLevelComponent,1,KeystoneBonusSkillPoints}')::int, 0)       AS keystone_sp,
       COALESCE((a.properties#>>'{TechKnowledgePlayerComponent,m_TechKnowledgePoints}')::int,0) AS intel
FROM dune.encrypted_player_state eps
JOIN dune.actor_fgl_entities afe
  ON afe.actor_id = eps.player_pawn_id AND afe.slot_name = 'DuneCharacter'
JOIN dune.fgl_entities fe
  ON fe.entity_id = afe.entity_id
LEFT JOIN dune.actors a
  ON a.id = eps.player_pawn_id
WHERE eps.player_pawn_id IS NOT NULL
  AND eps.account_id <> 0
  AND fe.components ? 'FLevelComponent'
  -- Deleted-character tombstones can linger with online_status='Online' and
  -- (via read_models' last-row-wins per-account dict) clobber the live
  -- character's name/level/online flag for re-roller accounts.
  AND eps.character_state IS DISTINCT FROM 'Deleted'
ORDER BY eps.account_id, eps.last_avatar_activity DESC NULLS LAST
"""

SNAPSHOT_COLUMNS = [
    "ts", "account_id", "char_name", "online_status",
    "xp", "lvl", "total_sp", "unspent_sp", "keystone_sp", "intel",
    "sample_hash",
]

LEVELUP_COLUMNS = [
    "ts", "account_id", "char_name",
    "from_lvl", "to_lvl", "from_xp", "to_xp",
    "crossed_lvl", "detected_at",
]


def _hash_sample(xp, total_sp, unspent_sp, keystone_sp, intel):
    """md5 over the progression fields. Excludes char_name + online_status."""
    payload = "%s|%s|%s|%s|%s" % (xp, total_sp, unspent_sp, keystone_sp, intel)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def _latest_by_account(conn):
    """Return {account_id: (id, xp, lvl, sample_hash)} for the latest row per account."""
    sql = (
        "SELECT p.account_id, p.id, p.xp, p.lvl, p.sample_hash "
        "FROM player_progression p "
        "JOIN (SELECT account_id, MAX(id) AS max_id "
        "      FROM player_progression GROUP BY account_id) m "
        "  ON m.account_id = p.account_id AND m.max_id = p.id"
    )
    out = {}
    for row in conn.execute(sql).fetchall():
        out[row["account_id"]] = (row["id"], row["xp"], row["lvl"], row["sample_hash"])
    return out


def run(ctx):
    rows = ctx.gamedb.query(HARVEST_QUERY, {})
    now = int(time.time())

    prev_by_account = _latest_by_account(ctx.store)

    snap_rows = []
    levelup_rows = []

    for row in rows:
        account_id = str(row.get("account_id"))
        xp = int(row.get("xp") or 0)
        lvl = int(row.get("lvl") or 0)
        total_sp = int(row.get("total_sp") or 0)
        unspent_sp = int(row.get("unspent_sp") or 0)
        keystone_sp = int(row.get("keystone_sp") or 0)
        intel = int(row.get("intel") or 0)
        char_name = row.get("char_name")
        online_status = row.get("online_status")
        sample_hash = _hash_sample(xp, total_sp, unspent_sp, keystone_sp, intel)

        snap_rows.append((
            now, account_id, char_name, online_status,
            xp, lvl, total_sp, unspent_sp, keystone_sp, intel,
            sample_hash,
        ))

        prev = prev_by_account.get(account_id)
        if prev is None:
            # First-ever sighting - no level-up emission. Avoids backfilling N
            # level-ups for a player we just started observing.
            continue
        _, prev_xp, prev_lvl, prev_hash = prev
        if sample_hash == prev_hash:
            # No progression change; insert-on-change will absorb the duplicate.
            continue

        delta = lvl - prev_lvl
        if delta > 0:
            for crossed in range(prev_lvl + 1, lvl + 1):
                levelup_rows.append((
                    now, account_id, char_name,
                    prev_lvl, lvl, prev_xp, xp,
                    crossed, now,
                ))
        elif delta < 0:
            log.warning(
                "progression: %s (%s) dropped from L%d to L%d",
                char_name or "?", account_id, prev_lvl, lvl)

    snap_written = db.insert_many_ignore(
        ctx.store, "player_progression", SNAPSHOT_COLUMNS, snap_rows)
    lvl_written = db.insert_many_ignore(
        ctx.store, "player_progression_levelups", LEVELUP_COLUMNS, levelup_rows)

    log.info("progression: %d players sampled, %d new snapshots, %d level-ups",
             len(rows), snap_written, lvl_written)


STREAM = {"name": "progression",
          "interval_attr": "progression_interval",
          "run": run}
