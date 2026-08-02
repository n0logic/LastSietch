-- dune-augment-verify.sql — READ-ONLY verification of the augment write surface
-- on OUR live build (2036754) before building the augment perfect-roll/swap tooling.
--
-- Confirms the augment jsonb shape RedBlink's repo relies on
-- (our internal design notes) exists on OUR DB.
-- Every statement is a SELECT. No writes, no procs, no DDL. Safe to run on the
-- change-frozen game box (still: read-only, no pod restart).
--
--   Run (owner):  /root/dq.sh -f dune-augment-verify.sql
--   or paste block-by-block through dq.sh -c '<one query>'
--
-- What we're proving:
--   1. augmented GEAR stores augments under stats->'FAugmentedItemStats'
--      with parallel AppliedAugments / AppliedAugmentQualities / AppliedAugmentRollData.
--   2. standalone AUGMENT consumables store their rolled shape under
--      stats->'FAugmentItemStats' (singular) — the roll-shape source.
--   3. the augment-slot keystones 42-49 exist in the game keystone map.
--   4. StatRolls value range on real items (so "perfect = 1.0" is confirmed, not assumed).

\set ON_ERROR_STOP off
SET search_path TO dune, public;

\echo '== 1. items carrying FAugmentedItemStats (augmented gear) =='
SELECT count(*) AS augmented_gear_rows
  FROM dune.items
 WHERE stats ? 'FAugmentedItemStats';

\echo '== 1b. sample augmented-gear stats shape (up to 5) =='
SELECT id, template_id, quality_level,
       jsonb_pretty(stats -> 'FAugmentedItemStats') AS faugmented_item_stats
  FROM dune.items
 WHERE stats ? 'FAugmentedItemStats'
 ORDER BY id DESC
 LIMIT 5;

\echo '== 1c. flatten AppliedAugments/Qualities/RollData for a few real items =='
SELECT i.id, i.template_id,
       aug ->> 'Name'                                      AS augment_name,
       (i.stats #> '{FAugmentedItemStats,1,AppliedAugmentQualities}') -> (ord - 1)::int AS quality,
       (i.stats #> '{FAugmentedItemStats,1,AppliedAugmentRollData}') -> (ord - 1)::int  AS roll_data
  FROM dune.items i
 CROSS JOIN LATERAL jsonb_array_elements(
         i.stats #> '{FAugmentedItemStats,1,AppliedAugments}'
       ) WITH ORDINALITY AS t(aug, ord)
 WHERE i.stats ? 'FAugmentedItemStats'
 ORDER BY i.id DESC
 LIMIT 20;

\echo '== 2. standalone augment consumables carrying FAugmentItemStats (roll-shape source) =='
SELECT count(*) AS standalone_augment_rows
  FROM dune.items
 WHERE stats ? 'FAugmentItemStats';

\echo '== 2b. sample standalone augment stats + template ids (do they use T#_Augment_ ?) =='
SELECT id, template_id, quality_level,
       jsonb_pretty(stats -> 'FAugmentItemStats') AS faugment_item_stats
  FROM dune.items
 WHERE stats ? 'FAugmentItemStats'
 ORDER BY id DESC
 LIMIT 5;

\echo '== 2c. distinct augment template_ids seen on gear (AppliedAugments[].Name) =='
SELECT DISTINCT aug ->> 'Name' AS augment_template_id
  FROM dune.items i
 CROSS JOIN LATERAL jsonb_array_elements(
         i.stats #> '{FAugmentedItemStats,1,AppliedAugments}'
       ) AS aug
 WHERE i.stats ? 'FAugmentedItemStats'
 ORDER BY 1
 LIMIT 50;

\echo '== 3. augment-slot keystones 42-49 present in the game keystone map? =='
-- RedBlink references dune.specialization_keystones_map; our grant path uses
-- dune.purchased_specialization_keystones. Probe whichever table(s) exist.
SELECT to_regclass('dune.specialization_keystones_map')      AS keystone_map_table,
       to_regclass('dune.purchased_specialization_keystones') AS purchased_table,
       to_regclass('dune.specialization_tracks')              AS tracks_table;

\echo '== 3b. rows for ids 42-49 in the keystone map (if the table exists) =='
-- If keystone_map_table above is NULL, skip this query (will error; that is a finding).
SELECT id, keystone_name, track_type
  FROM dune.specialization_keystones_map
 WHERE id BETWEEN 42 AND 49
 ORDER BY id;

\echo '== 4. StatRolls value distribution on real augmented items (confirm 0..1 range) =='
SELECT min(v::numeric) AS min_roll, max(v::numeric) AS max_roll, count(*) AS roll_values
  FROM dune.items i
 CROSS JOIN LATERAL jsonb_array_elements(
         i.stats #> '{FAugmentedItemStats,1,AppliedAugmentRollData}'
       ) AS rd
 CROSS JOIN LATERAL jsonb_array_elements(rd -> 'StatRolls') AS v
 WHERE i.stats ? 'FAugmentedItemStats';

\echo '== DONE. Expected: (1)>0 gear rows with the 3 parallel arrays; (2) standalone rows under FAugmentItemStats; (3) map table non-null + 42-49 present; (4) max_roll <= 1.0 => perfect = 1.0 confirmed. =='
