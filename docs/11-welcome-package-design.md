# 11. Sietch Welcome Package

An automated "welcome package" that grants every new player on the server a starter kit of items, currency, and unlocked research the first time they join. It works by writing directly to the server's PostgreSQL database - the same DB the game-server pods read.

This feature is **implemented and working**. The reference implementation is in this repo: [scripts/welcome-pack-grant.sh](../scripts/welcome-pack-grant.sh). This doc explains how it works and the schema facts it relies on.

## What the pack delivers

A single grant gives a new player:

- **Armor** - a full 5-piece light armor set plus a suspensor belt, power pack, and Holtzman shield
- **Tools** - mining tool, welding torch, vehicle backup tool
- **Vehicle parts** - a complete set of ornithopter (Scout) components: chassis, hull, cockpit, four wings, engine, generator, thruster
- **Fuel and welding materials** - fuel canisters and welding wire to assemble and run the ornithopter
- **A currency stack** - a stack of House Scrip
- **Base-construction research** - flips a set of base-construction recipes to `Purchased` so the player can build immediately
- **A pre-filled canteen** - a "decajon" canteen item whose water and fuel levels are set at insert time by writing its `stats` jsonb directly

Items land in the player's MAIN backpack and HOTBAR (see the slot-collision gotcha below for why placement matters).

## Schema map (the grant surface)

| Goal | Table | Notes |
|---|---|---|
| Grant items (armor, tools, vehicle parts, fuel, currency stack) | `dune.items` | Insert into the player's MAIN backpack (`inventory_type=0`) and HOTBAR (`inventory_type=15`). Template_ids are case-insensitive in practice. |
| Grant currency | `dune.player_virtual_currency_balances` | PK `(player_controller_id, currency_id)`, `balance bigint`. `currency_id=1` is House Scrip, a Landsraad virtual currency (confirmed). Keyed by the player's controller actor, not the account. |
| Flip base-construction research to Purchased | `dune.actors` (`properties` jsonb) | Research lives as a jsonb array at `properties#>'{TechKnowledgePlayerComponent,m_TechKnowledge,m_TechKnowledgeData}'`. The grant rebuilds the array, setting `UnlockedState=Purchased` on the named recipe keys. |
| Idempotency tracking | `dune.welcome_pack_grants` | Created once by the operator. One row per `account_id`; the `notes` column carries a version tag. |

Tables the welcome package explicitly does **not** touch:

- `dune.encrypted_player_state` - read-only here; used only to resolve `account_id` → controller/pawn IDs. Sensitive linkage owned by gameplay code.
- `dune.journey_story_node` - complex per-node jsonb quest state. High risk of breaking quest logic. Out of scope.

## CRITICAL: the slot-collision gotcha

`dune.items` has **no unique constraint on `(inventory_id, position_index)`**. Multiple rows can occupy the same slot, and the game client renders **only the lowest-id row per slot**. Any item written to a slot a lower-id row already occupies becomes shadowed - invisible to the player.

Therefore:

- **Welcome-pack INSERTs into the MAIN backpack MUST append at `MAX(position_index)+1`.** Never write to a fixed backpack position. The grant script computes the next free index with `SELECT COALESCE(MAX(position_index), -1) + 1 FROM dune.items WHERE inventory_id = <main_id>`.
- **Never write to worn/equipped slots (`inventory_type=1`).** Worn slots are managed by gameplay code; writing there produces shadowed, invisible gear.
- Early versions of this feature wrote to fixed positions and produced shadowed items that later had to be relocated. Appending is the fix - do not regress on this.

The HOTBAR (`inventory_type=15`) uses fixed positions 1-4 in the reference script because a brand-new character's hotbar is empty. If you adapt this for existing players, append there too rather than assuming the slots are free.

## Delivery mechanism

**Chosen approach: external watcher.** A poller monitors `dune.encrypted_player_state` for new account rows and grants the pack out-of-band, run as a **systemd timer**. Grants happen outside the game's own transactions, so a failure in the grant logic cannot break character creation or any other game flow.

The alternative - a Postgres `AFTER INSERT` trigger - was **rejected as too risky**: a trigger runs inside Funcom's own character-creation transaction, so any error there would break new-character creation entirely.

The grant logic itself is [scripts/welcome-pack-grant.sh](../scripts/welcome-pack-grant.sh): it resolves an `account_id` to its actor and inventory IDs, checks the tracking table, and performs all INSERTs/updates in a single transaction. The watcher's job is just to detect new accounts and invoke the grant script for each one.

## Idempotency

`dune.welcome_pack_grants` records every grant, one row per `account_id`. Before granting, the script checks this table; if the account already has a current-version pack it exits without doing anything. The `notes` column doubles as a version tag, so a pack revision can be detected separately from a never-granted account. This guarantees a player is never granted twice.

Create the tracking table once before first use:

```sql
CREATE TABLE dune.welcome_pack_grants (
  account_id    bigint PRIMARY KEY,
  granted_at    timestamptz NOT NULL DEFAULT now(),
  granted_items integer     NOT NULL DEFAULT 0,
  notes         text
);
```

## Future option: deliver into a storage container

**Assumption / researched but not implemented:** instead of writing into player inventory, the pack could be delivered into a placeable storage container (a deployed chest) owned by the new player. A container has its own inventory with ample, predictable slots, which would sidestep the `(inventory_id, position_index)` slot-collision issue entirely - there is no pre-existing player-owned content to shadow. This has not been built or validated; treat it as a research direction, not a recommendation.

## Related

- [scripts/welcome-pack-grant.sh](../scripts/welcome-pack-grant.sh) - the reference grant implementation
- [references/canonical-item-ids.md](../references/canonical-item-ids.md) - item template_ids used by the pack
- [07-troubleshooting.md](07-troubleshooting.md) - note the progression-reset issue: level/XP/skill-points are RAM-only, but pack items and currency persist
