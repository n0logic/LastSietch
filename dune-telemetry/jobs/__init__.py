"""
Job registry for the telemetry logger.

Jobs share the same module-level contract as streams - a `JOB` dict with
{"name", "interval_attr", "run"} - and run on the same scheduler. They process
data already in telemetry.db (no game-DB reads in flight_distance/weekly_rollup).
"""
from jobs import accounts_sweep, flight_distance, weekly_rollup

JOBS = [
    flight_distance.JOB,
    weekly_rollup.JOB,
    accounts_sweep.JOB,
]
