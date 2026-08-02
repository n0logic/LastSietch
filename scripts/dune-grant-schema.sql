-- dune.ls_progression_grants — audit + idempotency table for the Dune
-- progression-grant admin tool. See docs/DUNE-PROGRESSION-GRANT-TOOL-PLAN.md
-- section 5.
--
-- APPLY ONCE, MANUALLY, BY AN OPERATOR (task DG-D1). This file is NOT
-- auto-applied by any service, migration runner, or watcher. The admin-backend
-- uses SQLite for its own audit_log; this table lives in the game's Postgres
-- (database `dune`) and is created by hand from this script.
--
-- The table MUST be OWNER TO dune: a ls_* table owned by `postgres` makes
-- Funcom's pre-update pg_dump abort the entire game update (this burned the
-- team on the 2026-05-21 GA update).
--
-- Apply with the read/write equivalent of dq.sh, e.g.:
--   sudo kubectl exec -i -n <ns> <db-pod> -- \
--     env PGPASSWORD=<pw> psql -h localhost -p 15432 -U postgres -d dune \
--     -v ON_ERROR_STOP=1 -f dune-grant-schema.sql
-- Verify afterwards (read-only): \dt dune.ls_* and \d dune.ls_progression_grants

CREATE TABLE IF NOT EXISTS dune.ls_progression_grants (
  id               bigserial PRIMARY KEY,
  idempotency_key  uuid        NOT NULL UNIQUE,
  granted_at       timestamptz NOT NULL DEFAULT now(),
  account_id       bigint      NOT NULL,
  grant_type       text        NOT NULL,
  detail           jsonb       NOT NULL,           -- {template_id, qty, quality, amount, …}
  operator         text        NOT NULL,           -- admin username, passed from admin-backend
  status           text        NOT NULL DEFAULT 'pending',  -- pending | applied | deferred | failed
  applied_at       timestamptz,                    -- set when a deferred/RAM-fragile grant lands
  notes            text
);

ALTER TABLE dune.ls_progression_grants OWNER TO dune;

CREATE INDEX IF NOT EXISTS ls_progression_grants_account_idx
  ON dune.ls_progression_grants (account_id, granted_at DESC);

-- =============================================================================
-- G11 char_xp v2 dependency — character-level XP curve + xpToLevel function.
-- See docs/dune-research/CHAR-XP-GRANT-SPEC.md and our internal notes
-- memory. Transcribed from icehunter dune-admin/db.go:1518-1538 (cumulativeXPByLevel)
-- which itself comes from Funcom's SkillXPPerLevel.json. 201 entries (L0..L200);
-- L200 cumulative = 344,440 (the in-game cap).
--
-- Must be OWNER dune (custom-table-ownership rule,).
-- =============================================================================

CREATE TABLE IF NOT EXISTS dune.ls_char_xp_curve (
  level         int    PRIMARY KEY,
  cumulative_xp bigint NOT NULL
);

ALTER TABLE dune.ls_char_xp_curve OWNER TO dune;

-- Idempotent seed: ON CONFLICT updates cumulative_xp if a Funcom rebalance ever
-- changes the curve. Safe to re-run the whole schema file.
INSERT INTO dune.ls_char_xp_curve (level, cumulative_xp) VALUES
  (0,0),(1,40),(2,215),(3,440),(4,740),(5,1240),(6,1790),(7,2390),(8,2990),(9,3590),
  (10,4190),(11,4790),(12,5390),(13,5990),(14,6590),(15,7190),(16,7790),(17,8390),(18,8990),(19,9590),
  (20,10190),(21,10790),(22,11390),(23,11990),(24,12590),(25,13190),(26,13790),(27,14390),(28,14990),(29,15590),
  (30,16190),(31,16790),(32,17390),(33,17990),(34,18590),(35,19190),(36,19790),(37,20390),(38,20990),(39,21590),
  (40,22190),(41,22790),(42,23390),(43,23990),(44,24590),(45,25190),(46,25790),(47,26390),(48,26990),(49,27590),
  (50,28190),(51,28790),(52,29390),(53,29990),(54,30590),(55,31190),(56,31790),(57,32390),(58,32990),(59,33590),
  (60,34190),(61,34790),(62,35390),(63,35990),(64,36590),(65,37190),(66,37790),(67,38390),(68,38990),(69,39590),
  (70,40190),(71,40790),(72,41390),(73,41990),(74,42590),(75,43190),(76,43790),(77,44390),(78,44990),(79,45590),
  (80,46190),(81,46790),(82,47390),(83,47990),(84,48590),(85,49190),(86,49790),(87,50390),(88,50990),(89,51590),
  (90,52190),(91,52790),(92,53390),(93,53990),(94,54590),(95,55190),(96,55790),(97,56390),(98,56990),(99,57590),
  (100,58190),(101,58840),(102,59490),(103,60140),(104,60790),(105,61440),(106,62090),(107,62740),(108,63390),(109,64040),
  (110,64690),(111,65340),(112,65990),(113,66640),(114,67290),(115,67940),(116,68590),(117,69240),(118,69890),(119,70540),
  (120,71190),(121,71840),(122,72490),(123,73140),(124,73790),(125,74440),(126,75090),(127,75740),(128,76391),(129,77044),
  (130,77699),(131,78357),(132,79018),(133,79683),(134,80353),(135,81030),(136,81714),(137,82407),(138,83110),(139,83825),
  (140,84554),(141,85298),(142,86060),(143,86842),(144,87646),(145,88475),(146,89332),(147,90220),(148,91141),(149,92100),
  (150,93099),(151,94143),(152,95235),(153,96380),(154,97582),(155,98845),(156,100175),(157,101576),(158,103054),(159,104614),
  (160,106263),(161,108006),(162,109849),(163,111799),(164,113862),(165,116046),(166,118358),(167,120806),(168,123397),(169,126139),
  (170,129041),(171,132112),(172,135360),(173,138795),(174,142426),(175,146263),(176,150316),(177,154596),(178,159114),(179,163880),
  (180,168906),(181,174203),(182,179784),(183,185661),(184,191846),(185,198353),(186,205195),(187,212385),(188,219938),(189,227868),
  (190,236190),(191,244918),(192,254069),(193,263657),(194,273700),(195,284213),(196,295214),(197,306719),(198,318746),(199,331314),
  (200,344440)
ON CONFLICT (level) DO UPDATE SET cumulative_xp = EXCLUDED.cumulative_xp;

-- Helper function: xp -> level. Equivalent to icehunter's xpToLevel binary
-- search but expressed as a single SELECT against the curve table. IMMUTABLE
-- so the planner can fold constant calls; STABLE-on-data is fine because the
-- curve table is effectively a constant lookup table.
CREATE OR REPLACE FUNCTION dune.ls_xp_to_level(xp bigint) RETURNS int AS $$
  SELECT COALESCE(MAX(level), 0)
    FROM dune.ls_char_xp_curve
   WHERE cumulative_xp <= GREATEST(xp, 0);
$$ LANGUAGE sql IMMUTABLE PARALLEL SAFE;

ALTER FUNCTION dune.ls_xp_to_level(bigint) OWNER TO dune;

-- ---- G9 keystone SP-bonus reconciliation (parity audit Action #1) ----
-- Catalog maps keystone_id -> (track, name, sp_bonus). sp_bonus per
-- icehunter keystoneSPBonus(): suffix _SkillPoint_Super=+5,
-- _SkillPoint_Major=+3, _SkillPoint=+1, otherwise 0. Seeded from
-- dune-admin/keystones.go (v0.4.3, 8bd95827), extracted from game PAK.
--
-- 2026-05-29 backfill: req_level + spice_cost columns added for the 5-track
-- trait picker (level gate + melange cost). The 4-column INSERT below still
-- seeds track/name/sp_bonus; req_level/spice_cost are populated (all 205 rows)
-- by scripts/dune-keystone-catalog-backfill-2026-05-29.sql, which is generated
-- from the current keystones.go by scripts/build-keystone-catalog-sql.py and is
-- the authority for those two columns. Apply the backfill after this schema on
-- a fresh DB. Both are idempotent UPSERTs of the same keystone_ids, so order is
-- the only constraint, never divergence.

CREATE TABLE IF NOT EXISTS dune.ls_keystone_catalog (
  keystone_id   smallint PRIMARY KEY,
  track         text     NOT NULL,
  keystone_name text     NOT NULL,
  sp_bonus      smallint NOT NULL DEFAULT 0,
  req_level     smallint,
  spice_cost    integer
);
ALTER TABLE dune.ls_keystone_catalog OWNER TO dune;

-- Replay-safe column adds for tables created before the 2026-05-29 backfill.
ALTER TABLE dune.ls_keystone_catalog
  ADD COLUMN IF NOT EXISTS req_level  smallint,
  ADD COLUMN IF NOT EXISTS spice_cost integer;

INSERT INTO dune.ls_keystone_catalog (keystone_id, track, keystone_name, sp_bonus) VALUES
  (1,'Combat','DA_CombatKeystone_SkillPoint_Major',3),
  (2,'Combat','DA_CombatKeystone_SkillPoint',1),
  (3,'Combat','DA_CombatKeystone_SkillPoint_Major',3),
  (4,'Combat','DA_CombatKeystone_SkillPoint',1),
  (5,'Combat','DA_CombatKeystone_SkillPoint',1),
  (6,'Combat','DA_CombatKeystone_SkillPoint_Major',3),
  (7,'Combat','DA_CombatKeystone_SkillPoint',1),
  (8,'Combat','DA_CombatKeystone_SkillPoint',1),
  (9,'Combat','DA_CombatKeystone_SkillPoint_Major',3),
  (10,'Combat','DA_CombatKeystone_SkillPoint',1),
  (11,'Combat','DA_CombatKeystone_SkillPoint',1),
  (12,'Combat','DA_CombatKeystone_SkillPoint_Major',3),
  (13,'Combat','DA_CombatKeystone_SkillPoint',1),
  (14,'Combat','DA_CombatKeystone_SkillPoint',1),
  (15,'Combat','DA_CombatKeystone_SkillPoint_Major',3),
  (16,'Combat','DA_CombatKeystone_SkillPoint',1),
  (17,'Combat','DA_CombatKeystone_SkillPoint',1),
  (18,'Combat','DA_CombatKeystone_SkillPoint_Major',3),
  (19,'Combat','DA_CombatKeystone_SkillPoint',1),
  (20,'Combat','DA_CombatKeystone_SkillPoint',1),
  (21,'Combat','DA_CombatKeystone_SkillPoint_Major',3),
  (22,'Combat','DA_CombatKeystone_SkillPoint',1),
  (23,'Combat','DA_CombatKeystone_SkillPoint',1),
  (24,'Combat','DA_CombatKeystone_SkillPoint_Major',3),
  (25,'Combat','DA_CombatKeystone_SkillPoint',1),
  (26,'Combat','DA_CombatKeystone_SkillPoint',1),
  (27,'Combat','DA_CombatKeystone_SkillPoint_Major',3),
  (28,'Combat','DA_CombatKeystone_SkillPoint',1),
  (29,'Combat','DA_CombatKeystone_SkillPoint',1),
  (30,'Combat','DA_CombatKeystone_SkillPoint_Super',5),
  (31,'Combat','DA_CombatKeystone_MaxHealth_Major',0),
  (32,'Combat','DA_CombatKeystone_MaxHealth',0),
  (33,'Combat','DA_CombatKeystone_MaxHealth',0),
  (34,'Combat','DA_CombatKeystone_MaxHealth_Super',0),
  (35,'Combat','DA_CombatKeystone_MaxHealth',0),
  (36,'Combat','DA_CombatKeystone_MaxStamina',0),
  (37,'Combat','DA_CombatKeystone_MaxStamina_Major',0),
  (38,'Combat','DA_CombatKeystone_MaxStamina',0),
  (39,'Combat','DA_CombatKeystone_MaxStamina',0),
  (40,'Combat','DA_CombatKeystone_MaxStamina_Major',0),
  (41,'Combat','DA_CombatKeystone_Hat',0),
  (42,'Crafting','DA_CraftingKeystone_ArmorAugment_Major',0),
  (43,'Crafting','DA_CraftingKeystone_ArmorAugment_Major',0),
  (44,'Crafting','DA_CraftingKeystone_MeleeAugment_Major',0),
  (45,'Crafting','DA_CraftingKeystone_MeleeAugment_Major',0),
  (46,'Crafting','DA_CraftingKeystone_MeleeAugment_Major',0),
  (47,'Crafting','DA_CraftingKeystone_RangedAugment_Major',0),
  (48,'Crafting','DA_CraftingKeystone_RangedAugment_Major',0),
  (49,'Crafting','DA_CraftingKeystone_RangedAugment_Major',0),
  (50,'Crafting','DA_CraftingKeystone_ConsumableBatchCrafting',0),
  (51,'Crafting','DA_CraftingKeystone_CraftingJackpot_Major',0),
  (52,'Crafting','DA_CraftingKeystone_CraftingJackpot',0),
  (53,'Crafting','DA_CraftingKeystone_CraftingJackpot',0),
  (54,'Crafting','DA_CraftingKeystone_CraftingJackpot_Major',0),
  (55,'Crafting','DA_CraftingKeystone_CraftingJackpot_Major',0),
  (56,'Crafting','DA_CraftingKeystone_CraftingSpeedIncrease',0),
  (57,'Crafting','DA_CraftingKeystone_CraftingSpeedIncrease',0),
  (58,'Crafting','DA_CraftingKeystone_GhostData_Major',0),
  (59,'Crafting','DA_CraftingKeystone_AugmentCraftingCostDecrease',0),
  (60,'Crafting','DA_CraftingKeystone_CraftingSpeedIncrease',0),
  (61,'Crafting','DA_CraftingKeystone_AugmentCraftingCostDecrease',0),
  (62,'Crafting','DA_CraftingKeystone_CraftingSpeedIncrease',0),
  (63,'Crafting','DA_CraftingKeystone_AugmentCraftingCostDecrease',0),
  (64,'Crafting','DA_CraftingKeystone_RecyclingJackpot',0),
  (65,'Crafting','DA_CraftingKeystone_RecyclingJackpot',0),
  (66,'Crafting','DA_CraftingKeystone_RecyclingJackpot',0),
  (67,'Crafting','DA_CraftingKeystone_RecyclingYield',0),
  (68,'Crafting','DA_CraftingKeystone_RecyclingYield',0),
  (69,'Crafting','DA_CraftingKeystone_RecyclingYield',0),
  (70,'Crafting','DA_CraftingKeystone_RefiningYield_1',0),
  (71,'Crafting','DA_CraftingKeystone_RefiningYield_2',0),
  (72,'Crafting','DA_CraftingKeystone_RefiningYield_3',0),
  (73,'Crafting','DA_CraftingKeystone_RefiningYield_4',0),
  (74,'Crafting','DA_CraftingKeystone_RefiningYield_5',0),
  (75,'Crafting','DA_CraftingKeystone_MaxDurabilityLossReduction',0),
  (76,'Crafting','DA_CraftingKeystone_MaxDurabilityLossReduction',0),
  (77,'Crafting','DA_CraftingKeystone_MaxDurabilityLossReduction',0),
  (78,'Crafting','DA_CraftingKeystone_MaxDurabilityLossReduction_Major',0),
  (79,'Crafting','DA_CraftingKeystone_MaxDurabilityLossReduction',0),
  (80,'Crafting','DA_CraftingKeystone_SchematicsOnRecycling_Major',0),
  (81,'Crafting','DA_CraftingKeystone_Hat',0),
  (82,'Crafting','DA_CraftingKeystone_FragmentUpgrade_Major',0),
  (83,'Exploration','DA_ExplorationKeystone_CrashedShipBonusLoot',0),
  (84,'Exploration','DA_ExplorationKeystone_ClimbingStamina',0),
  (85,'Exploration','DA_ExplorationKeystone_VehicleHeatDissipation',0),
  (86,'Exploration','DA_ExplorationKeystone_VehicleHeatDissipation',0),
  (87,'Exploration','DA_ExplorationKeystone_VehicleHeatDissipation',0),
  (88,'Exploration','DA_ExplorationKeystone_FogOfWarRadius',0),
  (89,'Exploration','DA_ExplorationKeystone_WormThreatReduction',0),
  (90,'Exploration','DA_ExplorationKeystone_WormThreatReduction',0),
  (91,'Exploration','DA_ExplorationKeystone_LootPoolAlterations_Major',0),
  (92,'Exploration','DA_ExplorationKeystone_PlayerInventorySlots_Major',0),
  (93,'Exploration','DA_ExplorationKeystone_PlayerInventorySlots_Major',0),
  (94,'Exploration','DA_ExplorationKeystone_PlayerInventorySlots_Major',0),
  (95,'Exploration','DA_ExplorationKeystone_PlayerInventorySlots_Major',0),
  (96,'Exploration','DA_ExplorationKeystone_PlayerInventorySlots_Major',0),
  (97,'Exploration','DA_ExplorationKeystone_VehicleRecoeryCostReduction',0),
  (98,'Exploration','DA_ExplorationKeystone_VehicleRecoeryCostReduction',0),
  (99,'Exploration','DA_ExplorationKeystone_VehicleSandstormDamageReduction',0),
  (100,'Exploration','DA_ExplorationKeystone_VehicleSandstormDamageReduction',0),
  (101,'Exploration','DA_ExplorationKeystone_VehicleSandstormDamageReduction',0),
  (102,'Exploration','DA_ExplorationKeystone_ScanningRange',0),
  (103,'Exploration','DA_ExplorationKeystone_ScanningRange_Major',0),
  (104,'Exploration','DA_ExplorationKeystone_SurveyTimeDecrease_Major',0),
  (105,'Exploration','DA_ExplorationKeystone_SuspensorDrain',0),
  (106,'Exploration','DA_ExplorationKeystone_SuspensorDrain_Major',0),
  (107,'Exploration','DA_ExplorationKeystone_VehicleBoostHeatCostReduction',0),
  (108,'Exploration','DA_ExplorationKeystone_VehicleBoostHeatCostReduction',0),
  (109,'Exploration','DA_ExplorationKeystone_VehicleBoostHeatCostReduction',0),
  (110,'Exploration','DA_ExplorationKeystone_VehicleBoostHeatCostReduction',0),
  (111,'Exploration','DA_ExplorationKeystone_VehicleBoostHeatCostReduction',0),
  (112,'Exploration','DA_ExplorationKeystone_VehicleDamageResistance',0),
  (113,'Exploration','DA_ExplorationKeystone_VehicleDamageResistance',0),
  (114,'Exploration','DA_ExplorationKeystone_VehicleFuelEfficiency_Major',0),
  (115,'Exploration','DA_ExplorationKeystone_VehicleFuelEfficiency_Major',0),
  (116,'Exploration','DA_ExplorationKeystone_VehicleFuelEfficiency_Major',0),
  (117,'Exploration','DA_ExplorationKeystone_VehicleFuelEfficiency_Major',0),
  (118,'Exploration','DA_ExplorationKeystone_VehicleFuelEfficiency_Major',0),
  (119,'Exploration','DA_ExplorationKeystone_VehicleHeatDissipation',0),
  (120,'Exploration','DA_ExplorationKeystone_VehicleHeatDissipation',0),
  (121,'Exploration','DA_ExplorationKeystone_VehicleHeatDissipation',0),
  (122,'Exploration','DA_ExplorationKeystone_VehicleSpeedBonus_Major',0),
  (123,'Exploration','DA_ExplorationKeystone_Hat',0),
  (124,'Gathering','DA_GatheringKeystone_BonusBlood',0),
  (125,'Gathering','DA_GatheringKeystone_BonusWater',0),
  (126,'Gathering','DA_GatheringKeystone_PickupYield',0),
  (127,'Gathering','DA_GatheringKeystone_PickupYield',0),
  (128,'Gathering','DA_GatheringKeystone_ScrapObjectsYield_Major',0),
  (129,'Gathering','DA_GatheringKeystone_ScrapObjectsYield',0),
  (130,'Gathering','DA_GatheringKeystone_ByProductSalvage',0),
  (131,'Gathering','DA_GatheringKeystone_ByProductSalvage',0),
  (132,'Gathering','DA_GatheringKeystone_ByProductSalvage',0),
  (133,'Gathering','DA_GatheringKeystone_ByProductSalvage',0),
  (134,'Gathering','DA_GatheringKeystone_ByProductSalvage',0),
  (135,'Gathering','DA_GatheringKeystone_ByProductSalvage',0),
  (136,'Gathering','DA_GatheringKeystone_ByProductSalvage',0),
  (137,'Gathering','DA_GatheringKeystone_ByProductSalvage',0),
  (138,'Gathering','DA_GatheringKeystone_ByProductSalvage',0),
  (139,'Gathering','DA_GatheringKeystone_ByProductSalvage',0),
  (140,'Gathering','DA_GatheringKeystone_CompactorRange',0),
  (141,'Gathering','DA_GatheringKeystone_CompactorRange',0),
  (142,'Gathering','DA_GatheringKeystone_CompactorRange',0),
  (143,'Gathering','DA_GatheringKeystone_CompactorRange',0),
  (144,'Gathering','DA_GatheringKeystone_CompactorRange_Major',0),
  (145,'Gathering','DA_GatheringKeystone_CompactorThreat',0),
  (146,'Gathering','DA_GatheringKeystone_CompactorThreat',0),
  (147,'Gathering','DA_GatheringKeystone_CompactorThreat',0),
  (148,'Gathering','DA_GatheringKeystone_CompactorThreat',0),
  (149,'Gathering','DA_GatheringKeystone_CompactorThreat',0),
  (150,'Gathering','DA_GatheringKeystone_NewCorpseType_Major',0),
  (151,'Gathering','DA_GatheringKeystone_ToolPowerCostReduction_Major',0),
  (152,'Gathering','DA_GatheringKeystone_ToolPowerCostReduction',0),
  (153,'Gathering','DA_GatheringKeystone_ToolPowerCostReduction_Major',0),
  (154,'Gathering','DA_GatheringKeystone_YieldJackpot_Major',0),
  (155,'Gathering','DA_GatheringKeystone_YieldJackpot_Major',0),
  (156,'Gathering','DA_GatheringKeystone_YieldJackpot_Major',0),
  (157,'Gathering','DA_GatheringKeystone_YieldJackpot_Major',0),
  (158,'Gathering','DA_GatheringKeystone_YieldJackpot_Major',0),
  (159,'Gathering','DA_GatheringKeystone_YieldJackpot_Major',0),
  (160,'Gathering','DA_GatheringKeystone_YieldJackpot_Major',0),
  (161,'Gathering','DA_GatheringKeystone_YieldJackpot_Major',0),
  (162,'Gathering','DA_GatheringKeystone_YieldJackpot_Major',0),
  (163,'Gathering','DA_GatheringKeystone_YieldJackpot_Major',0),
  (164,'Gathering','DA_GatheringKeystone_Hat',0),
  (165,'Sabotage','DA_SabotageKeystone_LandsraadBribeCost',0),
  (166,'Sabotage','DA_SabotageKeystone_LandsraadBribeCost',0),
  (167,'Sabotage','DA_SabotageKeystone_LandsraadBribeCost',0),
  (168,'Sabotage','DA_SabotageKeystone_LandsraadBribeCost',0),
  (169,'Sabotage','DA_SabotageKeystone_LandsraadContribution',0),
  (170,'Sabotage','DA_SabotageKeystone_LandsraadContribution_Major',0),
  (171,'Sabotage','DA_SabotageKeystone_LandsraadContribution',0),
  (172,'Sabotage','DA_SabotageKeystone_LandsraadContribution',0),
  (173,'Sabotage','DA_SabotageKeystone_LandsraadContribution',0),
  (174,'Sabotage','DA_SabotageKeystone_LandsraadBribeCost_Major',0),
  (175,'Sabotage','DA_SabotageKeystone_ExtraLootOnCorpses2_Major',0),
  (176,'Sabotage','DA_SabotageKeystone_HouseCreditsBonus_Major',0),
  (177,'Sabotage','DA_SabotageKeystone_HeadshotDamage_Major',0),
  (178,'Sabotage','DA_SabotageKeystone_HeadshotDamage_Major',0),
  (179,'Sabotage','DA_SabotageKeystone_HeadshotDamage_Major',0),
  (180,'Sabotage','DA_SabotageKeystone_HeadshotDamage_Major',0),
  (181,'Sabotage','DA_SabotageKeystone_HeadshotDamage_Major',0),
  (182,'Sabotage','DA_SabotageKeystone_ExplosiveBarrels',0),
  (183,'Sabotage','DA_SabotageKeystone_HouseCreditsBonus_Major',0),
  (184,'Sabotage','DA_SabotageKeystone_HouseCreditsBonus',0),
  (185,'Sabotage','DA_SabotageKeystone_HouseCreditsBonus',0),
  (186,'Sabotage','DA_SabotageKeystone_HouseCreditsBonus_Major',0),
  (187,'Sabotage','DA_SabotageKeystone_ScanningRangeResistance',0),
  (188,'Sabotage','DA_SabotageKeystone_ScanningRangeResistance',0),
  (189,'Sabotage','DA_SabotageKeystone_ScanningRangeResistance',0),
  (190,'Sabotage','DA_SabotageKeystone_ScanningRangeResistance',0),
  (191,'Sabotage','DA_SabotageKeystone_ScanningRangeResistance',0),
  (192,'Sabotage','DA_SabotageKeystone_RecognitionReduction',0),
  (193,'Sabotage','DA_SabotageKeystone_RecognitionReduction_Major',0),
  (194,'Sabotage','DA_SabotageKeystone_AggroRangeReduction',0),
  (195,'Sabotage','DA_SabotageKeystone_AggroRangeReduction',0),
  (196,'Sabotage','DA_SabotageKeystone_ReducedScannedTime',0),
  (197,'Sabotage','DA_SabotageKeystone_ReducedScannedTime',0),
  (198,'Sabotage','DA_SabotageKeystone_ReducedScannedTime',0),
  (199,'Sabotage','DA_SabotageKeystone_ReducedScannedTime',0),
  (200,'Sabotage','DA_SabotageKeystone_ReducedScannedTime',0),
  (201,'Sabotage','DA_SabotageKeystone_HeadshotDamage_Major',0),
  (202,'Sabotage','DA_SabotageKeystone_HouseCreditsBonus_Major',0),
  (203,'Sabotage','DA_SabotageKeystone_LandsraadBribeCost_Major',0),
  (204,'Sabotage','DA_SabotageKeystone_IncreasedTrapTimer',0),
  (205,'Sabotage','DA_SabotageKeystone_Hat',0)
ON CONFLICT (keystone_id) DO UPDATE SET
  track         = EXCLUDED.track,
  keystone_name = EXCLUDED.keystone_name,
  sp_bonus      = EXCLUDED.sp_bonus;

-- ============================================================================
-- G11 v3 intel-per-level curve (icehunter parity for cmdAwardCharXP's
-- intelAtLevel helper, db.go:1558-1586). Cumulative intel earned through
-- the given character level. Used by build_char_xp_grant to delta-credit
-- m_TechKnowledgePoints on the player's actor properties alongside the
-- TotalXPEarned / TotalSkillPoints / UnspentSkillPoints writes.
-- Source curve from IntelPointsRewarded in SkillXPPerLevel.json (per icehunter).
--   L1=4, L2-3=+2, L4-15=+3, L16-30=+5, L31-50=+10,
--   L51-69=+20, L70-85=+30, L86-125=+40, L126+ caps at 2779.
-- ============================================================================
CREATE OR REPLACE FUNCTION dune.ls_intel_at_level(level integer)
RETURNS integer
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$
  SELECT CASE
    WHEN level <= 0   THEN 0
    WHEN level = 1    THEN 4
    WHEN level <= 3   THEN 4    + (level - 1)  * 2
    WHEN level <= 15  THEN 8    + (level - 3)  * 3
    WHEN level <= 30  THEN 44   + (level - 15) * 5
    WHEN level <= 50  THEN 119  + (level - 30) * 10
    WHEN level <= 69  THEN 319  + (level - 50) * 20
    WHEN level <= 85  THEN 699  + (level - 69) * 30
    WHEN level <= 125 THEN 1179 + (level - 85) * 40
    ELSE 2779
  END;
$$;

ALTER FUNCTION dune.ls_intel_at_level(integer) OWNER TO dune;

-- ============================================================================
-- G21 bb_clone deep-clone dependency — Last Sietch-reserved fgl_entities.entity_id
-- range. dune.fgl_entities.entity_id has NO sequence (Funcom mints IDs from a
-- runtime allocator at game-server start; observed values in production span
-- roughly -3e18..+4.1e18). To avoid any chance of collision with future
-- Funcom-allocated IDs while we deep-clone a base subgraph, Last Sietch mints from a
-- dedicated sequence starting at 5e18. That gives us ~4.2e18 IDs before
-- hitting the bigint ceiling (9.22e18) — orders of magnitude more than we
-- could ever need for base clones.
--
-- Per this sequence MUST be OWNER
-- dune so Funcom's pre-update pg_dump (run as the dune role) can dump it
-- without permission errors. The G21 bb_clone grant uses
-- nextval('dune.ls_fgl_entity_id_seq') once per cloned actor_fgl_entities
-- row (i.e. per FGL slot — actors can carry multiple slots like 'Actor' and
-- 'ContainerInventory'). See docs/dune-research/ITEM-G21-BUILD-SPEC.md.
-- ============================================================================
CREATE SEQUENCE IF NOT EXISTS dune.ls_fgl_entity_id_seq
  START WITH 5000000000000000000
  INCREMENT BY 1
  NO MAXVALUE
  CACHE 1;

ALTER SEQUENCE dune.ls_fgl_entity_id_seq OWNER TO dune;

COMMENT ON SEQUENCE dune.ls_fgl_entity_id_seq IS
  'Last Sietch-reserved entity_id range for G21 bb_clone deep-clone of base subgraphs. Starts at 5e18 to avoid collision with Funcom runtime-allocated IDs (observed range roughly -3e18..+4.1e18). One nextval per cloned actor_fgl_entities row.';

-- =============================================================================
-- G22 import_solido_to_basebackup dependency — per-class default templates for
-- synthesizing placeable actors from Solido JSON. Each row tells G22 how to
-- construct a fresh actor of that class without an existing source row to
-- clone. See docs/dune-research/ITEM-G22-BUILD-SPEC.md sections 4 + 5.
--
-- Per OWNER MUST be dune so Funcom's
-- pre-update pg_dump (run as the dune role) can dump it. A ls_* table owned
-- by postgres halts the entire game update (this burned the team on the
-- 2026-05-21 GA update).
--
-- v1 seeds ~22 high-value classes (2 reserved kinds: 'Totem' + 'Building',
-- plus 20 placeable classes). Expansion is empirical: operators capture
-- per-class defaults from live in-game placements via
-- scripts/capture-placeable-defaults.sh (planned future helper). Until each
-- class's default_components JSONB + component_name_hash are captured live,
-- the seed entries below contain stub values that the builder will detect
-- (NULL component_name_hash → RAISE G22_FAIL) and force the operator to fix
-- before granting against that class.
-- =============================================================================

CREATE TABLE IF NOT EXISTS dune.ls_solido_class_defaults (
  class_short_name        text    PRIMARY KEY,
  full_class_path         text    NOT NULL,
  default_properties      jsonb   NOT NULL DEFAULT '{}'::jsonb,
  default_components      jsonb   NOT NULL DEFAULT '{}'::jsonb,
  has_container_inventory boolean NOT NULL DEFAULT false,
  inventory_type          integer,
  inventory_max_count     integer,
  inventory_max_volume    real,
  component_name_hash     bigint,
  has_power_circuit       boolean NOT NULL DEFAULT false,
  notes                   text
);

ALTER TABLE dune.ls_solido_class_defaults OWNER TO dune;

-- G22 P2 additive columns (per consensus N1 + OQ1). Both nullable / safe
-- defaults so the migration is replay-idempotent against a populated table.
--
--   placeables_building_type  empirical value of dune.placeables.building_type
--                             for a freshly-placed actor of this class. Not
--                             derivable from class_short — empirically confirmed
--                             via captures (e.g. Door → 'Choam_Shelter_Door_Placeable',
--                             Choam_FloorLamp_2 → 'Choam_LightFloor_Placeable').
--                             Captured by scripts/capture-placeable-defaults.sh.
--                             Stays NULL until Phase 2b captures land. Preflight
--                             refuses any class with NULL here.
--
--   is_active                 v1 shipping gate. true = class is in the v1 surface;
--                             false = stub / deferred-to-v1.1 (preflight refuses).
--                             DEFAULT true is safe: existing stub rows still get
--                             rejected by the empty-default_components preflight
--                             check, so is_active works as an explicit kill switch
--                             on top of the data completeness check.
ALTER TABLE dune.ls_solido_class_defaults
  ADD COLUMN IF NOT EXISTS placeables_building_type TEXT,
  ADD COLUMN IF NOT EXISTS is_active                BOOLEAN NOT NULL DEFAULT true;

COMMENT ON TABLE dune.ls_solido_class_defaults IS
  'G22 import_solido_to_basebackup: per-class default templates for synthesizing placeable actors from Solido JSON. See docs/dune-research/ITEM-G22-BUILD-SPEC.md.';

-- v1 seed. 22 rows: 2 reserved kinds (Totem + Building) + 20 placeable classes.
-- default_components JSONB + component_name_hash are STUB placeholders for
-- most classes; capture live before granting against an unstubbed class.
INSERT INTO dune.ls_solido_class_defaults
  (class_short_name, full_class_path, default_properties, default_components,
   has_container_inventory, inventory_type, inventory_max_count, inventory_max_volume,
   component_name_hash, has_power_circuit, notes)
VALUES
  ('Totem',
   '/Game/Dune/Systems/Building/Pieces/BP_Totem.BP_Totem_C',
   '{"default_health": 5000.0}'::jsonb,
   '{}'::jsonb,
   false, NULL, NULL, NULL, NULL, false,
   'v1 seed; default_components captured empirically from actor 660 — STUB until live capture lands'),
  ('Building',
   '/Game/Dune/Systems/Building/Pieces/BP_DuneBuildingBase.BP_DuneBuildingBase_C',
   '{}'::jsonb,
   '{}'::jsonb,
   false, NULL, NULL, NULL, NULL, false,
   'v1 seed; building shells carry no permission_actor rows per F2 finding (spec §3)'),
  ('Generator',
   '/Game/Dune/Systems/Building/Pieces/BP_Generator_Placeable.BP_Generator_Placeable_C',
   '{"default_health": 800.0}'::jsonb,
   '{"Actor": {}, "ContainerInventory": {}}'::jsonb,
   true, 1, 5, 100.0,
   NULL, true,
   'v1 seed; component_name_hash + default_components need live capture from actor 681'),
  ('SpiceSilo',
   '/Game/Dune/Environment/Props/Interactables/BP_SpiceSiloContainer.BP_SpiceSiloContainer_C',
   '{"default_health": 500.0}'::jsonb,
   '{"Actor": {}, "ContainerInventory": {}}'::jsonb,
   true, 1, 50, 1000.0,
   NULL, false,
   'v1 seed; capture from actor 682 or 683'),
  ('GenericContainer',
   '/Game/Dune/Systems/Building/Pieces/BP_GenericContainer.BP_GenericContainer_C',
   '{"default_health": 400.0}'::jsonb,
   '{"Actor": {}, "ContainerInventory": {}}'::jsonb,
   true, 1, 30, 400.0,
   NULL, false,
   'v1 seed; component_name_hash STUB'),
  ('StorageContainer',
   '/Game/Dune/Systems/Building/Pieces/BP_StorageContainer.BP_StorageContainer_C',
   '{"default_health": 400.0}'::jsonb,
   '{"Actor": {}, "ContainerInventory": {}}'::jsonb,
   true, 1, 30, 600.0,
   NULL, false,
   'v1 seed; component_name_hash STUB'),
  ('MediumStorageContainer',
   '/Game/Dune/Systems/Building/Pieces/BP_MediumStorageContainer.BP_MediumStorageContainer_C',
   '{"default_health": 600.0}'::jsonb,
   '{"Actor": {}, "ContainerInventory": {}}'::jsonb,
   true, 1, 60, 1200.0,
   NULL, false,
   'v1 seed; component_name_hash STUB'),
  ('LargeWaterCistern',
   '/Game/Dune/Systems/Building/Pieces/BP_LargeWaterCistern.BP_LargeWaterCistern_C',
   '{"default_health": 700.0}'::jsonb,
   '{"Actor": {}, "ContainerInventory": {}}'::jsonb,
   true, 1, 10, 2000.0,
   NULL, false,
   'v1 seed; water cisterns store water units; component_name_hash STUB'),
  ('MediumWaterCistern',
   '/Game/Dune/Systems/Building/Pieces/BP_MediumWaterCistern.BP_MediumWaterCistern_C',
   '{"default_health": 500.0}'::jsonb,
   '{"Actor": {}, "ContainerInventory": {}}'::jsonb,
   true, 1, 10, 1000.0,
   NULL, false,
   'v1 seed; component_name_hash STUB'),
  ('WaterCistern',
   '/Game/Dune/Systems/Building/Pieces/BP_WaterCistern.BP_WaterCistern_C',
   '{"default_health": 400.0}'::jsonb,
   '{"Actor": {}, "ContainerInventory": {}}'::jsonb,
   true, 1, 10, 500.0,
   NULL, false,
   'v1 seed; component_name_hash STUB'),
  ('Windtrap',
   '/Game/Dune/Systems/Building/Pieces/BP_Windtrap.BP_Windtrap_C',
   '{"default_health": 500.0}'::jsonb,
   '{"Actor": {}, "ContainerInventory": {}}'::jsonb,
   true, 1, 5, 100.0,
   NULL, false,
   'v1 seed; windtraps generate water units into internal container'),
  ('WindTurbineDirectional',
   '/Game/Dune/Systems/Building/Pieces/BP_WindTurbineDirectional.BP_WindTurbineDirectional_C',
   '{"default_health": 500.0}'::jsonb,
   '{"Actor": {}}'::jsonb,
   false, NULL, NULL, NULL, NULL, true,
   'v1 seed; power-generating, no container'),
  ('WindTurbineOmnidirectional',
   '/Game/Dune/Systems/Building/Pieces/BP_WindTurbineOmnidirectional.BP_WindTurbineOmnidirectional_C',
   '{"default_health": 600.0}'::jsonb,
   '{"Actor": {}}'::jsonb,
   false, NULL, NULL, NULL, NULL, true,
   'v1 seed; power-generating, no container'),
  ('Deathstill',
   '/Game/Dune/Systems/Building/Pieces/BP_Deathstill.BP_Deathstill_C',
   '{"default_health": 400.0}'::jsonb,
   '{"Actor": {}, "ContainerInventory": {}}'::jsonb,
   true, 1, 5, 50.0,
   NULL, false,
   'v1 seed; deathstill converts corpse fluid -> water units'),
  ('Recycler',
   '/Game/Dune/Systems/Building/Pieces/BP_Recycler.BP_Recycler_C',
   '{"default_health": 500.0}'::jsonb,
   '{"Actor": {}, "ContainerInventory": {}}'::jsonb,
   true, 1, 20, 300.0,
   NULL, false,
   'v1 seed; deconstructs items into raw resources'),
  ('Fabricator',
   '/Game/Dune/Systems/Building/Pieces/BP_Fabricator.BP_Fabricator_C',
   '{"default_health": 600.0}'::jsonb,
   '{"Actor": {}, "ContainerInventory": {}}'::jsonb,
   true, 1, 30, 400.0,
   NULL, true,
   'v1 seed; generic fabricator'),
  ('SurvivalFabricator',
   '/Game/Dune/Systems/Building/Pieces/BP_SurvivalFabricator.BP_SurvivalFabricator_C',
   '{"default_health": 500.0}'::jsonb,
   '{"Actor": {}, "ContainerInventory": {}}'::jsonb,
   true, 1, 30, 400.0,
   NULL, true,
   'v1 seed; tier-1 fabricator'),
  ('WeaponsFabricator',
   '/Game/Dune/Systems/Building/Pieces/BP_WeaponsFabricator.BP_WeaponsFabricator_C',
   '{"default_health": 700.0}'::jsonb,
   '{"Actor": {}, "ContainerInventory": {}}'::jsonb,
   true, 1, 30, 400.0,
   NULL, true,
   'v1 seed'),
  ('WearablesFabricator',
   '/Game/Dune/Systems/Building/Pieces/BP_WearablesFabricator.BP_WearablesFabricator_C',
   '{"default_health": 700.0}'::jsonb,
   '{"Actor": {}, "ContainerInventory": {}}'::jsonb,
   true, 1, 30, 400.0,
   NULL, true,
   'v1 seed'),
  ('VehiclesFabricator',
   '/Game/Dune/Systems/Building/Pieces/BP_VehiclesFabricator.BP_VehiclesFabricator_C',
   '{"default_health": 1000.0}'::jsonb,
   '{"Actor": {}, "ContainerInventory": {}}'::jsonb,
   true, 1, 50, 1000.0,
   NULL, true,
   'v1 seed; tier-3+ requires power'),
  ('Door',
   '/Game/Dune/Systems/Building/Pieces/BP_Choam_Shelter_Door.BP_Choam_Shelter_Door_C',
   '{"default_health": 300.0}'::jsonb,
   '{"Actor": {}}'::jsonb,
   false, NULL, NULL, NULL, NULL, false,
   'v1 seed; capture from actor 679 (the door in the operator''s test base)'),
  ('Hark_StandingLight_01',
   '/Game/Dune/Systems/Building/Pieces/BP_Hark_StandingLight_01.BP_Hark_StandingLight_01_C',
   '{"default_health": 200.0}'::jsonb,
   '{"Actor": {}}'::jsonb,
   false, NULL, NULL, NULL, NULL, true,
   'v1 seed; decorative light, power-circuit participant')
ON CONFLICT (class_short_name) DO UPDATE SET
  full_class_path         = EXCLUDED.full_class_path,
  default_properties      = EXCLUDED.default_properties,
  default_components      = EXCLUDED.default_components,
  has_container_inventory = EXCLUDED.has_container_inventory,
  inventory_type          = EXCLUDED.inventory_type,
  inventory_max_count     = EXCLUDED.inventory_max_count,
  inventory_max_volume    = EXCLUDED.inventory_max_volume,
  component_name_hash     = EXCLUDED.component_name_hash,
  has_power_circuit       = EXCLUDED.has_power_circuit,
  notes                   = EXCLUDED.notes;

-- =============================================================================
-- G22 v1 SHIPPING SCOPE — Phase 2b captures (2026-05-25)
-- =============================================================================
-- v1 ships with N=4 active classes. The 22-row stub seed above is preserved as
-- documentation of the v1 target list, but only the 4 captures below have real
-- default_components + placeables_building_type (where applicable). The other
-- 18 are flipped to is_active=false at the end of this block so the G22 N2
-- preflight refuses any Solido that references them — better to refuse cleanly
-- than to silently produce broken bases.
--
-- Captured in this session:
--   - Generator  (the operator actor 3460, normal construction tool)
--   - Door       (the operator actor 3476, normal construction tool, mounted on wall)
--   - Totem      (indirect from live actor 3401)
--   - Building   (indirect from live actor 3403)
--
-- the operator paused before the remaining 18 placements. v1.1 backfill via
-- scripts/capture-placeable-defaults.sh once he can place again.
-- =============================================================================

INSERT INTO dune.ls_solido_class_defaults
  (class_short_name, full_class_path, default_properties, default_components,
   has_container_inventory, inventory_type, inventory_max_count, inventory_max_volume,
   component_name_hash, has_power_circuit, notes,
   placeables_building_type, is_active)
VALUES
  -- Generator (the operator actor 3460, fresh placement — m_FuelBurningId="None", clean defaults)
  ('Generator','/Game/Dune/Systems/Building/Pieces/BP_Generator_Placeable.BP_Generator_Placeable_C',
   '{"default_health": 2500}'::jsonb,
   '{"Actor": {"FHealthComponent": [0, {"m_CurrentHealth": 2500.0, "m_MaxDownButNotOutStateHealth": 0.0, "m_CurrentDownButNotOutStateHealth": 0.0}], "FShelterComponent": [0, {"m_ShelteredPercentage": 0.0, "m_IsShelteredTraceResults": [false, false, false, false, false, false, false, false, false]}], "FPlaceableComponent": [0, {"m_bHasSocketlessConnections": false}], "FAggroControllerComponent": [0, {"m_TotalDamageDone": 0.0}], "FPowerCircuitElementComponent": [0, {"m_bForceOff": false, "m_bIsEnabled": true, "m_ConnectedCircuit": 1}], "FFuelPoweredPlaceableComponent": [0, {"m_FuelBurningId": {"Name": "None"}, "m_FuelBurningDuration": 0.0, "m_FuelBurningInitialTime": 0.0, "m_FuelBurningPassedTimeSinceStart": 0.0}]}, "ContainerInventory": {}}'::jsonb,
   true, 3, 5, 100,
   1264785389, true,
   'v1 active; captured from the operator actor 3460 on 2026-05-25 (fresh placement)',
   'Generator_Placeable', true),

  -- Door (the operator actor 3476 — empirical building_type 'Choam_Shelter_Door_Placeable',
  -- NOT 'Door_Placeable'; this case validates the registry-source column add)
  ('Door','/Game/Dune/Systems/Building/Pieces/BP_Choam_Shelter_Door.BP_Choam_Shelter_Door_C',
   '{"default_health": 2500}'::jsonb,
   '{"Actor": {"FDoorComponent": [0, {"m_DoorState": "ClosedAutomatically", "m_bShouldDoorCloseAutomatically": true, "m_bShouldDoorOpenAutomaticallyOnFoot": true, "m_bShouldDoorOpenAutomaticallyInVehicle": true}], "FHealthComponent": [0, {"m_CurrentHealth": 2500.0, "m_MaxDownButNotOutStateHealth": 0.0, "m_CurrentDownButNotOutStateHealth": 0.0}], "FShelterComponent": [0, {"m_ShelteredPercentage": 0.333333, "m_IsShelteredTraceResults": [true, false, false, false, false, true, true, false, false]}], "FPlaceableComponent": [0, {"m_bHasSocketlessConnections": false}], "FAggroControllerComponent": [0, {"m_TotalDamageDone": 0.0}]}}'::jsonb,
   false, NULL, NULL, NULL,
   NULL, false,
   'v1 active; captured from the operator actor 3476 on 2026-05-25 (mounted on wall)',
   'Choam_Shelter_Door_Placeable', true),

  -- Totem (indirect capture from live actor 3401; Totem IS in dune.placeables
  -- empirically, so building_type='Totem_Placeable' lands. Components include
  -- FTotemComponent, FTotemLandclaimComponent, FInventoryCircuitElementComponent,
  -- FAudioTotemPlaceablesInfoComponent — these drive sub-fief mechanics)
  ('Totem','/Game/Dune/Systems/Building/Pieces/BP_Totem.BP_Totem_C',
   '{"default_health": 2500}'::jsonb,
   '{"Actor": {"FTotemComponent": [0, {}], "FHealthComponent": [0, {"m_CurrentHealth": 2500.0, "m_MaxDownButNotOutStateHealth": 0.0, "m_CurrentDownButNotOutStateHealth": 0.0}], "FShelterComponent": [0, {"m_ShelteredPercentage": 0.222222, "m_IsShelteredTraceResults": [false, false, false, false, true, false, false, true, false]}], "FPlaceableComponent": [2, {"m_bHasSocketlessConnections": false}], "FTotemLandclaimComponent": [0, {"m_BoundingCircleRadius": 11412.703125, "m_PendingStakingUnitsEntityIds": [], "m_PendingVerticalStakingUnitsEntityIds": []}], "FAggroControllerComponent": [0, {"m_TotalDamageDone": 0.0}], "FPowerCircuitElementComponent": [4, {"m_bForceOff": false, "m_bIsEnabled": true, "m_ConnectedCircuit": 1}], "FInventoryCircuitElementComponent": [0, {"m_bIsEnabled": true, "m_OutputCircuit": 1, "m_ConnectedCircuit": 1}], "FAudioTotemPlaceablesInfoComponent": [0, {"m_ActiveFabricatorCount": 0, "m_ActiveOreRefineryCount": 0, "m_ActivePowerGeneratorCount": 0}]}, "ContainerInventory": {}}'::jsonb,
   true, 3, 5, 50,
   1264785389, true,
   'v1 active; indirect capture from live actor 3401 on 2026-05-25 (reserved kind, never appears in _g22_stage_placeables but mint at step 7 looks up default_components)',
   'Totem_Placeable', true),

  -- Building (indirect capture from live actor 3403; BP_DuneBuildingBase is NOT
  -- in dune.placeables, so placeables_building_type stays NULL — correct, since
  -- Building is the shell that houses building_instances, not a placeable itself.
  -- N2 preflight only checks staged placeables, so NULL here doesn't trip anything)
  ('Building','/Game/Dune/Systems/Building/Pieces/BP_DuneBuildingBase.BP_DuneBuildingBase_C',
   '{"default_health": 0}'::jsonb,
   '{"Actor": {"FHealthComponent": [0, {"m_CurrentHealth": 0.0, "m_MaxDownButNotOutStateHealth": 0.0, "m_CurrentDownButNotOutStateHealth": 0.0}], "FAggroControllerComponent": [0, {"m_TotalDamageDone": 0.0}], "FBiomeWeatherModifierComponent": [0, {"m_CurrentSandColor": {"A": 1.0, "B": 0.06859, "G": 0.145263, "r": 0.428689}, "CurrentSandBuildupModifier": 0.3, "CurrentTemperatureModifier": 1.0}]}}'::jsonb,
   false, NULL, NULL, NULL,
   NULL, false,
   'v1 active; indirect capture from live actor 3403 on 2026-05-25 (reserved kind; default_health=0 is correct — building shells have no real HP, damage flows through building_instances)',
   NULL, true)
ON CONFLICT (class_short_name) DO UPDATE SET
  full_class_path          = EXCLUDED.full_class_path,
  default_properties       = EXCLUDED.default_properties,
  default_components       = EXCLUDED.default_components,
  has_container_inventory  = EXCLUDED.has_container_inventory,
  inventory_type           = EXCLUDED.inventory_type,
  inventory_max_count      = EXCLUDED.inventory_max_count,
  inventory_max_volume     = EXCLUDED.inventory_max_volume,
  component_name_hash      = EXCLUDED.component_name_hash,
  has_power_circuit        = EXCLUDED.has_power_circuit,
  notes                    = EXCLUDED.notes,
  placeables_building_type = EXCLUDED.placeables_building_type,
  is_active                = EXCLUDED.is_active;

-- v1.1 backlog: mark the 18 uncaptured v1-seed classes as is_active=false so
-- the G22 N2 preflight refuses any Solido that references them. Capture via
-- scripts/capture-placeable-defaults.sh once the operator can place again.
UPDATE dune.ls_solido_class_defaults
   SET is_active = false,
       notes     = 'v1.1 backlog: empirical capture pending — Phase 2b paused with N=4 active (Generator + Door + Totem + Building); the other 18 v1-seed classes await capture via scripts/capture-placeable-defaults.sh'
 WHERE class_short_name IN (
   'SpiceSilo', 'GenericContainer', 'StorageContainer', 'MediumStorageContainer',
   'LargeWaterCistern', 'MediumWaterCistern', 'WaterCistern', 'Windtrap',
   'WindTurbineDirectional', 'WindTurbineOmnidirectional',
   'Deathstill', 'Recycler', 'Fabricator', 'SurvivalFabricator',
   'WeaponsFabricator', 'WearablesFabricator', 'VehiclesFabricator',
   'Hark_StandingLight_01'
 );

-- =============================================================================
-- Moderation trio (Phase C, 2026-05-29): kick/ban/unban audit + ban registry.
-- See docs/dune-research/MODERATION-AND-DRILLDOWN-DESIGN-2026-05-29.md section 2b.
--
-- Two tables, both OWNER dune per so
-- Funcom's pre-update pg_dump (run as the dune role) can dump them. A ls_*-
-- style table owned by postgres halts the entire game update (2026-05-21 burn).
--
-- 1. lsadmin.bans: durable FLS-ID-keyed ban registry. One row per fls_id
--    (UNIQUE). active=true means the ban-watcher will re-kick the player every
--    30s while they are online (or expires_at < NOW(), whichever comes first).
--    Unbanning sets active=false + records unbanned_at/unban_reason/unbanned_by;
--    we keep the row for history.
--
-- 2. lsadmin.player_actions: append-only event log for kick/ban/unban actions.
--    Mirrors dune.ls_progression_grants in spirit but lives in lsadmin.* so
--    moderation history is separable from progression grants.
--
-- 3. lsadmin.player_ips: player -> source-IP map harvested from game-pod
--    LogNet lines (RemoteAddr + AccountId + Fls on one line). REVIVED 2026-06-02:
--    the FLS-ID native RMQ kick (KickPlayer ServerCommand) is a confirmed
--    silent no-op on the current GA build (publishes ok, never disconnects;
--    verified live + corroborated by every community repo + the owners Discord,
--   ). The ONLY mechanism that actually
--    disconnects a player is the iptables source-IP DROP on the hostNetwork
--    game node (ReditusDraco's model). FLS-ID > IP for evasion-resistance, but
--    an IP drop that WORKS beats an FLS kick that does not. The IP map is the
--    prerequisite. (Caveat surfaced in the admin UI: IP bans are VPN-avoidable.)
--
-- Apply with the same psql -f harness as the rest of this file.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS lsadmin;
ALTER SCHEMA lsadmin OWNER TO dune;

CREATE TABLE IF NOT EXISTS lsadmin.bans (
  id                bigserial   PRIMARY KEY,
  fls_id            text        NOT NULL UNIQUE,    -- target FuncomId
  account_id        bigint,                          -- best-effort link to dune.accounts.id
  reason            text        NOT NULL,
  note              text,
  duration_minutes  integer,                         -- NULL = permanent
  banned_at         timestamptz NOT NULL DEFAULT now(),
  expires_at        timestamptz,                     -- NULL = permanent; watcher only re-kicks while active AND (expires_at IS NULL OR expires_at > NOW())
  active            boolean     NOT NULL DEFAULT true,
  banned_by         text        NOT NULL,            -- admin username
  unbanned_at       timestamptz,
  unban_reason      text,
  unbanned_by       text
);

ALTER TABLE lsadmin.bans OWNER TO dune;

-- Watcher hot-path: every 30s, scan active non-expired bans + JOIN
-- dune.encrypted_player_state on account_id to find online targets. The
-- (active, expires_at) index keeps that scan O(active-bans).
CREATE INDEX IF NOT EXISTS bans_active_expires_idx
  ON lsadmin.bans (active, expires_at)
  WHERE active = true;

CREATE INDEX IF NOT EXISTS bans_account_id_idx
  ON lsadmin.bans (account_id);

CREATE TABLE IF NOT EXISTS lsadmin.player_actions (
  id                bigserial   PRIMARY KEY,
  idempotency_key   uuid        UNIQUE,            -- nullable for the watcher's
                                                   -- auto-expire rows; required
                                                   -- on relay-driven actions.
  account_id        bigint,
  fls_id            text,
  action_type       text        NOT NULL
                                CHECK (action_type IN ('kick', 'ban', 'unban')),
  reason            text,
  note              text,
  duration_minutes  integer,
  admin_user        text        NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE lsadmin.player_actions OWNER TO dune;

-- Idempotency column add (safe replay on existing tables).
ALTER TABLE lsadmin.player_actions
  ADD COLUMN IF NOT EXISTS idempotency_key uuid;
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'lsadmin.player_actions'::regclass
       AND conname  = 'player_actions_idempotency_key_key'
  ) THEN
    ALTER TABLE lsadmin.player_actions
      ADD CONSTRAINT player_actions_idempotency_key_key UNIQUE (idempotency_key);
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS player_actions_account_idx
  ON lsadmin.player_actions (account_id, created_at DESC);

CREATE INDEX IF NOT EXISTS player_actions_action_idx
  ON lsadmin.player_actions (action_type, created_at DESC);

-- 3. lsadmin.player_ips: account_id -> source IP map for the iptables kick/ban.
--    One row per (account_id, ip_address); last_seen bumped on every harvest so
--    "recent IPs" = rows with last_seen within the ban-scope window. fls_id +
--    character_name are best-effort labels captured from the same LogNet line.
CREATE TABLE IF NOT EXISTS lsadmin.player_ips (
  id              bigserial   PRIMARY KEY,
  account_id      bigint      NOT NULL,
  ip_address      inet        NOT NULL,
  fls_id          text,                              -- UniqueId: Fls:<hex> from the log line
  character_name  text,                              -- best-effort, may be null
  first_seen      timestamptz NOT NULL DEFAULT now(),
  last_seen       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (account_id, ip_address)
);

ALTER TABLE lsadmin.player_ips OWNER TO dune;

-- Kick/ban hot-path: resolve recent IPs for one account, newest first.
CREATE INDEX IF NOT EXISTS player_ips_account_lastseen_idx
  ON lsadmin.player_ips (account_id, last_seen DESC);

-- Reverse lookup (which account owns an IP) for audit / shared-IP checks.
CREATE INDEX IF NOT EXISTS player_ips_ip_idx
  ON lsadmin.player_ips (ip_address);

-- =============================================================================
-- W6 Spice-spawn toggle (VC2 P2, 2026-05-30): per-field-type append-only log of
-- is_spawning_active flips. See docs/dune-research/COMMUNITY-WINS-IMPLEMENTATION-
-- PATH-2026-05-30.md "W6: Spice-spawn toggle". v1 = boolean only (Decision A).
--
-- The toggle itself UPDATEs dune.spicefield_types.is_spawning_active; this table
-- records who/when/what for the audit trail. OWNER dune per
-- so Funcom's pre-update pg_dump (run as
-- the dune role) can dump it; a lsadmin.* table owned by postgres halts the
-- entire game update (2026-05-21 burn). change_id = idempotency/correlation id
-- minted in the admin-backend router (uuid4).
-- =============================================================================

CREATE TABLE IF NOT EXISTS lsadmin.spicefield_toggle_log (
  id          bigserial PRIMARY KEY,
  ts          timestamptz NOT NULL DEFAULT now(),
  who         text NOT NULL,
  type_id     int NOT NULL,
  new_value   bool NOT NULL,
  change_id   text NOT NULL
);
ALTER TABLE lsadmin.spicefield_toggle_log OWNER TO dune;
