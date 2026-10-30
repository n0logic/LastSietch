#!/usr/bin/env python3
"""Weekend effect worker on <game-host> (WEEKEND-EVENTS-PLAN Stage 4, admin plan G2).

Called by the forced-command dispatcher (scripts/dune-relay-dispatch.sh tokens
event-effect-start / -end / -status) on behalf of the <web-host> runner
(event-effects.py), and by lastsietch-event-effect-assert.timer every 5 minutes.

  launch start <id> <effect> <scope> <end_epoch>   record the job, run it as a transient unit
  launch end <id> <effect> <scope>                  (systemd-run --unit lastsietch-event-<id>-<phase>,
                                                    so an SSH drop cannot kill a long job)
  status <id> <start|end|assert>                   one line: none | running | ok ... | failed ...
  run <start|end> <id> <effect> <scope> [end]      the job itself (what the unit execs)
  assert                                           re-assert every open live window; end an
                                                   expired one <web-host> never ended
  rehearse --owner-go <text>                       L19: the full restart path with a no-op edit

Live effects (spec_xp, harvest) use the proven ServerExec route: set the
cvar with one ServerExec, read it back, and count a pod only when its own log
echoes the wanted value. Running needs EVERY target pod; zero pods is a failure,
never a vacuous pass. A start that does not read back on every pod is put back
to baseline and reported failed.

Restart effects (xp, crafting) are DARK: they refuse unless
/etc/lastsietch/event-effects-restart-enabled exists, and that file is created only
after the owner has watched the L19 no-op rehearsal (`rehearse`) succeed in a
planned window. The path calls operator-supplied stop/start/verify tooling in
LASTSIETCH_EFFECTS_TOOLS (stop.sh, start.sh, verify.sh, holadmin-drop.sh,
holadmin-restore.sh) and the shared maintenance lease; nothing here is a new
restart mechanism.

Kill switch: without /etc/lastsietch/event-effects-enabled, launch refuses and assert
does nothing.
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

TEST_ROOT = os.environ.get("LASTSIETCH_EFFECTS_TEST_ROOT", "")
SELF = os.path.abspath(__file__)
STATE_DIR = TEST_ROOT + "/var/lib/lastsietch-event-effects"
WINDOW_DIR = TEST_ROOT + "/etc/lastsietch/event-effects"
ENABLED = TEST_ROOT + "/etc/lastsietch/event-effects-enabled"
RESTART_ENABLED = TEST_ROOT + "/etc/lastsietch/event-effects-restart-enabled"
LOG = TEST_ROOT + "/var/log/lastsietch-event-effects.log"
ALERT_ENV = TEST_ROOT + "/opt/lastsietch-pod-watcher/.discord.env"
# Read by the hourly config watch (scripts/dune-config-hash.py): while a restart
# effect holds a settings key, that key's expected value is the effect's.
CONFIG_EXPECTED = TEST_ROOT + "/etc/lastsietch/event-effects/config-expected.json"
# Operator-supplied stop/start/verify tooling for the restart path (see the docstring).
TOOLS = os.environ.get("LASTSIETCH_EFFECTS_TOOLS", "/opt/lastsietch-event-effects/hotfix-tools")
BC = "/root/dune-service-broadcast.py"
DQ = "/root/dq.sh"
LEASE = "/opt/lastsietch-chat-rolling/current/game-maintenance.py"
NS = os.environ.get("LASTSIETCH_GAME_NAMESPACE", "")
BG = os.environ.get("LASTSIETCH_GAME_BATTLEGROUP", "")
EXPECTED_GAME_PODS = 32
FB_USER = "/srv/UserSettings/UserServerCustomSettings.ini"
FB_GEN = "/srv/Config/LinuxServer/ServerCustomSettings.ini"

EFFECTS = ("xp", "spec_xp", "crafting", "harvest")
SCOPES = ("fleet", "amtal")
PHASES = ("start", "end")
# Live effects: the cvars, the value while the window is open, and the value of
# record they go back to (ops/game-config/values-of-record.json: both mining cvars
# are 1.0 since 2026-10-06; the spec XP cvar is a runtime-only 1.0 default).
LIVE = {
    "spec_xp": ({"dw.LandsraadMissionRewardMultiplierSpecializationXP": "2.0"},
                {"dw.LandsraadMissionRewardMultiplierSpecializationXP": "1.0"}),
    "harvest": ({"Dune.GlobalMiningOutputMultiplier": "2.0", "Dune.GlobalVehicleMiningOutputMultiplier": "2.0"},
                {"Dune.GlobalMiningOutputMultiplier": "1.0", "Dune.GlobalVehicleMiningOutputMultiplier": "1.0"}),
}
# Restart effects: one UserServerCustomSettings key, its value of record and its event value.
RESTART = {
    "xp": ("GlobalXpMultiplier", "1.000000", "2.000000", "Double XP weekend"),
    "crafting": ("CraftingCost", "1.000000", "0.800000", "Crafting weekend"),
}
# A partition-scoped effect reaches one pod: ServerExec routed to that pod's server_id.
# Partition and pod come from the map-instance config (LASTSIETCH_MAP_INSTANCES_FILE: entries
# with "key", "partition" and "pod"); without the file, the defaults below.
MAP_INSTANCES = os.environ.get("LASTSIETCH_MAP_INSTANCES_FILE", TEST_ROOT + "/etc/lastsietch/map-instances.json")
SCOPE_DEFAULTS = {"amtal": (33, "-sg-survival-1-pod-33")}


def scope_targets(path=None):
    targets = dict(SCOPE_DEFAULTS)
    try:
        with open(path or MAP_INSTANCES) as fh:
            rows = json.load(fh)["instances"]
    except (OSError, ValueError, KeyError, TypeError):
        return targets
    for row in rows:
        if row.get("key") in targets and isinstance(row.get("partition"), int) and row.get("pod"):
            targets[row["key"]] = (row["partition"], row["pod"])
    return targets


_TARGETS = scope_targets()
SCOPE_PARTITION = {k: v[0] for k, v in _TARGETS.items()}
SCOPE_POD = {k: v[1] for k, v in _TARGETS.items()}
DEADMAN_GRACE_S = 1800
SLEEP_SCALE = float(os.environ.get("LASTSIETCH_EFFECTS_SLEEP_SCALE", "1"))


def now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    line = "%s %s" % (now_ts(), msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def sleep(seconds):
    time.sleep(seconds * SLEEP_SCALE)


def sh(argv, timeout=120, input_text=None):
    """(rc, stdout+stderr). Every external call goes through here, so the suite
    can replace the box with a recorder."""
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=input_text,
                             stdin=None if input_text is not None else subprocess.DEVNULL)
        return out.returncode, (out.stdout or "") + (out.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, "%s: %s" % (type(exc).__name__, exc)


def alert(text):
    """One line to the staff log channel (the pod watcher's credentials), never a mention."""
    env = {}
    try:
        for line in open(ALERT_ENV):
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                env[k] = v.strip().strip('"')
    except OSError:
        log("alert (no env): " + text)
        return
    if not env.get("DISCORD_BOT_TOKEN") or not env.get("DISCORD_CH_BOT_LOGS"):
        log("alert (no token): " + text)
        return
    body = json.dumps({"content": text[:1900], "allowed_mentions": {"parse": []}})
    sh(["curl", "-fsS", "-m", "10", "-X", "POST", "-H", "Authorization: Bot " + env["DISCORD_BOT_TOKEN"],
        "-H", "Content-Type: application/json", "-d", body,
        "https://discord.com/api/v10/channels/%s/messages" % env["DISCORD_CH_BOT_LOGS"]])


# --- job records -------------------------------------------------------------

def job_path(event_id, phase):
    return os.path.join(STATE_DIR, "%d.%s" % (int(event_id), phase))


def write_job(event_id, phase, line):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = job_path(event_id, phase) + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(line.replace("\n", " ")[:500] + "\n")
    os.replace(tmp, job_path(event_id, phase))


def read_job(event_id, phase):
    try:
        return open(job_path(event_id, phase)).read().strip()
    except OSError:
        return ""


def window_path(event_id):
    return os.path.join(WINDOW_DIR, str(int(event_id)))


def write_window(event_id, effect, scope, end_epoch):
    os.makedirs(WINDOW_DIR, exist_ok=True)
    with open(window_path(event_id), "w") as fh:
        fh.write("%s %s %d\n" % (effect, scope, int(end_epoch)))


def windows():
    out = []
    try:
        names = sorted(os.listdir(WINDOW_DIR))
    except OSError:
        return out
    for name in names:
        if not name.isdigit():
            continue
        try:
            effect, scope, end = open(os.path.join(WINDOW_DIR, name)).read().split()
            if effect in LIVE and scope in SCOPES:
                out.append((int(name), effect, scope, int(end)))
        except (OSError, ValueError):
            log("window %s unreadable, ignored" % name)
    return out


def remove_window(event_id):
    try:
        os.remove(window_path(event_id))
    except FileNotFoundError:
        pass


# --- live cvars --------------------------------------------------------------

def server_id(partition):
    rc, out = sh([DQ, "-At", "-c", "select f.server_id from dune.farm_state f join dune.world_partition w"
                  " on w.server_id = f.server_id where w.partition_id = %d;" % int(partition)])
    ids = [l.strip() for l in out.splitlines() if l.strip() and not l.startswith(("SET", "ERROR"))]
    return ids[0] if rc == 0 and len(ids) == 1 and re.fullmatch(r"[A-Za-z0-9._+/=-]{1,128}", ids[0]) else None


def target_pods(scope):
    rc, out = sh(["kubectl", "get", "pods", "-n", NS, "--no-headers"])
    if rc != 0:
        return []
    pods = [f[0] for f in (l.split() for l in out.splitlines())
            if len(f) >= 3 and "-pod-" in f[0] and f[2] == "Running"]
    if scope != "fleet":
        pods = [p for p in pods if p.endswith(SCOPE_POD[scope])]
    return pods


def server_exec(command, routing):
    argv = ["python3", BC, "--send", "--operator", "lastsietch-events"]
    if routing:
        argv += ["--routing-key", routing]
    return sh(argv + ["serverexec", "--exec", command], timeout=60)[0] == 0


def routing_for(scope):
    """(ok, routing key or None). Fleet = the default fan-out to every pod."""
    if scope == "fleet":
        return True, None
    sid = server_id(SCOPE_PARTITION[scope])
    return sid is not None, sid


def set_cvars(values, scope):
    ok, routing = routing_for(scope)
    if not ok:
        return False
    return all([server_exec("%s %s" % (name, value), routing) for name, value in values.items()])


def readback(values, scope):
    """(ok, detail). A pod counts only when its own log echoes every wanted value
    after a fresh read; no pods is a failure."""
    sleep(8)
    ok, routing = routing_for(scope)
    if not ok:
        return False, "no single server_id for %s" % scope
    for name in values:
        server_exec(name, routing)
    sleep(6)
    pods = target_pods(scope)
    if not pods:
        return False, "readback 0/0: no Running game pod for %s" % scope
    good, bad = 0, []
    for pod in pods:
        rc, logs = sh(["kubectl", "logs", "-n", NS, pod, "--since=20s"], timeout=60)
        if rc == 0 and all('%s = "%s' % (name, want) in logs for name, want in values.items()):
            good += 1
        else:
            bad.append(pod.rsplit("-sg-", 1)[-1])
    detail = "readback %d/%d" % (good, len(pods))
    if bad:
        detail += " (missing: %s)" % ", ".join(bad[:8])
    return good == len(pods), detail


def baseline_check(on, off, scope):
    """(ok, detail) before a start: every target pod reads each cvar at its value
    of record or already at the effect's (a hand run of the same weekend). Any
    other value is tuning someone set on purpose, and the end would overwrite it."""
    ok, routing = routing_for(scope)
    if not ok:
        return False, "no single server_id for %s" % scope
    for name in on:
        server_exec(name, routing)
    sleep(6)
    pods = target_pods(scope)
    if not pods:
        return False, "no Running game pod for %s" % scope
    for pod in pods:
        rc, logs = sh(["kubectl", "logs", "-n", NS, pod, "--since=20s"], timeout=60)
        for name in on:
            if rc != 0 or not any('%s = "%s' % (name, v) in logs for v in (off[name], on[name])):
                return False, "%s on %s is neither %s nor %s; not starting, so the end cannot overwrite it" % (
                    name, pod.rsplit("-sg-", 1)[-1], off[name], on[name])
    return True, ""


def run_live(phase, event_id, effect, scope, end_epoch):
    on, off = LIVE[effect]
    if phase == "start":
        ok, detail = baseline_check(on, off, scope)
        if not ok:
            return False, "precheck: " + detail
        set_cvars(on, scope)
        ok, detail = readback(on, scope)
        if ok:
            write_window(event_id, effect, scope, end_epoch)
            return True, detail
        # Half-applied is worse than not applied: put it back, then report.
        set_cvars(off, scope)
        back, back_detail = readback(off, scope)
        return False, "%s; reverted to baseline (%s)" % (detail, back_detail if back else "revert " + back_detail)
    # The window goes first: with it still present, the assert timer would put the
    # effect straight back while an early end (unpublish, cancel) is in progress.
    remove_window(event_id)
    set_cvars(off, scope)
    return readback(off, scope)


# --- restart effects ---------------------------------------------------------

def broadcast(title, message, seconds=60):
    sh(["python3", BC, "--send", "--operator", "lastsietch-events", "generic", "--title", title,
        "--message", message, "--duration", str(seconds)], timeout=60)


def current_tag():
    rc, out = sh(["kubectl", "get", "battlegroup", BG, "-n", NS, "-o", "jsonpath={.spec}"])
    tags = re.findall(r"seabass-server[^\"]*:([0-9a-z-]+)", out) if rc == 0 else []
    if not tags:
        return None
    return max(set(tags), key=tags.count)


def fb_pod():
    rc, out = sh(["kubectl", "get", "pods", "-n", NS, "--no-headers"])
    pods = [l.split()[0] for l in out.splitlines() if "-fb-deploy-" in l] if rc == 0 else []
    return pods[0] if pods else None


def settings_value(fb, path, key):
    """The values of `key` in one settings file, as a list (one is the only sane answer)."""
    rc, out = sh(["kubectl", "exec", "-n", NS, fb, "--", "grep", "-E", "^%s=" % key, path])
    return [l.split("=", 1)[1].strip() for l in out.splitlines() if l.startswith(key + "=")]


EDIT = r'''set -u
K=$1; FROM=$2; TO=$3; SUF=$4; U=$5; G=$6
cp -p "$U" "$U.bak-$SUF"; cp -p "$G" "$G.bak-$SUF"
[ "$(grep -c "^$K=" "$U")" = "1" ] && grep -qx "$K=$FROM" "$U" || { echo "GATE: $U does not hold exactly one $K=$FROM"; exit 1; }
sed -i "s|^$K=$FROM\$|$K=$TO|" "$U"
if grep -q "^$K=" "$G"; then sed -i "s|^$K=.*\$|$K=$TO|" "$G"; fi
u_changed=$(grep -vxFf "$U.bak-$SUF" "$U" | wc -l)
want=1; [ "$FROM" = "$TO" ] && want=0
if [ "$u_changed" = "$want" ] && grep -qx "$K=$TO" "$U" && [ "$(wc -l < "$U")" = "$(wc -l < "$U.bak-$SUF")" ] \
   && { ! grep -q "^$K=" "$G" || grep -qx "$K=$TO" "$G"; }; then
  echo "EDIT OK $K=$TO (user changed $u_changed lines; backups .bak-$SUF)"
else
  cp -p "$U.bak-$SUF" "$U"; cp -p "$G.bak-$SUF" "$G"; echo "GATE FAILED: both files restored"; exit 1
fi
'''


def edit_settings(fb, key, from_val, to_val, suffix):
    rc, out = sh(["kubectl", "exec", "-i", "-n", NS, fb, "--", "sh", "-s", "--", key, from_val, to_val,
                  suffix, FB_USER, FB_GEN], input_text=EDIT, timeout=60)
    log("settings edit: " + out.strip().replace("\n", " | "))
    return rc == 0 and "EDIT OK" in out, out.strip().splitlines()[-1:] or ["no output"]


def lease(mode, owner):
    argv = ["python3", "-B", LEASE, mode, "--kind", "operator", "--owner", owner]
    if mode == "claim":
        argv.append("--no-preempt")
    rc, out = sh(argv, timeout=60)
    return rc == 0 and ('"held"' in out or '"released"' in out), out.strip()


def restart_cycle(owner, key, from_val, to_val, label, lines):
    """The clean fleet stop of every settings window: lease, T-10 and T-1, holadmin
    out of the way, spec.stop=true to 0 processes, edit both files in the gap with
    a gate, stop=false to REGISTERED, verify, holadmin back, lease and mutes off.
    (ok, detail). Never forces: a failed gate puts the files back and starts the
    fleet unchanged; it never retries."""
    fb, tag = fb_pod(), current_tag()
    if not fb or not tag:
        return False, "precheck: filebrowser pod or image tag unreadable"
    have = settings_value(fb, FB_USER, key)
    if have != [from_val]:
        return False, "precheck: %s reads %s in the user file, expected exactly %s" % (key, have, from_val)
    held, out = lease("claim", owner)
    if not held:
        return False, "maintenance lease not free: " + out[:200]
    try:
        broadcast(lines[0], lines[1])
        sleep(540)
        broadcast(lines[2], lines[3])
        sleep(60)
        rc, out = sh(["bash", TOOLS + "/holadmin-drop.sh", owner], timeout=600)
        if rc != 0:
            broadcast("Restart cancelled", "The restart is off. Carry on.", 30)
            return False, "holadmin drop refused, nothing stopped: " + out.strip()[-200:]
        rc, out = sh(["bash", TOOLS + "/stop.sh"], timeout=900)
        log("stop: " + out.strip()[-400:])
        if rc != 0 or "DRAINED" not in out:
            sh(["bash", TOOLS + "/start.sh", tag, str(EXPECTED_GAME_PODS)], timeout=1800)
            sh(["bash", TOOLS + "/holadmin-restore.sh"], timeout=600)
            return False, "stop did not drain; fleet started again with settings unchanged"
        edited, why = edit_settings(fb, key, from_val, to_val, "%s-%s" % (owner, time.strftime("%Y%m%dT%H%M%S")))
        rc, out = sh(["bash", TOOLS + "/start.sh", tag, str(EXPECTED_GAME_PODS)], timeout=1800)
        log("start: " + out.strip()[-400:])
        registered = rc == 0 and "REGISTERED" in out
        rc2, out2 = sh(["bash", TOOLS + "/verify.sh", tag], timeout=600)
        log("verify: " + out2.strip().replace("\n", " | ")[-1500:])
        rc3, out3 = sh(["bash", TOOLS + "/holadmin-restore.sh"], timeout=600)
        restored = "HOLADMIN RESTORED" in out3
        if not edited:
            return False, "settings gate failed (%s); fleet restarted unchanged" % why[0]
        if not registered:
            return False, "edited, but the fleet did not register in 22 min: page the owner"
        user, gen = settings_value(fb, FB_USER, key), settings_value(fb, FB_GEN, key)
        if user != [to_val] or gen not in ([], [to_val]):
            return False, "after boot %s reads user=%s generated=%s, wanted %s" % (key, user, gen, to_val)
        detail = "%s=%s in both files after boot, fleet registered (runtime value has no read path)" % (key, to_val)
        if not restored:
            detail += "; HOLADMIN RESTORE FAILED, run holadmin-restore.sh"
        return True, detail
    finally:
        lease("release", owner)
        sh(["/opt/lastsietch-bg-watchdog/pause.sh", "off"])
        sh(["/opt/lastsietch-pod-watcher/maint-window.sh", "off"])
        do_assert()


def run_restart(phase, event_id, effect):
    if not os.path.exists(RESTART_ENABLED):
        return False, ("restart effects are DARK until the owner-watched L19 rehearsal "
                       "(no %s); nothing was stopped" % RESTART_ENABLED)
    key, base, on, label = RESTART[effect]
    from_val, to_val = (base, on) if phase == "start" else (on, base)
    verb = "starts" if phase == "start" else "ends"
    lines = ("%s %s in 10 min" % (label, verb),
             "All maps restart in 10 minutes for about 20 minutes. Log out to save your progress.",
             "Restart in 1 minute",
             "All maps restart in 1 minute for the %s. Log out now." % label)
    ok, detail = restart_cycle("event-%d-%s" % (int(event_id), phase), key, from_val, to_val, label, lines)
    if ok:
        write_config_expected(key, to_val if phase == "start" else None)
    return ok, detail


def write_config_expected(key, value):
    """Publish (value) or withdraw (None) the effect's settings value for the config watch."""
    if value is None:
        try:
            os.remove(CONFIG_EXPECTED)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(CONFIG_EXPECTED), exist_ok=True)
    with open(CONFIG_EXPECTED, "w") as fh:
        json.dump({"UserServerCustomSettings.ini": {"/Script/DuneSandbox.UserServerCustomSettings": {key: [value]}}},
                  fh, indent=2, sort_keys=True)


# --- commands ----------------------------------------------------------------

def valid(phase, event_id, effect, scope, end_epoch=None):
    if phase not in PHASES or not re.fullmatch(r"[0-9]{1,9}", str(event_id)):
        return False
    if effect not in EFFECTS or scope not in SCOPES:
        return False
    if effect in RESTART and scope != "fleet":
        return False
    if phase == "start" and not re.fullmatch(r"[0-9]{10}", str(end_epoch or "")):
        return False
    return True


def cmd_launch(args):
    phase, event_id, effect, scope = args[:4]
    end_epoch = args[4] if len(args) > 4 else None
    if len(args) != (5 if phase == "start" else 4) or not valid(phase, event_id, effect, scope, end_epoch):
        print("refused: bad arguments")
        return 2
    if not os.path.exists(ENABLED):
        print("refused: event effects disabled (no %s)" % ENABLED)
        return 3
    unit = "lastsietch-event-%d-%s" % (int(event_id), phase)
    if sh(["systemctl", "is-active", "--quiet", unit])[0] == 0:
        print("already running")
        return 0
    write_job(event_id, phase, "running %s" % now_ts())
    argv = ["systemd-run", "--unit", unit, "--collect", "--quiet", "/usr/bin/python3", SELF, "run", phase,
            str(int(event_id)), effect, scope] + ([str(end_epoch)] if end_epoch else [])
    rc, out = sh(argv)
    if rc != 0:
        write_job(event_id, phase, "failed %s systemd-run: %s" % (now_ts(), out.strip()[:200]))
        print("failed systemd-run")
        return 1
    log("launched %s %s %s %s" % (unit, effect, scope, end_epoch or ""))
    print("launched")
    return 0


def cmd_status(args):
    if len(args) != 2 or not re.fullmatch(r"[0-9]{1,9}", args[0]) or args[1] not in PHASES + ("assert",):
        print("refused: bad arguments")
        return 2
    print(read_job(args[0], args[1]) or "none")
    return 0


def cmd_run(args):
    phase, event_id, effect, scope = args[:4]
    end_epoch = args[4] if len(args) > 4 else None
    if not valid(phase, event_id, effect, scope, end_epoch):
        write_job(event_id, phase, "failed %s bad arguments" % now_ts())
        return 2
    try:
        if effect in LIVE:
            ok, detail = run_live(phase, int(event_id), effect, scope, end_epoch)
        else:
            ok, detail = run_restart(phase, int(event_id), effect)
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, "worker error: %s" % exc
    write_job(event_id, phase, "%s %s %s" % ("ok" if ok else "failed", now_ts(), detail))
    log("%s %s %s %s: %s" % (phase, event_id, effect, "ok" if ok else "FAILED", detail))
    if not ok:
        alert("⚠️ Event effect %s %s (event %s, %s): FAILED, %s" % (effect, phase, event_id, scope, detail))
    return 0 if ok else 1


def do_assert():
    if not os.path.exists(ENABLED):
        return 0
    now = int(time.time())
    for event_id, effect, scope, end in windows():
        on, off = LIVE[effect]
        if now < end:
            set_cvars(on, scope)
            ok, detail = readback(on, scope)
            write_job(event_id, "assert", "%s %s %s" % ("ok" if ok else "failed", now_ts(), detail))
            if not ok:
                log("assert %s %s %s: %s" % (event_id, effect, scope, detail))
        elif now >= end + DEADMAN_GRACE_S:
            # <web-host> never sent the end: close the window here so the effect
            # cannot outlive its weekend.
            remove_window(event_id)
            set_cvars(off, scope)
            ok, detail = readback(off, scope)
            write_job(event_id, "end", "%s %s dead-man end: %s" % ("ok" if ok else "failed", now_ts(), detail))
            log("dead-man end %s %s %s: %s" % (event_id, effect, scope, detail))
            alert("⚠️ Event effect %s (event %s) ended on <game-host> %d min after its window: <web-host> never "
                  "sent the end. %s" % (effect, event_id, (now - end) // 60, detail))
    return 0


def cmd_rehearse(args):
    if len(args) != 2 or args[0] != "--owner-go" or len(args[1].strip()) < 3:
        print("usage: rehearse --owner-go '<the owner's text go, quoted>'")
        return 2
    log("L19 rehearsal begins, owner go: %s" % args[1][:200])
    key, base, _on, _label = RESTART["xp"]
    lines = ("Maintenance in 10 min", "All maps restart in 10 minutes for about 20 minutes. Log out to save your progress.",
             "Restart in 1 minute", "All maps restart in 1 minute. Log out now.")
    ok, detail = restart_cycle("event-rehearsal", key, base, base, "maintenance", lines)
    log("L19 rehearsal %s: %s" % ("OK" if ok else "FAILED", detail))
    print("REHEARSAL %s: %s" % ("OK" if ok else "FAILED", detail))
    return 0 if ok else 1


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "launch" and len(args) >= 4:
        return cmd_launch(args)
    if cmd == "status":
        return cmd_status(args)
    if cmd == "run" and len(args) >= 4:
        return cmd_run(args)
    if cmd == "assert" and not args:
        return do_assert()
    if cmd == "rehearse":
        return cmd_rehearse(args)
    print("refused: unknown command")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
