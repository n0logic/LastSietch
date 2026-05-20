# Canonical item template_ids

A growing catalog of item template_ids used by `dune.items`, `dune.vehicle_modules`, and related tables. Useful when granting items via direct postgres writes (admin tooling, welcome packages, debugging).

These slugs come from `dune.gaming.tools` URLs and have been spot-verified in the live database. Tier suffix is the Mk number minus an offset is not exact - e.g. `_6` = Mk6 here.

## Naming conventions and gotchas

Read this section before guessing a slug - the naming is inconsistent in ways that cause real mistakes.

### `Sda` = Sidearm (pistol), NOT sword

The `Sda` token in a template_id means **Sidearm**, i.e. a pistol. It does **not** mean "sword". `ChoamSda1` is a Mk1 pistol; `UniqueSda_Doubleshot_04` is a unique double-barrel pistol. Granting an `Sda` slug expecting a melee weapon is a common and costly error.

The actual sword/melee families use these prefixes:

- `UniqueSword_*` - unique long blades
- `CHOAMSword_*` - standard CHOAM swords
- `Kindjal*` - kindjals
- `Blade*` - blades
- `Dirk*` - dirks
- `Rapier*` - rapiers

### Tier vs Mk

The `_N` suffix on a template_id is usually the **Mk** level (1-7) within a family. The `RCP_TN_*_Recipe` prefix encodes the **Tier** (tech-level gating, usually 1-6). They are different numbering schemes - do not assume `_5` means Tier 5.

### Case sensitivity

Game-spawned items use **CamelCase** template_ids; script-granted items often use **lowercase**. Observed: the database treats `template_id` case-insensitively in practice for matching purposes, so `combat_choam_light06_helmet` and `Combat_Choam_Light06_Helmet` resolve to the same item.

**Always use case-insensitive `WHERE` clauses when matching** - e.g. `WHERE lower(template_id) = lower('UniqueSword_05')`, or list both forms in an `IN (...)`. The DB stores whatever form you INSERT.

Assumption: `dune.items` tends to hold lowercase forms and `dune.vehicle_modules` tends to hold PascalCase forms, but treat the table-specific casing as something to verify on your own deployment rather than rely on blindly.

## Armor

### CHOAM Light - Tier 6 set (quality 5)

A full 5-piece set, used by the v1.x welcome pack:

| Slot | template_id |
|---|---|
| Head | `combat_choam_light06_helmet` |
| Chest | `combat_choam_light06_top` |
| Legs | `combat_choam_light06_bottom` |
| Hands | `combat_choam_light06_gloves` |
| Feet | `combat_choam_light06_boots` |

### Stillsuits

| Set | template_ids |
|---|---|
| Unique "Batigh" - Tier 5 | `stillsuit_unique_efficient_05_mask`, `stillsuit_unique_efficient_05_top`, `stillsuit_unique_efficient_05_gloves`, `stillsuit_unique_efficient_05_boots` |
| Unique Efficient - Tier 4 variant | `Stillsuit_Unique_Efficient_04_mask`, `Stillsuit_Unique_Efficient_04_top`, `Stillsuit_Unique_Efficient_04_gloves`, `Stillsuit_Unique_Efficient_04_boots` |

Assumption: the T4 variant piece suffixes mirror the T5 set (`_mask` / `_top` / `_gloves` / `_boots`) - verify the exact casing/suffixes against the DB or `dune.gaming.tools` before granting.

### Other armor

- `Combat_Nati_SandtroutLeathers01_*` - starter Bandit Leathers set (Helmet/Top/Bottom/Gloves/Boots)

## Weapons

| template_id | Item | Notes |
|---|---|---|
| `UniqueSword_05` | Replica Pulse-sword | Tier 6 unique long blade. Verified via `dune.gaming.tools/items/uniquesword_05`. |
| `UniqueSword_04` | Unique sword | Tier 6 variant |
| `UniqueSda_Doubleshot_04` | Unique double-shot sidearm | A pistol (`Sda` = sidearm) |
| `UniqueAr2` | Unique assault rifle | Mk2 (Assumption) |
| `ChoamSda1` | CHOAM Sidearm Mk1 | A pistol; starter weapon |

Reminder: `Sda` entries are pistols. See the naming gotchas above.

## Tools / equipment

| template_id | Item | Notes |
|---|---|---|
| `repairtool5` | Mk5 Welding Torch | Used to weld vehicle parts together. Lower Mk: `repairtool` (Mk1), `repairtool3` (Mk3). |
| `vehiclebackuptool` | Vehicle Backup Tool | Tier 1 Common; needed to store/recall vehicles |
| `BasicBuildingTool` | Building Tool | Granted by research progression |
| `Binoculars_1` | Binoculars | |
| `MiningTool_1h_Standard` | Mk1 1-handed Cutteray | Starter mining tool |
| `miningtool_2h_light` | Cutteray Mk5 | 2-handed mining tool, not a weapon |
| `BodyFluidExtractor` | Bodyfluid Extractor | |
| `RespawnBeacon` | Respawn Beacon | Starter item |
| `healthpack_channeled` | Standard Health Pack | |
| `fullsuspensorbelt` | Suspensor Belt | Tier 4 |
| `powerpack4` | Mk6 Power Pack | Assumption: the `_4` suffix maps to Mk6 (Funcom's internal numbering is offset for this family). |
| `holtzmanshieldactivedrain3` | Mk5 Holtzman Shield | |
| `decajon` | Decaliterjon (canteen) | Fillable - holds water and fuel. Pre-fill by writing its `stats` jsonb directly. |
| `weldingmaterial` | Welding Wire | Consumable for the welding torch |

## Ornithopter - Scout (Light) Mk6

Required for a fully assembled Scout Mk6:

| UI name | template_id (items) | notes |
|---|---|---|
| Hull (Body) | `ornithopterlighthullback_6` | "Hull Mk6" in UI. Provides 2625 durability, 1500 armor, 1 utility slot. Attaches to chassis with welding torch. |
| Cockpit | `ornithopterlighthullfront_6` | "Cockpit Mk6" in UI. Houses pilot seat. Required mount point for the Scan Module. |
| Chassis | `ornithopterlightchassis_6` | "Chassis Mk6" in UI. 3500 durability, 1500 armor, 400 fuel capacity. Central component. |
| Wing (×4) | `ornithopterlightlocomotion_6` | Need 4 to fly. Same template for all 4 positions in `dune.items`; position-specific suffixes only appear in `dune.vehicle_modules`. |
| Engine | `ornithopterlightengine_6` | Defines max speed. |
| Generator | `ornithopterlightgenerator_6` | Power source. |
| Thruster | `ornithopterlightboost_6` | Optional speed boost; risks overheating. Mounts on body hull. |
| Storage | `ornithopterlightinventory_6` | Optional inventory module. Mounts on body hull. (slug pattern verified at Mk4) |
| Scan Module | `ornithopterlightscanner_6` | Optional scanner. Attaches to cockpit with welding torch. (slug pattern verified at Mk4) |
| Rocket Launcher | `ornithopterlightlauncher_6` | Optional weapon module. |

To assemble, players also need:

- `repairtool5` - Mk5 Welding Torch (the tool used to weld parts together)
- `vehiclebackuptool` - Vehicle Backup Tool (to store/recall the finished vehicle)
- `weldingmaterial` - Welding Wire (consumable for the torch)

### Vehicle-module CamelCase variants

`dune.vehicle_modules` stores these in PascalCase, and the wing modules are **position-specific** there:

```
OrnithopterLightChassis_6, OrnithopterLightHullBack_6, OrnithopterLightHullFront_6,
OrnithopterLightEngine_6, OrnithopterLightGenerator_6, OrnithopterLightBoost_6,
OrnithopterLightLocomotionFrontRight_6, OrnithopterLightLocomotionFrontLeft_6,
OrnithopterLightLocomotionBackRight_6, OrnithopterLightLocomotionBackLeft_6
```

## Ornithopter - Carrier Mk6

| UI name | template_id |
|---|---|
| Main Hull | `ornithoptertransporthull_6` |
| Chassis | `ornithoptertransportchassis_6` |
| Engine | `ornithoptertransportengine_6` |
| Generator | `ornithoptertransportgenerator_6` |
| Wing | `ornithoptertransportlocomotion_6` |
| Thruster | `ornithoptertransportboost_6` |

## Resources / currency

| template_id | Item | Notes |
|---|---|---|
| `SolarisCoin` | Solari Coins | Stack-size equals the coin balance |
| `FuelCanister_Large` | Large Fuel Cell | Crafted. PascalCase even in `dune.items`. |
| `Oil` | Raw fuel resource | |
| `ScrapMetal` | Salvaged Metal | PascalCase in `dune.items` |
| `weldingmaterial` | Welding Wire | Consumable |
| `Stone` | Stone | Base rock |

## Cosmetics (starter outfit)

| template_id | slot |
|---|---|
| `Social_Choam_MaulaCastOffs01_Top_Fremkit` | top |
| `Social_Choam_MaulaCastOffs01_Bottom` | bottom |
| `Social_Choam_MaulaCastOffs01_Gloves` | gloves |
| `Social_Choam_MaulaCastOffs01_Shoes` | shoes |
| `Combat_Nati_SandtroutLeathers01_*` | combat outfit (Helmet/Top/Bottom/Gloves/Boots) |

## How to validate a new slug

Before INSERTing an unknown template_id:

1. **Web check:** load `https://dune.gaming.tools/items/<slug>` - should return an item page, not 404.
2. **DB check:** search `dune.items` for any existing rows with the same slug (use a case-insensitive match - `WHERE lower(template_id) = lower('<slug>')`). If present, it's been instantiated before and is known-good.
3. **Variant check:** if it's a vehicle module, also check `dune.vehicle_modules` with the PascalCase form (`OrnithopterLightX_6`).
4. **Fallback - INSERT and observe:** if a template_id is wrong, the item shows as null/broken in-game; DELETE and try a variant. Always set `acquisition_time = 0` on grant-INSERTs so they're tagged for later cleanup.

## Future expansion

- Sandbike Mk6 components (`sandbike*_6` family)
- Treadwheel parts
- Faction-specific weapons (Atreides, Harkonnen)
- Maker hook (the worm-summoning thumper variants)

Pull requests welcome - drop a row into the right table with the template_id, UI name, and any notes.
