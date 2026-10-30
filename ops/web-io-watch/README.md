# <web-host> disk-stall watch

Deployed 2026-09-28 after that morning's <web-host> disk stall (10:43-11:08 ET): the portal hung, bot buy
prices went stale and hid, cron skipped runs, and the only signal was a player report. The provider panel
shows CPU and memory, and neither moved; the kernel's IO pressure gauge was at ~93% "full".

- `io-watch.sh` -> `/opt/lastsietch/io-watch.sh`; `io-watch.service` + `io-watch.timer` -> `/etc/systemd/system/`,
  timer enabled, runs every 60 s.
- A bad minute is `/proc/pressure/io` full avg60 >= 30% OR the local lastsietch-admin check
  (`http://127.0.0.1:8078/api/dune/rules`) not answering 200 within 10 s. Three bad minutes in a row -> one
  alert naming the trigger ("disk stall" / "portal not answering"); the first good minute after that -> one
  recovery note with the duration. Posts via `karum-notify-local.sh` (#server-logs + owner DM).
- State in `/var/lib/lastsietch-io-watch/` (`streak`, `alerted`). Thresholds via env: FULL_PCT, PORTAL_SLOW_S, BAD_MIN.
- Limit: it runs on the box it watches, so during a hard stall the alert lands when the stall eases or
  persists, not at its first second.
- Tested before deploy with a stub notifier: forced disk-bad (FULL_PCT=0) and a dead portal port both alert
  on the 3rd minute only, once; recovery posts once; quiet afterwards.
