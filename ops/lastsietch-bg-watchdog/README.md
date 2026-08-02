# lastsietch-bg-watchdog — BattleGroup auto-recovery

Auto-detects and fixes the **"BattleGroup comes back `spec.stop=true` after a host power reboot"**
gotcha, which kills every game pod with **no self-recovery**.

We hit this twice, both times after a host power event: once during announced provider
maintenance, where it was diagnosed and fixed by hand, and once unannounced, where the server sat
dead for nearly two hours because it happened outside the hours anyone was looking.

The second one is the instructive case. The fix was already written down, but only as a manual step
inside a planned-maintenance runbook, so an unplanned reboot meant nobody ran it. A recovery
procedure that only exists inside a document you read when you were expecting trouble is not a
recovery procedure.

This watchdog turns that outage into two or three minutes, unattended.

## What it does

Every 60s (systemd timer, also fires 90s after boot):

1. Read `battlegroup.status.phase`.
2. `Stopped` for `CONFIRM_TICKS` (default 2) consecutive reads, and not paused
   -> apply the documented fix `spec.stop=false` and post a plain-language Discord alert.
3. When the phase leaves `Stopped` after acting -> post a "RECOVERED" alert.

## Safety (it acts on the live server unattended)

| Guard | Behaviour |
|---|---|
| Precise trigger | Acts ONLY on `status.phase == "Stopped"`. `Reconciling` / `Starting` / `Stopping` / `Healthy` never trigger. |
| Unreadable API | A failed or empty read is **never** treated as Stopped (does nothing). Protects against the API not being up yet at boot. |
| Confirm first | Requires 2 consecutive Stopped reads (~2 min), so a legitimate transient stop->start is not fought. |
| Rate limit | Max `MAX_ATTEMPTS` (default 3) per 6h. Beyond that it **stops acting** and posts a "MANUAL ACTION NEEDED" alert exactly once. It can never flap or fight a human. |
| Pause flag | `pause.sh on <min>` suppresses auto-recovery for INTENTIONAL stops. Self-heals on expiry (6h cap) so it can never stay paused forever. |
| Idempotent action | The patch is the exact proven recovery command; re-applying it is harmless. |

### The pause flag is deliberately SEPARATE from the pod-watcher mute

A separate alert-mute tool (not published here) suppresses crash **alerts**. `ops/lastsietch-bg-watchdog/pause.sh`
suppresses **auto-recovery**. They are independent on purpose:

- **Your own update windows** (you deliberately stop the BattleGroup): arm **BOTH**, else the
  watchdog would un-stop the BG mid-update.
- **Provider maintenance windows** (a datacentre router upgrade with several short reboots, say):
  arm the alert mute if you want less noise, and **leave the watchdog ACTIVE** so any resulting stop
  is auto-recovered. If the two were coupled, silencing noise would disable recovery at exactly the
  moment it is most likely to be needed.

## Usage

```bash
/opt/lastsietch-bg-watchdog/watchdog.sh            # one manual tick (safe; obeys all guards)
/opt/lastsietch-bg-watchdog/pause.sh on 120        # pause auto-recovery (intentional stop)
/opt/lastsietch-bg-watchdog/pause.sh off | status
systemctl status lastsietch-bg-watchdog.timer
journalctl -u lastsietch-bg-watchdog -n 50
```

State: `/var/lib/lastsietch-bg-watchdog/{state.json,pause}`.
Tunables via env: `BGW_CONFIRM_TICKS`, `BGW_MAX_ATTEMPTS`, `BGW_ATTEMPT_WINDOW`.

## Tests

`bash ops/lastsietch-bg-watchdog/test-watchdog.sh` — 35 assertions, fully offline (stubs kubectl + the
notifier, touches nothing live). Covers: healthy no-op, confirm-then-act, recovery alert, paused,
expired-pause self-heal, API-failure safety, rate-limit cap + single alert, window pruning,
patch-failure alerting, every non-Stopped phase, and the pause CLI.

## Manual fix (if you ever need it)

```bash
kubectl -n funcom-seabass-sh-<your-hostid>-<random> \
  patch battlegroup sh-<your-hostid>-<random> --type=merge -p '{"spec":{"stop":false}}'
```
Do NOT patch the director's `suspend` (the BattleGroup reverts it).
Detection and the one-field fix are documented in `docs/14-known-funcom-issues.md`.
