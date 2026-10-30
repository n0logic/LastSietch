# Weekend events: the effect runner (Stage 4) and the manual runner it replaces

The Events Manager arms an effect on a published event; this runner starts and ends it on the game host.

## How it fits

```
Events Manager (lastsietch-admin, <web-host>)      publish = the go; armed in admin.db (portal_events.effect_*)
  -> lastsietch-event-effects.timer (<web-host>)   every minute: event-effects.py decides start / end / poll
  -> relay key -> dune-relay-dispatch.sh    event-effect-start|end|status, numeric + enum args only
  -> event-effect-worker.py (<game-host>)      launch = transient unit lastsietch-event-<id>-<phase>; status file
  -> lastsietch-event-effect-assert.timer          every 5 min: re-assert open live windows; dead-man end
```

| Effect | What changes | Start / end | While running |
|---|---|---|---|
| `spec_xp` (live) | `dw.LandsraadMissionRewardMultiplierSpecializationXP` 2.0, back to 1.0 | ServerExec, read back on every target pod | re-asserted every 5 min |
| `harvest` (live) | `Dune.GlobalMiningOutputMultiplier` + `Dune.GlobalVehicleMiningOutputMultiplier` 2.0, back to 1.0 | same | same |
| `xp` (restart, DARK) | `GlobalXpMultiplier` 2.0 in the settings files | clean fleet stop | n/a |
| `crafting` (restart, DARK) | `CraftingCost` 0.8 in the settings files | clean fleet stop | n/a |

Scope is `fleet` or `amtal` (partition 33 only: ServerExec routed to Amtal's server_id, read back on pod-33 only). XP
and crafting are fleet-wide by nature (shared settings files), and the form, the store, the dispatcher and the worker
all refuse them on one map.

## States (admin.db `portal_events.effect_state`)

The store sets `armed` on publish and clears it on unpublish or cancel. Only the runner moves it on, compare-and-set,
never touching `updated_at` (the Discord sync revision):

- Before a live start, every target pod must read each cvar at its value of record or already at the effect's value
  (a hand run of the same weekend). Anything else is tuning someone set on purpose: the start refuses (`failed`,
  "precheck") rather than let the end overwrite it.
- `armed` -> `running`: the worker read the value back on **every** target pod. Zero pods is a failure. A short
  readback puts the cvars back to baseline and the effect is `failed` with the count and the missing pods.
- `armed` -> `missed`: the window closed before a start ran, or a restart effect would start more than 60 min late.
  A live effect that starts late catches up, and the lateness is written to `effect_error` and posted to bot-logs.
- `running` -> `done`: the end read back the baseline on every pod. Unpublishing or cancelling a running event ends it
  at once (its effect, map and window are frozen while it runs or starts).
- anything -> `failed`: refused on <game-host>, no result within 15 min (live) or 90 min (restart), a lost job, or a
  restart effect's end more than 60 min late (the settings still hold the effect; end it in a planned window).
  "Re-arm effect" in the Events Manager puts a failed or missed event back to `armed`.

An ssh failure leaves the event as it was and retries on the next tick, inside the same limits. If <web-host> never
sends the end, <game-host> ends a live window itself 30 min after its end (dead-man) and posts it.

## Kill switches (house pattern: files under /etc/lastsietch)

| File | Host | Without it |
|---|---|---|
| `/etc/lastsietch/event-effects-enabled` | <web-host> | the runner does nothing (published events go `missed` once their limits pass) |
| `/etc/lastsietch/event-effects-enabled` | <game-host> | `launch` refuses (the event goes `failed`), the assert timer does nothing |
| `/etc/lastsietch/event-effects-restart-enabled` | <game-host> | xp and crafting refuse before any notice or stop (see below) |
| `/etc/lastsietch/event-effects-notices-enabled` | <web-host> | live starts send no in-game broadcast and no Discord post (the effect still runs) |

## Start notices (admin plan G3 minimum)

When a live effect (spec_xp, harvest) goes `running`, the runner sends one in-game broadcast (title + one line, 60 s)
through the relay key's existing `broadcast-send` dispatcher action, and one post to #announcements through the signed
notice sender (`make-notice.py` beside the runner, then `/opt/lastsietch-notices/releases/<rel>/send.py`, no
mentions). Restart effects keep their own T-10 / T-1 broadcasts on <game-host> and get no start notice here.

- **Text:** the event's title (if 80 chars or fewer) and the first line of its description (if 280 chars or fewer,
  else its first sentence). A field that is empty, too long or carries `@everyone`, `@here` or `<@` falls back to the
  effect's fixed copy ("Double Landsraad weekend" / "Harvest weekend", "... is live now.").
- **Exactly once per event start:** only the tick whose compare-and-set moved the row to `running` sends, and it first
  claims `/var/lib/lastsietch-event-effects/<id>.<start YYYYMMDDTHHMM>.notice` on <web-host> with `O_EXCL`. A re-armed event
  with the same start, a timer restart or a second runner finds the claim and sends nothing. The claim is taken before
  sending, so a crash mid-send loses a notice rather than repeating it (a duplicate public post is worse than a gap the
  alert makes visible). The file holds `in-game ok|failed ...` and `discord ok|failed ...` afterwards.
- **Failures never touch the effect:** the row is already `running` when notices go out. A failed or raising send is
  written to the claim file, logged and posted to bot-logs ("start notice ... FAILED, the effect is running"). It is
  not retried: send it by hand (`post.sh`) if it matters.
- **Why a file, not a column:** a new `portal_events` column needs the admin deploy and a lastsietch-admin restart; the claim
  file is runner-local state on the host that sends. Moving it to admin.db is a G3-full item (admin "send now").
- With notices on, do not also arm manual `post.sh` timers for the same weekend.

## Deploy (announce in the ops channel first; writer before caller)

1. `./ops/deploy-event-effects.sh admin` then `--apply`: <web-host> `/opt/lastsietch-admin` gets `event_effects.py` (new),
   `database.py`, `routers/portal_events.py`, `routers/v2_events.py`, `templates/v2/events.html`,
   `static/js/v2/events.js`; import-checked under the running environment, then lastsietch-admin restarts (backups
   restored if it does not come up). Expect 8 `effect*` columns and rows 1 and 2 labelled `spec_xp`.
2. `./ops/deploy-event-effects.sh <game-host>` then `--apply`: `/opt/lastsietch-event-effects/event-effect-worker.py`, the
   hotfix-tools copies it calls, `lastsietch-event-effect-assert.{service,timer}`, and `/root/dune-relay-dispatch.sh` (drift
   guard: the live dispatcher must be this branch's base or this build). Verified through the relay key:
   `event-effect-status 0 start` answers `none` and a start answers `refused` while disabled.
   For the restart effects also `./ops/deploy-config-watch.sh` then `--apply` (<game-host> `/root/dune-config-hash.py`
   reads `/etc/lastsietch/event-effects/config-expected.json`); not needed for spec_xp and harvest, which change no file.
3. `./ops/deploy-event-effects.sh runner` then `--apply`: `/opt/lastsietch-event-effects/event-effects.py`,
   `make-notice.py` beside it, and `lastsietch-event-effects.{service,timer}` on <web-host> (it refuses if the signed
   notice sender named in `NOTICE_SEND` is not there). Alert channel:
   `/opt/lastsietch-event-effects/.alert.env` (mode 600) with `DISCORD_BOT_TOKEN=` and `DISCORD_CH_BOT_LOGS=`; without it
   alerts go to `/var/log/lastsietch-event-effects.log` only. Configured on <web-host> since 2026-10-30: mode 600, owner
   root, Cielago's bot token, `DISCORD_CH_BOT_LOGS=<channel-id>` (`⚙️ server-logs`, ADMIN category).
   Do not use Cielago's `CIELAGO_SERVICE_ALERTS_CHANNEL_ID` for this: it is the PUBLIC #server-info. 
4. Go live (owner): `touch /etc/lastsietch/event-effects-enabled` on <game-host>, then on <web-host>. Watch the timers by
   `LastTriggerUSec` advancing, never by `is-active`.
5. Start notices (owner, separately): `touch /etc/lastsietch/event-effects-notices-enabled` on <web-host>, only for a
   weekend whose manual broadcast and Discord timers are not armed. Proof at the next live start: one banner in game,
   one #announcements post, and the claim file reading `in-game ok sent` / `discord ok sent`.

## Live proofs before relying on it (owner-gated, in this order)

1. **Spec XP through the runner.** Effect windows are fixed lengths, so there is no short test event: use the real
   10-30 weekend (row 2, backfilled `armed`). With both switches on, the runner starts it at Fri 04:00Z, or catches
   it up late with the lateness recorded if it is switched on after that. Read: `effect_state=running`, the worker log line `readback N/N`, and
   `/var/lib/lastsietch-event-effects/2.start` on <game-host>. Stage observed: effective runtime value on every pod.
2. **A pod restart mid-window.** The next assert tick (5 min) re-applies it: `/var/lib/lastsietch-event-effects/2.assert`
   reads `ok ... readback N/N` after the pod is back.
3. **The end.** Mon 00:00 ET: `done`, baseline `1.0` read back on every pod.
4. **Harvest** on the 4th weekend the same way (or an owner-approved Amtal-only test window).

## Restart effects (xp, crafting): DARK until the L19 rehearsal

Built, deployed with the worker, and refused at the first line of `run_restart` until
`/etc/lastsietch/event-effects-restart-enabled` exists on <game-host>. Nothing creates that file but the owner, and only after
the L19 no-op rehearsal below has run clean in a planned window (admin plan section 5, L19). Without it a published
xp or crafting event fails at T-10 with "restart effects are DARK" and nothing is announced or stopped.

The path is the existing settings-window shape, not a new mechanism: it calls operator-supplied stop/start/verify
tooling (`stop.sh`, `start.sh`, `verify.sh`, `holadmin-drop.sh`, `holadmin-restore.sh`) from `LASTSIETCH_EFFECTS_TOOLS`
(default `/opt/lastsietch-event-effects/hotfix-tools/`), and the shared lease (`game-maintenance.py claim --kind operator
--no-preempt`, never stolen). The runner launches it 10 min before the start (or the end):

1. Precheck: the user file holds exactly one `KEY=<from>`; the lease is free (else `failed`, nothing said in game).
2. In game: "<Event> starts|ends in 10 min" (T-10), then "Restart in 1 minute" (T-1).
3. `holadmin-drop.sh` (refuses to drop without a verified backup; on refusal: "Restart cancelled", `failed`).
4. `stop.sh`: mutes 90 min, `spec.stop=true`, 0 pods, 0 processes, generated file quiet. Not drained: `start.sh` with
   the settings untouched, holadmin restored, `failed`.
5. Gap: back up both settings files, change the one key in the user file (and in the generated file only if it carries
   it), gate on exactly one changed line, the new value, the same line count. Gate failed: both files restored, fleet
   started unchanged, `failed`.
6. `start.sh <current tag> 32` to REGISTERED, `verify.sh`, `holadmin-restore.sh`.
7. Read the key back from both files after boot. `running` (start) or `done` (end) on success; the config watch is
   told the effect's value through `/etc/lastsietch/event-effects/config-expected.json` so the weekend is not drift.
8. Always: lease released, mutes off, live windows re-asserted.

Stages observed: the input written and still present after boot, and the fleet registered. The runtime XP or
crafting multiplier has no read path; the first kill or craft by a player is the stage-4 check.

### L19: the no-op rehearsal (owner-gated, planned window, before 11-06)

Needs an announced window and the owner's (or Medic's) text go at the time, per AGENTS.md section 1.

1. Deploy the <game-host> part (above). Read-only precheck: run your read-only precheck on <game-host>
   (lease free, no table not owned by dune, record the four settings SHAs).
2. Discord notice as for any short restart (make-notice.py, `#server-info`), no pings.
3. Run it as a transient unit so an SSH drop cannot kill it:
   `ssh <game-host> "sudo systemd-run --unit lastsietch-event-rehearsal --collect /usr/bin/python3
   /opt/lastsietch-event-effects/event-effect-worker.py rehearse --owner-go '<the owner's go, quoted>'"`, then
   `ssh <game-host> 'sudo journalctl -fu lastsietch-event-rehearsal'`.
4. Pass = `REHEARSAL OK: GlobalXpMultiplier=1.000000 in both files after boot, fleet registered`, AND, read by hand:
   the four settings SHAs unchanged against step 1, `verify.sh` 32/32 on the tag with 0 restarts, `HOLADMIN
   RESTORED`, lease `free`, mutes off, and a player (owner) logs in and plays. Record the timings (T-10 to REGISTERED).
5. Anything else is a fail: the gate stays off. Only after a pass, and on the owner's word:
   `ssh <game-host> 'sudo touch /etc/lastsietch/event-effects-restart-enabled'`.

Deadline: the first restart weekend is XP, Fri 2026-11-06 00:00 ET (05:00Z); its start job launches Thu 11-05 23:50 ET.
If L19 has not passed by 11-04 (owner decision D2): publish that weekend with effect "None" and run the window by hand,
so it does not fail loudly at 23:50.

## Logs and state

<web-host> `/var/log/lastsietch-event-effects.log`, start-notice claims `/var/lib/lastsietch-event-effects/<id>.<start>.notice`; <game-host> `/var/log/lastsietch-event-effects.log`, job results
`/var/lib/lastsietch-event-effects/<id>.<start|end|assert>`, open live windows `/etc/lastsietch/event-effects/<id>`
("effect scope end_epoch"). `journalctl -u 'lastsietch-event-*'` for the transient units.

2026-10-30 first live start of the runner was marked failed because its `ssh()` read the worker's first stdout line
instead of the reply on the worker's last line. Fixed in #345.

2026-10-24 to 10-26 run: portal event 1, start Sat 12:00 ET, broadcasts Sat 20:00 / Sun 12:00 / Sun 20:00 ET,
Discord Sat 12:00 (#announcements) and Sun 12:00 (#server-info), end Mon 00:00 ET, teaser Thu 10-29 12:00 ET.
