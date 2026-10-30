"""Weekend effects on portal events: the store side (WEEKEND-EVENTS-PLAN Stage 3).

An effect event changes the game for its window. This module owns only what an
admin write may set and what the write does to the effect's state; the runner
(ops/weekend-events/event-effects.py on <web-host>) owns every transition after
that and is the only thing that ever sets running, done, failed or missed.

  * THE EFFECT IS A CLOSED ENUM and the window is a fixed length counted in
    Eastern wall time, so a weekend that crosses the DST change still ends at
    Sunday 24:00 ET.
  * PUBLISHING IS THE GO (owner ruling 2): a published effect event is armed,
    a draft or a cancelled one is not. A running effect is never disarmed by a
    write: the runner sees the event withdrawn and ends it, which needs the
    effect and its window unchanged, so those fields are frozen while it runs.

Stdlib only, so the store's test suite can execute it directly.
"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

EFFECTS = ("none", "xp", "spec_xp", "crafting", "harvest")
LIVE_EFFECTS = ("spec_xp", "harvest")        # runtime cvars, no restart
RESTART_EFFECTS = ("xp", "crafting")         # settings files, clean fleet stop
SCOPES = ("fleet", "amtal")
STATES = ("armed", "running", "done", "failed", "missed")
# Fri 00:00 to Sun 24:00 ET; the same plus a Monday holiday; 7 days (Christmas).
WINDOW_HOURS = (72, 96, 168)
WINDOW_LABELS = {72: "Friday 00:00 to Sunday 24:00 ET", 96: "With a Monday holiday (4 days)",
                 168: "7 days"}

# Friday ordinal in the month -> effect (owner ruling 1; a 5th Friday repeats Landsraad).
ROTATION = {1: "xp", 2: "spec_xp", 3: "crafting", 4: "harvest", 5: "spec_xp"}

COPY = {
    "xp": ("Double XP weekend", "monthly-reward",
           "Double XP on every map all weekend, Friday 00:00 to Sunday 24:00 ET. "
           "All maps restart for about 20 minutes at the start and again at the end, "
           "with in-game warnings 10 minutes and 1 minute before."),
    "spec_xp": ("Double Landsraad weekend", "landsraad-term",
                "Double specialization XP from Landsraad mission rewards on every map, "
                "Friday 00:00 to Sunday 24:00 ET. No restart."),
    "crafting": ("Crafting weekend", "sietch-almanac",
                 "Crafting costs 20% less on every map, Friday 00:00 to Sunday 24:00 ET. "
                 "All maps restart for about 20 minutes at the start and again at the end, "
                 "with in-game warnings 10 minutes and 1 minute before."),
    "harvest": ("Harvest weekend", "spoils-of-kanly",
                "Double cutteray and vehicle mining output on every map, Friday 00:00 to "
                "Sunday 24:00 ET. It stacks with the PvP zone bonus on Amtal. No restart."),
}

FROZEN_WHILE_RUNNING = ("effect", "effect_scope", "starts_utc", "ends_utc")


def _utc(stored):
    try:
        return datetime.strptime(str(stored or "")[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _col(row, key):
    if row is None:
        return None
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def wall_hours(starts_utc, ends_utc):
    """The window length in Eastern wall-clock hours, or None."""
    start, end = _utc(starts_utc), _utc(ends_utc)
    if start is None or end is None:
        return None
    def naive(moment):
        return moment.astimezone(EASTERN).replace(tzinfo=None)
    return (naive(end) - naive(start)).total_seconds() / 3600


def end_for(starts_utc, hours):
    """starts_utc + hours of Eastern wall time, as the stored UTC string."""
    start = _utc(starts_utc)
    if start is None:
        return None
    local = start.astimezone(EASTERN).replace(tzinfo=None) + timedelta(hours=int(hours))
    return local.replace(tzinfo=EASTERN).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def in_flight(row):
    """Running, or a <game-host> job already launched for it: either way the game
    may be changed, so only the runner may move it on."""
    return _col(row, "effect_state") == "running" or bool(_col(row, "effect_job"))


def validate(body, existing, starts_utc, ends_utc):
    """(effect, scope, error, message) for one write. `existing` is the row an
    update merges into, or None."""
    raw = body.get("effect") if "effect" in body else (_col(existing, "effect") or "none")
    effect = str(raw or "none").strip().lower()
    if effect not in EFFECTS:
        return None, None, "invalid_effect", "Pick an effect from the list: %s." % ", ".join(EFFECTS)
    if effect == "none":
        scope = None
    else:
        raw = body.get("effect_scope") if "effect_scope" in body else _col(existing, "effect_scope")
        scope = str(raw or "fleet").strip().lower()
        if scope not in SCOPES:
            return None, None, "invalid_effect_scope", "An effect runs on: %s." % ", ".join(SCOPES)
        if effect in RESTART_EFFECTS and scope != "fleet":
            return None, None, "invalid_effect_scope", \
                "XP and crafting change the shared settings files, so they are fleet-wide."

    if in_flight(existing):
        after = {"effect": effect, "effect_scope": scope, "starts_utc": starts_utc, "ends_utc": ends_utc}
        if any((_col(existing, k) or None) != (after[k] or None) for k in FROZEN_WHILE_RUNNING):
            return None, None, "effect_running", \
                "This effect is running or starting, so its effect, map and window cannot change. " \
                "Unpublish or cancel the event to end it early."

    if effect != "none":
        unchanged = existing is not None and all(
            (_col(existing, k) or None) == (v or None) for k, v in
            (("effect", effect), ("starts_utc", starts_utc), ("ends_utc", ends_utc)))
        if not unchanged and wall_hours(starts_utc, ends_utc) not in WINDOW_HOURS:
            return None, None, "invalid_effect_window", \
                "An effect event runs for a fixed window: %s." % "; ".join(
                    "%dh (%s)" % (h, WINDOW_LABELS[h]) for h in WINDOW_HOURS)
    return effect, scope, None, None


def state_after_write(existing, fields, rearm=False):
    """(effect_state, effect_error) a create or edit leaves behind."""
    state, error = _col(existing, "effect_state"), _col(existing, "effect_error")
    if in_flight(existing):
        return state, error
    if fields["effect"] == "none":
        return None, None
    published = fields["status"] == "published"
    if state in (None, "armed"):
        return ("armed" if published else None), None
    rescheduled = any((_col(existing, k) or None) != (fields.get(k) or None) for k in FROZEN_WHILE_RUNNING)
    if published and (rescheduled or rearm):
        return "armed", None
    return state, error


def fridays(year, month):
    day = date(year, month, 1)
    day += timedelta(days=(4 - day.weekday()) % 7)
    out = []
    while day.month == month:
        out.append(day)
        day += timedelta(days=7)
    return out


def month_drafts(year, month):
    """The rotation's weekends for one month as store field dicts (drafts, synced
    to Discord once published), Friday 00:00 ET for 72 wall-clock hours."""
    out = []
    for ordinal, friday in enumerate(fridays(year, month), start=1):
        effect = ROTATION[ordinal]
        title, banner, description = COPY[effect]
        start = datetime(friday.year, friday.month, friday.day, tzinfo=EASTERN).astimezone(timezone.utc)
        starts_utc = start.strftime("%Y-%m-%d %H:%M:%S")
        out.append({"title": title, "kind": "community", "banner": banner,
                    "starts_utc": starts_utc, "ends_utc": end_for(starts_utc, 72),
                    "map_name": None, "host": None, "description": description,
                    "status": "draft", "discord_sync": 1,
                    "effect": effect, "effect_scope": "fleet"})
    return out
