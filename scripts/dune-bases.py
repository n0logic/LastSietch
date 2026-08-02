#!/usr/bin/env python3
# Read-only base/land-claim ownership directory for the Last Sietch relay. No writes.
# Deployed to lastsietch-dune:/root/dune-bases.py — invoked by the relay over SSH.
#
# Ownership is NOT on the actor. A totem has no owner_account_id, and the
# buildings around it carry owner_entity_id pointing at the TOTEM's own FGL
# entity, not at a player. The real link is:
#
#   totem actor id -> permission_actor_rank.permission_actor_id
#                  -> player_id (a player CONTROLLER actor id)
#                  -> player_state.player_controller_id -> account + name
#
# rank 1 is the owner. Verified 2026-07-25: across all 9,165 permission-bearing
# actors, rank 1 appears at most once each -- so it is a true single owner and
# not merely "highest rank present". Ranks 2+ are co-holders with build/access
# rights. (Most permission actors are placeables -- generators, containers,
# silos. Only 166 are totems, i.e. actual land claims.)
#
# PII rule: this emits character names and tells you who owns what. Same rule as
# dune-roster.py but stronger — the relay endpoint is auth-gated and this must
# NEVER be merged into the shared /portal/maps/{map}/data payload, which the
# public site is designed to consume.
import json
import subprocess
import sys

# health has no max column, so normalise against the highest health observed
# server-wide for that building_type. It is a proxy, not a config read: good
# enough to rank bases by condition, not exact enough to quote as a percentage
# to a player.
BASES_SQL = """
WITH typemax AS (
  SELECT building_type, max(health) AS mx
  FROM dune.building_instances GROUP BY 1
),
totem_entity AS (
  SELECT afe.actor_id AS totem_id, afe.entity_id
  FROM dune.actor_fgl_entities afe
  WHERE afe.slot_name = 'Actor'
    AND afe.actor_id IN (SELECT id FROM dune.totems)
),
pieces AS (
  SELECT te.totem_id,
         count(*) AS piece_count,
         round((avg(bi.health / t.mx) * 100)::numeric, 1) AS health_pct
  FROM totem_entity te
  JOIN dune.building_instances bi ON bi.owner_entity_id = te.entity_id
  JOIN typemax t ON t.building_type = bi.building_type
  GROUP BY 1
),
placeable_count AS (
  SELECT te.totem_id, count(*) AS placeable_count
  FROM totem_entity te
  JOIN dune.placeables p ON p.owner_entity_id = te.entity_id
  GROUP BY 1
),
segs AS (
  SELECT totem_id, count(*) AS claim_segments
  FROM dune.landclaim_segments GROUP BY 1
),
-- A totem with no owner is one of two very different things, and the map must
-- not draw them the same way:
--   actor_state='BaseBackup'  -> not a claim at all. The base was picked up into
--     the reconstruction tool; base_backup_save calls permission_actor_destroy(),
--     which is exactly why the permission rows are gone. 16 of these live.
--   no state row + no rank rows -> a REAL claim whose owner is unrecoverable.
--     permission_actor_rank.player_id FKs to actors ON DELETE CASCADE, so
--     deleting the owner's character silently erases the ownership record while
--     the base stays standing. 12 of these, one with 5k pieces.
-- Verified 2026-07-25: all 138 owned totems have no state row, so absence of a
-- state row is the live-claim signal, not a data gap.
state AS (
  SELECT actor_id, state::text AS state FROM dune.actor_state
),
holders AS (
  SELECT par.permission_actor_id AS totem_id,
         json_agg(json_build_object(
           'rank', par.rank,
           'account_id', ps.account_id,
           'name', ps.character_name,
           'online', ps.online_status = 'Online',
           'last_login', ps.last_login_time,
           'days_away', CASE WHEN ps.last_login_time IS NULL THEN NULL
                             ELSE extract(day FROM (now() - ps.last_login_time))::int END
         ) ORDER BY par.rank) AS holders
  FROM dune.permission_actor_rank par
  LEFT JOIN dune.player_state ps ON ps.player_controller_id = par.player_id
  GROUP BY 1
)
SELECT coalesce(json_agg(b ORDER BY b->>'map', (b->'owner'->>'name')), '[]'::json)
FROM (
  SELECT json_build_object(
    'totem_id', t.id,
    'label', coalesce(pa.actor_name, ''),
    'map', a.map,
    'dimension_index', a.dimension_index,
    'partition_id', a.partition_id,
    'x', round((((a.transform).location).x)::numeric, 0)::bigint,
    'y', round((((a.transform).location).y)::numeric, 0)::bigint,
    'z', round((((a.transform).location).z)::numeric, 0)::bigint,
    'vertical_level', t.landclaim_vertical_level,
    'claim_segments', coalesce(s.claim_segments, 0),
    'pieces', coalesce(p.piece_count, 0),
    'placeables', coalesce(pc.placeable_count, 0),
    'health_pct', p.health_pct,
    'owner', (SELECT h FROM json_array_elements(hs.holders) h
              WHERE (h->>'rank')::int = 1 LIMIT 1),
    'holders', hs.holders,
    'actor_state', st.state,
    'ownership', CASE
      WHEN st.state = 'BaseBackup' THEN 'stored_backup'
      WHEN hs.holders IS NULL THEN 'orphaned'
      WHEN NOT EXISTS (SELECT 1 FROM json_array_elements(hs.holders) h
                       WHERE (h->>'rank')::int = 1) THEN 'orphaned'
      ELSE 'owned' END,
    -- Orphaned and abandoned are NOT the same thing and the tooltip must not
    -- conflate them: orphaned = no owner record survives at all; abandoned =
    -- there IS an owner, they just have not logged in for a long time.
    -- Bands are cut from the live distribution (2026-07-25): 86 of 138 owners
    -- were inside 10 days, then a thin tail out to a maximum of 66. Raw
    -- days_away travels alongside the label so the UI can always show the fact
    -- rather than only our word for it.
    'days_away', (SELECT (h->>'days_away')::int FROM json_array_elements(hs.holders) h
                  WHERE (h->>'rank')::int = 1 LIMIT 1),
    'owner_activity', CASE
      WHEN st.state = 'BaseBackup' THEN NULL
      WHEN (SELECT (h->>'days_away')::int FROM json_array_elements(hs.holders) h
            WHERE (h->>'rank')::int = 1 LIMIT 1) IS NULL THEN NULL
      WHEN (SELECT (h->>'days_away')::int FROM json_array_elements(hs.holders) h
            WHERE (h->>'rank')::int = 1 LIMIT 1) < 14 THEN 'active'
      WHEN (SELECT (h->>'days_away')::int FROM json_array_elements(hs.holders) h
            WHERE (h->>'rank')::int = 1 LIMIT 1) < 30 THEN 'quiet'
      WHEN (SELECT (h->>'days_away')::int FROM json_array_elements(hs.holders) h
            WHERE (h->>'rank')::int = 1 LIMIT 1) < 60 THEN 'dormant'
      ELSE 'abandoned' END
  ) AS b
  FROM dune.totems t
  JOIN dune.actors a ON a.id = t.id
  LEFT JOIN dune.permission_actor pa ON pa.actor_id = t.id
  LEFT JOIN holders hs ON hs.totem_id = t.id
  LEFT JOIN state st ON st.actor_id = t.id
  LEFT JOIN pieces p ON p.totem_id = t.id
  LEFT JOIN placeable_count pc ON pc.totem_id = t.id
  LEFT JOIN segs s ON s.totem_id = t.id
) q;
"""

# Nearest-base lookup for a map click. Same shape as one BASES_SQL row plus a
# distance, so the tooltip renderer has one format to deal with.
NEAR_SQL = """
WITH near AS (
  SELECT t.id AS totem_id,
         a.dimension_index AS dim,
         sqrt(power(((a.transform).location).x - %(x)s, 2)
            + power(((a.transform).location).y - %(y)s, 2)) AS dist_units
  FROM dune.totems t
  JOIN dune.actors a ON a.id = t.id
  LEFT JOIN dune.actor_state ast ON ast.actor_id = t.id
  -- Stored backups still have a totem row and a world transform, but they are
  -- not claims on the ground. Never surface one as "the base you clicked".
  --
  -- The dimension filter is NOT optional. PvE and PvP Hagga are two worlds
  -- sharing one coordinate space (dim 0 = Habbanya PvE, dim 1 = Kulon PvP),
  -- so filtering on map alone ranks claims from the OTHER world against the
  -- point you clicked. Measured 2026-07-27 from a dim 0 standing position: a
  -- dim 1 totem came back second at 81 m, ahead of every real neighbour, and
  -- an earlier lookup put another dim 1 claim fourth. Nothing in the payload
  -- said which world a row belonged to, so it read as a correct answer.
  WHERE a.map = %(map)s
    AND a.dimension_index = %(dim)s
    AND (ast.state IS NULL OR ast.state::text <> 'BaseBackup')
  ORDER BY dist_units
  LIMIT %(limit)s
)
SELECT coalesce(json_agg(json_build_object(
         'totem_id', n.totem_id,
         -- Echoed back so a consumer can assert it got the world it asked for.
         'dimension_index', n.dim,
         'label', coalesce(pa.actor_name, ''),
         'distance_m', round((n.dist_units / 100.0)::numeric, 1),
         'owner', o.owner,
         'ownership', CASE WHEN o.owner IS NULL THEN 'orphaned' ELSE 'owned' END,
         'days_away', (o.owner->>'days_away')::int,
         'owner_activity', CASE
           WHEN o.owner IS NULL THEN NULL
           WHEN (o.owner->>'days_away')::int IS NULL THEN NULL
           WHEN (o.owner->>'days_away')::int < 14 THEN 'active'
           WHEN (o.owner->>'days_away')::int < 30 THEN 'quiet'
           WHEN (o.owner->>'days_away')::int < 60 THEN 'dormant'
           ELSE 'abandoned' END
       ) ORDER BY n.dist_units), '[]'::json)
FROM near n
LEFT JOIN dune.permission_actor pa ON pa.actor_id = n.totem_id
LEFT JOIN LATERAL (
  SELECT json_build_object(
           'account_id', ps.account_id,
           'name', ps.character_name,
           'online', ps.online_status = 'Online',
           'days_away', CASE WHEN ps.last_login_time IS NULL THEN NULL
                             ELSE extract(day FROM (now() - ps.last_login_time))::int END
         ) AS owner
  FROM dune.permission_actor_rank par
  JOIN dune.player_state ps ON ps.player_controller_id = par.player_id
  WHERE par.permission_actor_id = n.totem_id AND par.rank = 1
  LIMIT 1
) o ON TRUE;
"""


# Shipped WITH the data so the portal's help tooltip renders from the same
# source that produced the labels. Duplicating these definitions in a template
# is how a legend ends up quietly describing something the code stopped doing.
LEGEND = {
    "ownership": {
        "_what": "Whether anyone is on record as owning this claim.",
        "owned": "An owner resolves. Normal.",
        "orphaned": ("A real base, still standing, with NO owner record left. "
                     "The ownership rows cascade-delete with the owner's "
                     "character, so deleting a character erases who owned the "
                     "base while the base stays up. Nobody can be contacted "
                     "about these."),
        "stored_backup": ("Not a claim on the ground. The base was picked up "
                          "into the Base Reconstruction Tool; it keeps a world "
                          "position but nothing is built there. Hidden from the "
                          "map."),
    },
    "owner_activity": {
        "_what": ("How long the OWNER has been away. This is about the player, "
                  "NOT the base -- it says nothing about power, fuel, decay or "
                  "condition. A dormant owner's base can be fully powered and "
                  "at full health. For condition, read health_pct."),
        "active": "Owner logged in within the last 14 days.",
        "quiet": "Owner last seen 14-29 days ago.",
        "dormant": "Owner last seen 30-59 days ago.",
        "abandoned": "Owner last seen 60+ days ago.",
        "_null": "No owner to measure (orphaned claim or stored backup).",
    },
    "health_pct": ("Average condition of the base's pieces. There is no "
                   "max-health column in the schema, so each piece is measured "
                   "against the highest health seen server-wide for its type. "
                   "A proxy -- good for ranking bases, not exact enough to "
                   "quote at a player."),
}


def run_sql(sql, timeout=60):
    out = subprocess.run(["/root/dq.sh", "-tAc", sql],
                         capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        print(json.dumps({"available": False, "error": "db query failed",
                          "detail": (out.stderr or out.stdout).strip()[:300]}))
        return None
    return (out.stdout or "").strip()


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "list"

    if mode == "list":
        raw = run_sql(BASES_SQL)
        if raw is None:
            return
        try:
            bases = json.loads(raw or "[]")
        except json.JSONDecodeError:
            print(json.dumps({"available": False, "error": "bad json from db"}))
            return
        counts = {}
        activity = {}
        for b in bases:
            k = b.get("ownership") or "unknown"
            counts[k] = counts.get(k, 0) + 1
            a = b.get("owner_activity")
            if a:
                activity[a] = activity.get(a, 0) + 1
        print(json.dumps({"available": True, "count": len(bases),
                          "by_ownership": counts, "by_owner_activity": activity,
                          "legend": LEGEND, "bases": bases}))
        return

    if mode == "near":
        # near <map> <dim> <x> <y> [limit]
        #
        # dim is required and deliberately has NO default. A default would mean
        # "search every dimension", which is exactly the bug this signature
        # exists to stop: on Hagga that mixes PvE and PvP claims into one
        # ranking. A caller that cannot say which world it means should fail
        # here rather than receive a confident wrong neighbour.
        if len(sys.argv) < 6:
            print(json.dumps({"available": False,
                              "error": "usage: near <map> <dim> <x> <y> [limit]"}))
            return
        map_name = sys.argv[2]
        try:
            dim = int(sys.argv[3])
            x = float(sys.argv[4])
            y = float(sys.argv[5])
            limit = int(sys.argv[6]) if len(sys.argv) > 6 else 5
        except ValueError:
            print(json.dumps({"available": False,
                              "error": "dim, x, y and limit must be numeric"}))
            return
        limit = max(1, min(limit, 25))
        # Inlined rather than parameterised: dq.sh is psql -c, not a driver.
        # map_name is the only text input, so quote-escape it and reject the
        # rest by type above.
        sql = (NEAR_SQL
               .replace("%(x)s", repr(x))
               .replace("%(y)s", repr(y))
               .replace("%(dim)s", str(dim))
               .replace("%(limit)s", str(limit))
               .replace("%(map)s", "'" + map_name.replace("'", "''") + "'"))
        raw = run_sql(sql, timeout=30)
        if raw is None:
            return
        try:
            near = json.loads(raw or "[]")
        except json.JSONDecodeError:
            print(json.dumps({"available": False, "error": "bad json from db"}))
            return
        print(json.dumps({"available": True, "count": len(near),
                          "map": map_name, "dimension_index": dim,
                          "bases": near}))
        return

    print(json.dumps({"available": False, "error": f"unknown mode: {mode}"}))


if __name__ == "__main__":
    main()
