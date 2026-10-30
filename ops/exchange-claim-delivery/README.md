# Exchange "Completed tab" claim delivery

The third delivery lane, and the only **unconditional** one. 

## Why this lane exists

| lane | requires | fails how |
|---|---|---|
| DB insert (`grant.sh`) | player **OFFLINE** | mid-session write collides on `position_index` (BUG-011) |
| RMQ `AddItemToInventory` | player **ONLINE**, grade 0, **and the items must fit** | overflow **drops on the ground** |
| **exchange claim** | nothing | - |

The exchange lane writes **nothing to a player inventory**. The item sits in the exchange's own
inventory (610, exchange_id 2) until the player collects it at a terminal, at which point the
engine's own `dune_exchange_retrieve_storage_item` moves it. No collision, no RAM clobber, no
capacity ceiling, no online/offline gate.

That matters concretely: the v1.6 welcome pack needs **31 slots / 152.5+ volume against a 35-slot /
175-volume starter bag**, so it cannot all be delivered over RMQ. The ornithopter kit alone is 10
slots / 76 volume. Handing that subset to the player through the tradepost exchange is both the fix
and a better story than a second-login delivery.

## Proven, 2026-07-25, without writing anything

n0logic had a real unclaimed item in his own Completed tab (order 238246, `T6Machinery` x7,
`completion_type 3`, `owner_id 3041`). He collected it mid-session while the DB was being inspected:

```
item 1500751834   was: inventory 610 (exchange-held)   now: inventory 2879 (main backpack, slot 34)
order 238246      gone
```

So the format renders, is claimable, places correctly into a free slot **while the player is
online**, and cleans itself up on collection. `owner_id` is the **`player_controller_id`**.

## Still unproven, what `claim-test.sql` is for

1. Does a **synthetic** order render, or does the client need real trade history behind it?
2. Does **`expiration_time = NULL`** display correctly?

(2) is the important one. `dune_exchange_purge_completed_orders` DELETEs a completed order once
`in_current_time >= expiration_time`, on the **game clock**, the same one whose skew culled the
CHOAM payouts in BUG-010. Its WHERE clause only touches rows where `expiration_time IS NOT NULL`, so
a NULL expiry is permanently safe. Every one of the 6,028 live orders has one set purely because the
engine always sets one.

## Running it

```bash
sudo /root/dq.sh -v ON_ERROR_STOP=1 < claim-test.sql          # prints new_order_id + new_item_id
sudo /root/dq.sh -v ON_ERROR_STOP=1 \
     -v order_id=<id> -v item_id=<id> < rollback-claim.sql    # only while still unclaimed
```

`claim-test.sql` mirrors order 238246 exactly, `completion_type 3`, self-referencing
`original_order_id`, `is_npc_order` false, changing only the item and the expiry. INSERTs only; no
existing row is updated or deleted.

`rollback-claim.sql` refuses to run if the item has left the exchange inventory, because at that
point the player owns it and deleting it would be taking an item off them.

⚠️ **This is a live CHOAM exchange write.** It is the one system where a malformed row reaches other
players' trades. The standing rule "never touch non-NPC orders" does not cover synthetic orders
*owned by* a player, that is a new category, and it needs owner sign-off every time until this lane
has a track record.
