# dune-update-auto

Unattended handling of a Funcom update window.

Funcom ships updates on their schedule, not yours, and the moment the client
update lands, players cannot connect to a server still running the old build.
If that happens at 3am your time, the choice is being awake for it or having
something reliable act on your behalf.

## What it does

`dune-update-orchestrator.sh` is a single flock-guarded run:

1. **Warns players** on a schedule anchored to a target time: chat and in-game
   broadcasts at T-30m, T-15m, and T-60s.
2. **Waits for the real signal.** It polls the Steam depot for the self-host
   product and waits for the public buildid to move off the installed one.
   That flip is the only trustworthy go-signal; an announcement is not.
3. **Applies the update.** Deliberately proceeds even with players online: once
   the client update ships, an old-build server is unreachable anyway, so
   waiting for an empty server means staying down longer. The rolling restart
   is graceful and flushes progression on shutdown.
4. **Runs pre-flight gates**, then updates, then verifies the director is
   healthy and the server is announcing itself again. One automatic recovery
   attempt, and no second unattended restart if that fails.
5. **Reports.** Success posts an all-clear. A failure after recovery stops and
   escalates with the rollback command rather than continuing to improvise.
6. **Gives up cleanly.** If no build appears by a deadline, it notifies and
   leaves the server untouched.

`dune-hotfix-watch.sh` is the lighter version: it watches the depot and tells
you when the buildid moves, without acting.

`verify-post-coriolis.sh` is a read-only check that staged configuration
changes actually took effect after a restart.

## Before you use this

**This is the shape of the thing, not a turnkey service.** It was written for
one deployment and it shows in places:

- Notifications go through our Discord bot, which is not published yet. Every
  notification path is a shell-out, so substituting your own is the main port.
- It backs up, drops, and restores a custom schema of ours around the update,
  because leaving it in place aborts Funcom's own migration replay. If you have
  your own schema alongside Funcom's, you likely need the same dance; if you do
  not, disable that step.
- Host names, paths, and timings come from environment variables with
  placeholder defaults. Nothing works until you set them.

Read it before you arm it. It restarts your game server without asking.

## Safety properties worth keeping if you rewrite this

- **One lock.** Never two runs at once.
- **A real go-signal, not a timer.** Poll for the condition; do not assume the
  update landed because the clock says so.
- **A verified backup before anything destructive.** The schema drop here
  refuses to run without a dump it has checked.
- **One recovery attempt, then stop.** Unattended automation that keeps
  retrying a failing restart turns a bad window into a worse one.
- **A deadline.** If the expected thing never happens, do nothing and say so.
- **A disarm that does not require you to be awake or clever:** disable the
  timer, drop a marker file, or flip one environment variable.

## Configuration

All via environment in the service unit. The names are readable in the script
header. The ones you will certainly set: target time, the currently installed
buildid, game host, database pod, and whichever notification toggles you want
off (`..._BCAST_ENABLE`, `..._DM_ENABLE`, and so on all default on).

## Status, disarm, re-arm

```bash
systemctl --user list-timers dune-update.timer --all   # when it next fires
cat ~/dune-update-auto/state.json                      # what phase it is in
tail -f ~/dune-update-auto/logs/run-*.log              # live, during a window

systemctl --user disable --now dune-update.timer       # stop it
touch ~/dune-update-auto/done.marker                   # make this run a no-op
```

Re-time by editing `OnCalendar` in the timer unit, then
`systemctl --user daemon-reload`.

A `test-notify` subcommand exercises the notification path only and fires
nothing else. Use it before trusting the rest.
