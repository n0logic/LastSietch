# Last Sietch Dune Telemetry Logger

A passive, read-only telemetry collector for the Last Sietch Dune Awakening
server. It polls the game database with `SELECT`-only queries and writes the
results to its own SQLite store, then serves that history over a small
localhost-bound read API.

## What it does

The logger runs a single-threaded scheduler with these streams and jobs, each
on its own env-tunable cadence:

| Unit | Source | Writes |
|---|---|---|
| `presence` | `dune.encrypted_player_state` | online-player roll, one row/player/sweep |
| `world` | `dune.actors` counts | server-wide counters (subfiefs, structures, vehicles) |
| `connections` | survival pod logs + geoip | new inbound connection IPs, geolocated |
| `roster` | `dune.player_state` join | online roster with names/map/guild/faction (PII) |
| `combat` | `dune.game_events` | all game events; deaths resolve victim + killer |
| `vehicles` | `dune.overmap_players` join | flying-vehicle position snapshots |
| `positions` | `dune.overmap_players` join | live-map player coords (PII-safe, coords-only) |
| `grant_events` | `dune.ls_progression_grants` | per-grant audit copy from the ledger |
| `progression` | `dune.encrypted_player_state` + `fgl_entities` | per-player XP/level/SP/intel snapshot + level-up events |
| `flight_distance` (job) | own `vehicle_positions` | per-player per-week air distance |
| `weekly_rollup` (job) | own `vehicle_positions` | prunes consumed raw positions |

All history lands in `/var/lib/lastsietch-telemetry/telemetry.db` (SQLite, WAL mode).

## Safety contract

This service is **passive**:

- It issues only read-only `SELECT`s against the `dune.*` schema. It never runs
  `INSERT`/`UPDATE`/`DELETE`/`DDL` there. `gamedb.py` has no write method.
- It touches k8s only with read verbs (`kubectl get`, `kubectl exec ... printenv`,
  `kubectl logs`). It never restarts, deletes, scales, or patches anything.
- It writes only to its own SQLite store at `/var/lib/lastsietch-telemetry/telemetry.db`.
- A failed poll is harmless: it just retries next interval. One stream failing
  never stops the others.

## Running locally

```
pip install -r requirements.txt
export DB_HOST=... DB_PASS=... GAME_NS=... GAME_POD=...
export TELEMETRY_DB=./telemetry.db
python3 logger_service.py --once     # one sweep of every stream/job, then exit
python3 logger_service.py            # run the scheduler loop
```

On <game-host> use `deploy/run-telemetry.sh`, which resolves `DB_*` and `GAME_*`
from k3s automatically.

The read API:

```
uvicorn api.app:app --host 127.0.0.1 --port 8078
```

Endpoints: `/health`, `/presence`, `/world`, `/events`, `/grants`,
`/leaderboard/pvp`, `/leaderboard/deaths`, `/leaderboard/pilots`,
`/positions/live`, `/positions/stream`, `/progression/snapshot`,
`/progression/{account_id}/history`, `/progression/levelups`, `/roster/latest`.

## Deployment

See `RUNBOOK.md` (supervisor-gated). The logger and the read API each ship a
systemd unit under `deploy/`.

## Future hardening (not built)

- An optional `dune.ls_combat_log` mirror table in the game DB could give
  durability beyond the rolling 3-day `game_events` window. It is deferred -
  the logger writes only to `telemetry.db` today.
- `creature_catalog` labelling (boss vs NPC) for `combat_events`. The
  `causer_row_index` is stored raw so this can be added without re-harvesting.
- An `economy_events` stream reading the market-bot's `ls_market_log`.
