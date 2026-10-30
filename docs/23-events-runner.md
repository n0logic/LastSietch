# 23 - The weekend events runner

We run a weekend event most weekends: double harvest, double XP and similar. Starting and ending one by hand means
someone awake at the right time, typing the right command into every pod, and remembering to put it back. The
events runner does that from the portal's Events Manager: an admin publishes an event with an effect and a window,
and the runner starts it, keeps it applied, ends it, and tells players.

The code ships under `ops/weekend-events/` and `admin-backend/`. It sits on the control plane from
[15](15-control-plane-architecture.md): the web tier decides, the game host acts, and the only path between them is
the relay key and the forced-command dispatcher.

## How it fits

```text
Events Manager (admin panel, web host)   publish = the go; the event row is "armed"
  -> runner timer (web host)            every minute: start, end or poll
  -> relay key -> dispatcher            event-effect-start | end | status, numeric and enum arguments only
  -> effect worker (game host)          runs the change as a transient systemd unit, writes a status file
  -> assert timer (game host)           every 5 minutes: re-apply open live windows; end them if the web host goes quiet
```

The dispatcher accepts only an event id, a phase and an effect name from a fixed list. The web tier cannot name a
cvar or a value.

## Effects

| Kind | Example | How it starts and ends | While it runs |
|---|---|---|---|
| Live | Harvest weekend: `Dune.GlobalMiningOutputMultiplier` and `Dune.GlobalVehicleMiningOutputMultiplier` to 2.0, back to 1.0 | Sent to every target server at runtime, then read back on every target pod | Re-asserted every 5 minutes, so a pod that restarts mid-weekend picks it up again |
| Restart | Double XP: `GlobalXpMultiplier=2.0` in the settings file | A planned fleet restart at the start and at the end | Nothing to do: the file holds it |

Scope is the whole fleet or one map (our PvP sietch). Restart effects are fleet-wide by nature (the settings files
are shared), and the form, the store, the dispatcher and the worker all refuse them on one map.

Restart effects ship **dark**: they refuse at the first line until a flag file exists on the game host, and nothing
creates that file but the owner, after a no-op rehearsal has run clean in a planned window. The rehearsal runs the
whole path with the value unchanged: notices, stop, settings gate, start, verify, readback.

## States

The store sets `armed` when an event is published and clears it on unpublish or cancel. Only the runner moves it on,
with compare-and-set:

| From | To | When |
|---|---|---|
| `armed` | `running` | The worker read the new value back on **every** target pod. Zero pods is a failure, not a pass. |
| `armed` | `missed` | The window closed before a start ran, or a restart effect would start more than an hour late. A live effect that starts late catches up and records how late. |
| `running` | `done` | The end read the baseline back on every pod. Unpublishing or cancelling a running event ends it at once. |
| any | `failed` | Refused on the game host, no result in time, a lost job, or a short readback. A short readback puts the values back to baseline first. |

Two guards worth copying:

- **Precheck before a live start.** Every target pod must read each value at its value of record, or already at the
  effect's value. Anything else is tuning someone set on purpose, and the start refuses rather than let the end
  overwrite it with the baseline.
- **Dead-man end.** If the web host never sends the end, the game host ends a live window itself 30 minutes after
  its scheduled end and posts that it did.

An SSH failure leaves the event as it was and retries on the next tick, inside the same time limits. "Re-arm" in the
Events Manager puts a failed or missed event back to `armed`.

## Start notices

When a live effect goes `running`, the runner sends one in-game broadcast through the dispatcher's existing
broadcast action and one Discord post through the bot, with no mentions.

- **Text** comes from the event: its title if it is 80 characters or fewer, and the first line of its description if
  it is 280 or fewer (else its first sentence). An empty or too-long field, or one carrying a mass mention, falls back
  to the effect's fixed copy.
- **Exactly once per start.** Only the tick whose compare-and-set moved the row to `running` sends, and it first
  claims a notice file with exclusive create. A re-armed event, a restarted timer or a second runner finds the claim
  and sends nothing. The claim is taken before sending, so a crash mid-send loses a notice rather than repeating it:
  a duplicate public post is worse than a gap the ops alert makes visible.
- **A failed notice never touches the effect.** The row is already `running`. The failure is logged and posted to
  the ops channel, and not retried.

Restart effects keep their own ten-minute and one-minute in-game warnings from the restart path.

## Kill switches

Files under `/etc/lastsietch/`, the house pattern:

| File | Host | Without it |
|---|---|---|
| `event-effects-enabled` | web host | The runner does nothing. Published events go `missed` once their limits pass. |
| `event-effects-enabled` | game host | Launches refuse (the event goes `failed`); the assert timer does nothing. |
| `event-effects-restart-enabled` | game host | Restart effects refuse before any notice or stop. |
| `event-effects-notices-enabled` | web host | Live starts send no broadcast and no Discord post. The effect still runs. |

## Deploying it

Deploy the writer before the caller: the admin store and routes first, then the worker, dispatcher entries and
assert timer on the game host, then the runner on the web host.

The game-host deploy has a **dispatcher drift guard**: the live dispatcher must match the version the change was
built on, or the deploy refuses. The dispatcher is the security boundary, and an edit made on the box and not in
the repository would otherwise be overwritten without anyone noticing.

Watch the timers by `LastTriggerUSec` advancing, not by `systemctl is-active`. A timer can read `active` and never
fire: one built on `OnBootSec` plus `OnUnitActiveSec` has no anchor after it is stopped and started again without a
reboot, which is what a maintenance hold does. `NextElapseUSecMonotonic=infinity` is the tell. An `OnActiveSec`
drop-in anchors the first firing to the timer's own activation.

## Failure we hit

On the first live weekend the runner marked a start `failed` that had in fact run. The runner read the worker's
first line of output as the reply; the reply is the last line. Parse a reply from a defined place, and test the
runner against a worker that logs before it answers.

Two more from running weekends by hand before the runner existed:

- **Transient timers do not survive a reboot.** `systemd-run --on-calendar` timers vanish when the host restarts.
  Re-arm them, or use the runner.
- **Never put `\;` in a `systemd-run ... sh -c '...'` string.** Inside the script it is a literal `;` argument, not a
  separator, and the first command fails with a usage error. Use `&&`, or one timer per command.

## Related

- [15-control-plane-architecture.md](15-control-plane-architecture.md): the relay and dispatcher this rides on
- [21-custom-settings-and-rates.md](21-custom-settings-and-rates.md): the values effects move away from and back to
- [20-player-portal.md](20-player-portal.md): the Events page and the weekend card players see
