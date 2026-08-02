"""
Config for the Last Sietch Dune telemetry logger.

A frozen Config dataclass populated from environment variables, loaded once at
startup. Cadences are env-tunable so the supervisor can throttle the service
without a rebuild. DB_* values are normally filled by the launcher
(deploy/run-telemetry.sh), which resolves the game-DB ClusterIP fresh each start.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# Vehicle position snapshot filter. The tighter /FlyingVehicles/ class path is
# required: /Vehicles/ also matches BP_VehiclesFabricator_C (a building) and
# would pollute the data. /GroundVehicles/ is reserved for future telemetry.
VEHICLE_CLASS_FILTER = "%/FlyingVehicles/%"

# Per-segment speed ceiling for the flight-distance teleport filter, in
# Unreal cm/s. A deliberately loose first guess (~4000 m/s) until the
# supervisor tunes it against real vehicle_positions data (see RUNBOOK).
TELEPORT_MAX_SPEED_CMS = 4_000_00


def _int(name, default):
    return int(os.environ.get(name, str(default)))


@dataclass(frozen=True)
class Config:
    telemetry_db: str
    db_host: str
    db_port: int
    db_user: str
    db_pass: str
    db_name: str
    game_ns: str
    game_pod: str
    presence_interval: int
    login_days_interval: int
    world_interval: int
    connections_interval: int
    roster_interval: int
    combat_interval: int
    vehicle_interval: int
    positions_interval: int
    flight_job_interval: int
    rollup_interval: int
    combat_cursor_overlap: int
    geoip_api: str
    log_level: str
    api_bind: str
    api_port: int
    teleport_max_speed_cms: float
    grant_events_interval: int
    progression_interval: int
    accounts_sweep_interval: int
    accounts_sweep_min_live: int
    transfers_interval: int
    read_models_interval: int
    read_model_script_dir: str
    storage_interval: int
    market_interval: int
    world_events_interval: int


def load_config():
    return Config(
        telemetry_db=os.environ.get("TELEMETRY_DB", "/var/lib/lastsietch-telemetry/telemetry.db"),
        db_host=os.environ.get("DB_HOST", ""),
        db_port=_int("DB_PORT", 15432),
        db_user=os.environ.get("DB_USER", "postgres"),
        db_pass=os.environ.get("DB_PASS", ""),
        db_name=os.environ.get("DB_NAME", "dune"),
        game_ns=os.environ.get("GAME_NS", ""),
        game_pod=os.environ.get("GAME_POD", ""),
        presence_interval=_int("PRESENCE_INTERVAL", 300),
        # Login-days recorder shares the presence source + cadence (300s).
        login_days_interval=_int("LOGIN_DAYS_INTERVAL", 300),
        world_interval=_int("WORLD_INTERVAL", 300),
        connections_interval=_int("CONNECTIONS_INTERVAL", 300),
        roster_interval=_int("ROSTER_INTERVAL", 300),
        combat_interval=_int("COMBAT_INTERVAL", 90),
        vehicle_interval=_int("VEHICLE_INTERVAL", 90),
        positions_interval=_int("POSITIONS_INTERVAL", 5),
        flight_job_interval=_int("FLIGHT_JOB_INTERVAL", 900),
        rollup_interval=_int("ROLLUP_INTERVAL", 86400),
        combat_cursor_overlap=_int("COMBAT_CURSOR_OVERLAP", 60),
        geoip_api=os.environ.get("GEOIP_API", "http://ip-api.com/json/"),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        api_bind=os.environ.get("API_BIND", "127.0.0.1"),
        api_port=_int("API_PORT", 8078),
        teleport_max_speed_cms=float(
            os.environ.get("TELEPORT_MAX_SPEED_CMS", str(TELEPORT_MAX_SPEED_CMS))),
        grant_events_interval=_int("GRANT_EVENTS_INTERVAL", 60),
        progression_interval=_int("PROGRESSION_INTERVAL", 120),
        accounts_sweep_interval=_int("ACCOUNTS_SWEEP_INTERVAL", 3600),
        accounts_sweep_min_live=_int("ACCOUNTS_SWEEP_MIN_LIVE", 5),
        transfers_interval=_int("TRANSFERS_INTERVAL", 60),
        # Read-model mirror: full-refresh of all accounts' portal/admin read
        # models. 60s default (per-account builders run several sub-queries each;
        # the single-threaded scheduler must not starve the 5s positions stream,
        # so keep this gentle and tune against the measured run duration).
        read_models_interval=_int("READ_MODELS_INTERVAL", 60),
        read_model_script_dir=os.environ.get("READ_MODEL_SCRIPT_DIR", "/root"),
        storage_interval=_int("STORAGE_INTERVAL", 90),
        market_interval=_int("MARKET_INTERVAL", 60),
        # Sandstorm/worm-breach pod-log tail. Each run tails ~200k lines/pod
        # (several hours of logs); a 2h cadence keeps consecutive tails well
        # overlapped so the UNIQUE dedup loses no events between runs.
        world_events_interval=_int("WORLD_EVENTS_INTERVAL", 7200),
    )
