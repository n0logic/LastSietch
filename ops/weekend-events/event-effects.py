#!/usr/bin/env python3
"""Weekend effect runner on <web-host> (WEEKEND-EVENTS-PLAN Stage 4, admin plan G2).

lastsietch-event-effects.timer runs this once a minute. It reads armed and running
effect events from admin.db and, for a due start or end, calls <game-host> through
the forced-command dispatcher with fixed tokens (numeric and enum args only):

  event-effect-start <id> <effect> <scope> <end_epoch>
  event-effect-end <id> <effect> <scope>
  event-effect-status <id> <start|end|assert>

<game-host> runs each job as a transient unit and this runner polls its status, so
all state lives in admin.db and a restart of the timer resumes, never repeats.

State machine (the store sets armed; only this runner moves it on):

  armed   -> start launched (effect_job=start) -> running   readback ok on EVERY target pod
                                               -> failed    readback short, refused, or no report
  armed   -> missed   the window closed before a start ran, or a restart effect is
                      more than 60 min late (never run at an odd hour)
  running -> end launched (effect_job=end)     -> done      at the end, or once unpublished/cancelled
                                               -> failed    the end did not read back
  running -> failed   a restart effect's end more than 60 min late (left for a planned window)

A live effect (spec_xp, harvest) that starts late catches up, and the lateness is
recorded in effect_error and posted, never silent. A transport failure (ssh down)
retries on the next tick inside the same limits.

Every write is compare-and-set on (effect_state, effect_job) and never touches
updated_at, so an admin edit is never overwritten and the Discord sync revision
does not move. Kill switch: without /etc/lastsietch/event-effects-enabled nothing runs.

Start notices (admin plan G3 minimum): when a live effect goes running, one
in-game broadcast (the relay's broadcast-send) and one #announcements post (the
signed notice sender) with the event title and the first line of its
description. Once per event start: only the tick whose compare-and-set moved the
row to running sends, after claiming /var/lib/lastsietch-event-effects/<id>.<start>.notice
with O_EXCL. The claim comes first, so a crash mid-send loses a notice rather
than repeating it, and a re-armed event with the same start never re-announces.
A notice failure is logged, written to the claim file and posted to bot-logs; it
never touches the effect. Dark until /etc/lastsietch/event-effects-notices-enabled.
"""
import base64
import json
import os
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

DB = os.environ.get("LASTSIETCH_DB_PATH", "/var/lib/lastsietch-admin/admin.db")
ENABLED = os.environ.get("LASTSIETCH_EFFECTS_ENABLED_FILE", "/etc/lastsietch/event-effects-enabled")
LOG = os.environ.get("LASTSIETCH_EFFECTS_LOG", "/var/log/lastsietch-event-effects.log")
NOTICES_ENABLED = os.environ.get("LASTSIETCH_EFFECTS_NOTICES_FILE", "/etc/lastsietch/event-effects-notices-enabled")
STATE_DIR = os.environ.get("LASTSIETCH_EFFECTS_STATE_DIR", "/var/lib/lastsietch-event-effects")
ALERT_ENV = "/opt/lastsietch-event-effects/.alert.env"
MAKE_NOTICE = "/opt/lastsietch-event-effects/make-notice.py"
# The pinned notices release that sends the Discord post.
NOTICE_SEND = os.environ.get("LASTSIETCH_NOTICE_SEND", "/opt/lastsietch-notices/releases/fe92ada49a9ad0f6/send.py")
NOTICE_TOKEN_CMD = '["sh","-c","grep ^DISCORD_BOT_TOKEN= /opt/cielago/.env | cut -d= -f2-"]'
SSH_KEY = "/opt/lastsietch-relay/.ssh/id_ed25519"
# The game host the start notice goes through (its dispatcher runs broadcast-send).
SSH_TARGET = os.environ.get("LASTSIETCH_EFFECTS_SSH_TARGET", "")

LIVE_EFFECTS = ("spec_xp", "harvest")
RESTART_EFFECTS = ("xp", "crafting")
SCOPES = ("fleet", "amtal")
LATE_NOTE_S = 300                  # a live start later than this is recorded and posted
RESTART_LATE_LIMIT_S = 3600        # plan failure rule: more than 60 min late = skipped
RESTART_LEAD_S = 600               # restart jobs launch early: T-10 and T-1 come first
JOB_TIMEOUT_S = {"live": 900, "restart": 5400}
CHECK_EVERY_MIN = 5
# broadcast-send limits (scripts/dune-broadcast-send.py)
TITLE_MAX, MESSAGE_MAX = 80, 280
MENTIONS = ("@everyone", "@here", "<@")
DEFAULT_NOTICE = {
    "spec_xp": ("Double Landsraad weekend",
                "Double specialization XP from Landsraad mission rewards is live now."),
    "harvest": ("Harvest weekend", "Double cutteray and vehicle mining output is live now."),
}

_COLUMNS = ("id, title, description, status, starts_utc, ends_utc, effect, effect_scope, effect_state, "
            "effect_error, effect_job, effect_job_at, effect_started_at")


def stamp(moment):
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def parse(text):
    try:
        return datetime.strptime(str(text or "")[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def log(msg):
    line = "%s %s" % (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), msg)
    print(line, flush=True)
    try:
        with open(LOG, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def alert(text):
    env = {}
    try:
        for line in open(ALERT_ENV):
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                env[k] = v.strip().strip('"')
    except OSError:
        pass
    if not env.get("DISCORD_BOT_TOKEN") or not env.get("DISCORD_CH_BOT_LOGS"):
        log("alert (no channel configured): " + text)
        return
    req = urllib.request.Request(
        "https://discord.com/api/v10/channels/%s/messages" % env["DISCORD_CH_BOT_LOGS"],
        data=json.dumps({"content": text[:1900], "allowed_mentions": {"parse": []}}).encode(),
        headers={"Authorization": "Bot " + env["DISCORD_BOT_TOKEN"], "Content-Type": "application/json",
                 "User-Agent": "DiscordBot (https://lastsietch.com, 1.0)"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as exc:  # noqa: BLE001
        log("alert failed (%s): %s" % (exc, text))


def ssh(*args):
    """(rc, last line). The worker logs to stdout before its one-word reply, so the reply is the last line.
    rc 255/124 = transport, retried next tick."""
    argv = ["timeout", "90", "ssh", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
            "-o", "StrictHostKeyChecking=yes", SSH_TARGET, " ".join(str(a) for a in args)]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, str(exc)
    text = (out.stdout or "").strip() or (out.stderr or "").strip()
    return out.returncode, text.splitlines()[-1] if text else ""


def pipe(argv, data, timeout=120):
    """(rc, stdout, stderr) with data on stdin. rc 124 = could not run or timed out."""
    try:
        out = subprocess.run(argv, input=data, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, "", str(exc)
    return out.returncode, out.stdout or "", out.stderr or ""


def kind(effect):
    return "restart" if effect in RESTART_EFFECTS else "live"


def decide(row, now):
    """The one decision for this tick: (action, detail). Pure: the clock and the
    row in, nothing read or written."""
    start, end = parse(row["starts_utc"]), parse(row["ends_utc"])
    effect, state, job = row["effect"], row["effect_state"], row["effect_job"]
    if effect not in LIVE_EFFECTS + RESTART_EFFECTS or row["effect_scope"] not in SCOPES:
        return ("fail", "effect %r on %r is not one the runner knows" % (effect, row["effect_scope"])) \
            if state == "armed" and not job else ("wait", "")
    if start is None or end is None:
        return ("fail", "the event has no fixed window") if state == "armed" and not job else ("wait", "")
    lead = timedelta(seconds=RESTART_LEAD_S if kind(effect) == "restart" else 0)
    if job:
        launched = parse(row["effect_job_at"])
        if launched and (now - launched).total_seconds() > JOB_TIMEOUT_S[kind(effect)]:
            return "timeout", "the %s job sent no result within %d min" % (job, JOB_TIMEOUT_S[kind(effect)] // 60)
        return "poll", job
    published = row["status"] == "published"
    if state == "armed":
        if not published or now < start - lead:
            return "wait", ""
        if now >= end:
            return "missed", "the window closed before the start ran"
        late = (now - start).total_seconds()
        if kind(effect) == "restart" and late > RESTART_LATE_LIMIT_S:
            return "missed", "the start was %d min late (limit 60): skipped, never restarted at an odd hour" % (late // 60)
        return "start", ("started %d min late" % (late // 60)) if late > LATE_NOTE_S else ""
    if state == "running":
        withdrawn = not published
        if withdrawn or now >= end - lead:
            late = (now - end).total_seconds()
            if kind(effect) == "restart" and not withdrawn and late > RESTART_LATE_LIMIT_S:
                return "fail", ("the end was %d min late (limit 60) and was not run; the settings still hold "
                                "the effect: end it in a planned window" % (late // 60))
            return "end", "withdrawn" if withdrawn else ""
        if now.minute % CHECK_EVERY_MIN == 0:
            return "check", ""
    return "wait", ""


def epoch(moment):
    return int(moment.timestamp())


def transition(conn, row, **cols):
    """Compare-and-set on what this tick read. 0 rows = an admin or a second
    runner moved it first; that is not an error, it is the next tick's input."""
    sets = ", ".join("%s = ?" % k for k in cols)
    cur = conn.execute("UPDATE portal_events SET %s WHERE id = ? AND effect_state IS ? AND effect_job IS ?" % sets,
                       list(cols.values()) + [row["id"], row["effect_state"], row["effect_job"]])
    conn.commit()
    return cur.rowcount == 1


def label(row):
    return "%s (event %d, %s)" % (row["effect"], row["id"], row["effect_scope"])


def act(conn, row, now):
    action, detail = decide(row, now)
    if action == "wait":
        return action
    if action in ("missed", "fail", "timeout"):
        state = "missed" if action == "missed" else "failed"
        if transition(conn, row, effect_state=state, effect_error=detail, effect_job=None):
            log("%s %s: %s" % (state, label(row), detail))
            alert("⚠️ Event effect %s %s: %s" % (label(row), state.upper(), detail))
        return action
    if action == "start":
        rc, out = ssh("event-effect-start", row["id"], row["effect"], row["effect_scope"],
                      epoch(parse(row["ends_utc"])))
        return launched(conn, row, now, "start", rc, out, detail)
    if action == "end":
        rc, out = ssh("event-effect-end", row["id"], row["effect"], row["effect_scope"])
        return launched(conn, row, now, "end", rc, out, row["effect_error"])
    if action == "poll":
        return poll(conn, row, now, detail)
    if action == "check":
        rc, out = ssh("event-effect-status", row["id"], "assert")
        if rc == 0 and out.startswith("failed"):
            note = "re-assert: " + out.split(" ", 2)[-1]
            if row["effect_error"] != note and transition(conn, row, effect_error=note):
                log("%s %s" % (label(row), note))
                alert("⚠️ Event effect %s re-assert short: %s" % (label(row), note))
        elif rc == 0 and out.startswith("ok") and (row["effect_error"] or "").startswith("re-assert"):
            transition(conn, row, effect_error=None)
        return action
    return action


def launched(conn, row, now, phase, rc, out, note):
    if rc == 0 and out in ("launched", "already running"):
        if transition(conn, row, effect_job=phase, effect_job_at=stamp(now), effect_error=note or None):
            log("%s launched for %s%s" % (phase, label(row), (": " + note) if note else ""))
            if note and phase == "start":
                alert("⚠️ Event effect %s %s: <web-host> was not running it on time." % (label(row), note))
        return phase
    if rc in (124, 255):
        log("%s for %s: transport failure (%s), retrying next tick" % (phase, label(row), out[:200]))
        return "retry"
    reason = "%s refused on <game-host>: %s" % (phase, out[:200] or "rc %d" % rc)
    if transition(conn, row, effect_state="failed", effect_error=reason):
        log("failed %s: %s" % (label(row), reason))
        alert("⚠️ Event effect %s FAILED: %s" % (label(row), reason))
    return "refused"


def poll(conn, row, now, phase):
    rc, out = ssh("event-effect-status", row["id"], phase)
    if rc != 0:
        log("status %s for %s: rc %d (%s), retrying" % (phase, label(row), rc, out[:200]))
        return "retry"
    if out.startswith("running"):
        return "running"
    if out.startswith("ok"):
        if phase == "start":
            ok = transition(conn, row, effect_state="running", effect_job=None, effect_started_at=stamp(now))
        else:
            ok = transition(conn, row, effect_state="done", effect_job=None, effect_ended_at=stamp(now),
                            effect_error=None if (row["effect_error"] or "").startswith("re-assert")
                            else row["effect_error"])
        if ok:
            log("%s %s: %s" % (phase, label(row), out))
            if phase == "start" and row["effect"] in LIVE_EFFECTS:
                announce(row)
        return "ok"
    reason = "%s: %s" % (phase, out.split(" ", 2)[-1] if out.startswith("failed") else
                         "<game-host> has no record of the job (%s)" % (out or "none"))
    if transition(conn, row, effect_state="failed", effect_job=None, effect_error=reason[:500]):
        log("failed %s: %s" % (label(row), reason))
        alert("⚠️ Event effect %s FAILED: %s" % (label(row), reason))
    return "failed"


# --- start notices -----------------------------------------------------------

def clean(text):
    return " ".join(str(text or "").split())


def notice_text(row):
    """(title, one line). The event's own words when they fit and carry no mention,
    else the effect's fixed copy."""
    title, line = DEFAULT_NOTICE[row["effect"]]
    own = clean(row["title"])
    if own and len(own) <= TITLE_MAX and not any(m in own for m in MENTIONS):
        title = own
    first = clean((row["description"] or "").strip().split("\n")[0])
    if len(first) > MESSAGE_MAX and ". " in first:
        first = first.split(". ")[0] + "."
    if first and len(first) <= MESSAGE_MAX and not any(m in first for m in MENTIONS):
        line = first
    return title, line


def claim_notice(row):
    """The claim file's path for exactly one caller per (event, start), else None.
    The file exists before anything is sent."""
    os.makedirs(STATE_DIR, exist_ok=True)
    start = parse(row["starts_utc"]).strftime("%Y%m%dT%H%M")
    path = os.path.join(STATE_DIR, "%d.%s.notice" % (row["id"], start))
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return None
    with os.fdopen(fd, "w") as fh:
        fh.write("claimed %s\n" % stamp(datetime.now(timezone.utc)))
    return path


def broadcast_ingame(title, line):
    """(ok, detail) through the relay key's broadcast-send (b64 JSON on stdin)."""
    job = json.dumps({"title": title, "message": line, "duration": 60, "mode": "apply",
                      "operator": "lastsietch-events"}, separators=(",", ":"), sort_keys=True)
    rc, out, err = pipe(["timeout", "90", "ssh", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                         "-o", "StrictHostKeyChecking=yes", SSH_TARGET, "broadcast-send"],
                        base64.b64encode(job.encode()).decode("ascii"))
    try:
        reply = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        reply = {}
    if rc == 0 and reply.get("success") is True:
        return True, "sent"
    return False, "rc %d: %s" % (rc, (reply.get("detail") or err or out or "no output").strip()[:200])


def post_discord(row, body):
    """(ok, detail): make-notice.py builds and hashes the job, the signed sender posts it."""
    rc, job, err = pipe(["python3", MAKE_NOTICE, "event-%d-start" % row["id"], body, "--minutes", "10",
                         "--channel", "announcements"], "", timeout=30)
    if rc != 0 or not job.strip():
        return False, "make-notice rc %d: %s" % (rc, (err or job).strip()[:200])
    rc, out, err = pipe(["python3", NOTICE_SEND, "discord", "--token-command-json", NOTICE_TOKEN_CMD], job)
    if rc == 0:
        return True, "sent"
    return False, "send rc %d: %s" % (rc, (err or out or "no output").strip()[:200])


def announce(row):
    """Never raises and never touches the row: the effect is already running."""
    if not os.path.exists(NOTICES_ENABLED):
        return "off"
    path = None
    try:
        path = claim_notice(row)
        if path is None:
            log("notice for %s: already sent for this start" % label(row))
            return "already"
        title, line = notice_text(row)
        results = [("in-game",) + broadcast_ingame(title, line),
                   ("discord",) + post_discord(row, "**%s** is live now. %s" % (title, line))]
    except Exception as exc:  # noqa: BLE001
        results = [("notice", False, "error: %s" % exc)]
    if path:
        try:
            with open(path, "a") as fh:
                fh.writelines("%s %s %s\n" % (name, "ok" if ok else "failed", detail) for name, ok, detail in results)
        except OSError:
            pass
    for name, ok, detail in results:
        log("notice %s for %s: %s" % (name, label(row), detail if ok else "FAILED " + detail))
        if not ok:
            alert("⚠️ Event effect %s start notice (%s) FAILED, the effect is running: %s" % (label(row), name, detail))
    return "sent" if all(ok for _n, ok, _d in results) else "failed"


def tick(now=None):
    if not os.path.exists(ENABLED):
        return []
    now = now or datetime.now(timezone.utc)
    # Opening as root is safe when lastsietch-admin holds no connection: SQLite run as root gives the
    # -wal/-shm it creates the database file's owner (proven on <web-host>, 3.45.1, 2026-10-29).
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT %s FROM portal_events WHERE effect_state IN ('armed', 'running')"
                            " AND effect IS NOT NULL AND effect <> 'none' ORDER BY starts_utc" % _COLUMNS).fetchall()
        return [(row["id"], act(conn, row, now)) for row in rows]
    finally:
        conn.close()


if __name__ == "__main__":
    for event_id, result in tick():
        if result not in ("wait", "running"):
            print("event %d: %s" % (event_id, result))
    sys.exit(0)
