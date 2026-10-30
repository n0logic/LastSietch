\set ACCT 2
\set ACTOR 19

-- Recursive flatten of actor pawn properties
WITH RECURSIVE keys AS (
  SELECT key::text AS path, value
    FROM dune.actors, jsonb_each(properties)
   WHERE id = :ACTOR
  UNION ALL
  SELECT k.path || '.' || sub.key, sub.value
    FROM keys k, jsonb_each(k.value) sub
   WHERE jsonb_typeof(k.value) = 'object'
)
SELECT path,
       jsonb_typeof(value) AS jtype,
       CASE
         WHEN jsonb_typeof(value) IN ('string','number','boolean') THEN value::text
         WHEN jsonb_typeof(value) = 'array' THEN '<arr len ' || jsonb_array_length(value) || '>'
         ELSE NULL
       END AS val
  FROM keys
 WHERE jsonb_typeof(value) != 'object'
 ORDER BY path;

\echo ---ACTOR_CONTROLLER_FLAT---

WITH RECURSIVE keys AS (
  SELECT key::text AS path, value
    FROM dune.actors, jsonb_each(properties)
   WHERE id = (SELECT player_controller_id FROM dune.encrypted_player_state WHERE account_id = :ACCT)
  UNION ALL
  SELECT k.path || '.' || sub.key, sub.value
    FROM keys k, jsonb_each(k.value) sub
   WHERE jsonb_typeof(k.value) = 'object'
)
SELECT path,
       jsonb_typeof(value) AS jtype,
       CASE
         WHEN jsonb_typeof(value) IN ('string','number','boolean') THEN value::text
         WHEN jsonb_typeof(value) = 'array' THEN '<arr len ' || jsonb_array_length(value) || '>'
         ELSE NULL
       END AS val
  FROM keys
 WHERE jsonb_typeof(value) != 'object'
 ORDER BY path;

\echo ---GUILD_PARTY---

SELECT 'guild' AS context, gm.guild_id::text AS info FROM dune.guild_members gm WHERE gm.player_id = :ACTOR
UNION ALL
SELECT 'party', pm.party_id::text FROM dune.party_members pm WHERE pm.player_id = :ACTOR;
