# Last Sietch Dune Telemetry Logger - Deployment Runbook

Supervisor-gated. The implementer team produced the code only; every step below
is run later by the supervisor on the game host. The logger is read-only against the
game DB, but deploy cleanly anyway.

## 1. Pre-flight

- Confirm no Dune maintenance window is active.
- Note the current battlegroup namespace hash (`kubectl get ns | grep funcom-`)
  for sanity.

## 2. Copy code

```
rsync -a dune-telemetry/ <game-host>:/opt/lastsietch-telemetry/
```

## 3. venv

```
python3 -m venv /opt/lastsietch-telemetry/venv
/opt/lastsietch-telemetry/venv/bin/pip install -r /opt/lastsietch-telemetry/requirements.txt
```

## 4. Config

```
cp /opt/lastsietch-telemetry/deploy/telemetry.env.example /opt/lastsietch-telemetry/telemetry.env
```

Review the intervals. Leave `DB_*` and `GAME_*` blank - the launcher fills them.

## 5. Verify discovery

```
KUBECONFIG=/etc/rancher/k3s/k3s.yaml /opt/lastsietch-telemetry/venv/bin/python3 \
  /opt/lastsietch-telemetry/gamedb.py --discover
```

Confirm it prints exactly one `funcom-*` namespace, the `db-dbdepl-sts-0` pod,
the `db-dbdepl-svc` service, and a `GAME_POD`. If `GAME_POD` is empty or wrong,
inspect `kubectl get pods -n <ns>` and pin the survival-pod grep in
`gamedb.discover_gamedb()` (the `game`/`survival` match is best-effort).

## 6. Dry sweep

```
/opt/lastsietch-telemetry/deploy/run-telemetry.sh --once
```

Confirm `telemetry.db` is created at `/var/lib/lastsietch-telemetry/` with WAL files
present, and that rows landed in `presence`, `world_snapshots`,
`combat_events`, `vehicle_positions`, `grant_events`, and `player_progression`.
Confirm zero errors in the journal.

Expected on the first sweep: `player_progression` shows one row per active
character; `player_progression_levelups` is EMPTY. Level-up emission requires a
prior baseline - the first sweep establishes that baseline. Subsequent sweeps
that catch a level crossing will populate `player_progression_levelups`.

## 7. Parallel-run gate (Phase 1)

With the old `lastsietch-stats-sampler.timer` still active, let the logger run a few
sweeps and diff `telemetry.db` `presence`/`world_snapshots`/`connections`
against `/var/lib/lastsietch-stats/stats.db` (`snapshots` table). They should agree
within one sweep.

## 8. Install units

```
cp /opt/lastsietch-telemetry/deploy/lastsietch-telemetry.service \
   /opt/lastsietch-telemetry/deploy/lastsietch-telemetry-api.service /etc/systemd/system/
cp /opt/lastsietch-telemetry/deploy/run-telemetry.sh /opt/lastsietch-telemetry/
chmod +x /opt/lastsietch-telemetry/run-telemetry.sh
systemctl daemon-reload
systemctl enable --now lastsietch-telemetry lastsietch-telemetry-api
```

## 9. Verify API

```
curl -s http://127.0.0.1:8078/health
curl -s 'http://127.0.0.1:8078/presence?window=24h'
```

## 10. Retire the old sampler

ONLY after the parallel-run gate (step 7) passes:

```
systemctl disable --now lastsietch-stats-sampler.timer
```

Keep `stats.db` for history; the digest still reads it until the Phase 4
consumer refactor.

## 11. Tune the teleport threshold

After ~a week of `vehicle_positions` data, inspect the per-segment speed
distribution and set `TELEPORT_MAX_SPEED_CMS` in `telemetry.env` to a real
value (the default 400000 cm/s is a deliberately loose first guess), then
`systemctl restart lastsietch-telemetry`.

## Rollback

```
systemctl disable --now lastsietch-telemetry lastsietch-telemetry-api
systemctl enable --now lastsietch-stats-sampler.timer
```

The logger never wrote to the game DB - there is nothing else to undo.
