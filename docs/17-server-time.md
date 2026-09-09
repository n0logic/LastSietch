# Read-only server clock and recurrence display

scripts/server-schedule.py produces a minimal public clock reading. It reads the
system clock and explicit operator configuration only. It opens no network port,
queries no game database, and never schedules or triggers maintenance.

```sh
python3 scripts/server-schedule.py --time-zone UTC
```

Without an anchor, the clock remains available but the schedule is unavailable.
To display a verified recurring schedule, supply an offset-qualified anchor and
period. This is a synthetic example, not a server's actual reset schedule:

```sh
python3 scripts/server-schedule.py --time-zone Europe/London \
  --cycle-anchor 2030-01-01T00:00:00Z --cycle-days 14
```

The anchor defines a recurrence in UTC, including preceding cycles. The display
timezone changes presentation, not the recurrence. A schedule based on local
civil time may need a different provider around daylight-saving transitions.
Validate the current native schedule before presenting any recurrence to players.

The JSON contains available, server_now_utc, time_zone and a coriolis object.
The latter is either unavailable or contains the calculated cycle boundaries.
If exposing this through an existing web application, serve a read-only GET
with Cache-Control: no-store. Do not include deployment configuration, login
passwords, tokens, player records or raw Director responses.

web/server-time.mjs provides formatting and parsing helpers with no framework
dependency. Use the server reading as an anchor, advance it with monotonic
elapsed time, and periodically resynchronize. Do not substitute the browser's
wall clock as server authority. Reject invalid readings and display unavailable
states instead of inventing a schedule.

The countdown says Scheduled time reached when a boundary passes. That does
not claim that a reset completed. Actual completion must be verified separately.

Python requires zoneinfo support and timezone data. JavaScript tests require
Node.js with IANA timezone support:

```sh
python3 scripts/tests/test_server_schedule.py
```
