"""Map instances: which worlds this server runs, read from one config file.

LASTSIETCH_MAP_INSTANCES_FILE (default /etc/lastsietch/map-instances.json) holds
{"instances": [{...}, ...]}. Each entry:

  key       required  unique id, a-z 0-9 and -; what scripts and flags name
  name      required  player-facing name
  map       required  the map_model.MAPS key the instance belongs to
  inst      optional  the instance key within that map (default: key)
  partition optional  dune.world_partition.partition_id, or null
  dim       optional  dune.actors.dimension_index, or null
  pod       optional  game pod name suffix, e.g. "-sg-survival-1-pod-33", or null
  pvp       optional  bool, default false
  closed    optional  bool, default false; a closed instance is shown nowhere
                      but its leftovers (roles, rows) are still recognised
  tracked   optional  bool, default true; false keeps positions, vehicles and
                      head counts for this instance off every public surface
  mode      optional  display mode (default "PvP" or "PvE" from pvp)
  label     optional  canonical name for ticket queues (default: name)
  aliases   optional  lower-case words players use for it in free text

A missing file means the built-in DEFAULTS, which are this server's own
instances. A file that fails validation also falls back to DEFAULTS and logs
why: a typo must not take a map off the portal. Check a file before installing
it with: python3 map_instances.py --check FILE

This module is copied byte for byte into the Cielago bot
(discord/cielago-bot/src/cielago/map_instances.py; scripts/tests/
test_map_instances.py holds the copies equal). Edit both. The game-host scripts
under ops/ read the same file with small inline readers of their own.
"""
import json
import logging
import os
import re
import sys

logger = logging.getLogger("map_instances")

DEFAULT_PATH = "/etc/lastsietch/map-instances.json"

DEFAULTS = (
    {"key": "habbanya", "name": "Habbanya", "map": "hagga", "inst": "habbanya",
     "partition": 1, "dim": 0, "pod": "-sg-survival-1-pod-1",
     "pvp": False, "closed": False, "tracked": True, "mode": "PvE",
     "label": "Habbanya (PvE)",
     "aliases": ("habbanya", "habanya", "habbanaya", "pve", "hagga pve")},
    # Closed 2026-10-28; its bases moved to amtal. Listed first among the PvP
    # sietches so a ticket that still says "kulon" keeps the player's word.
    {"key": "kulon", "name": "Kulon", "map": "hagga", "inst": "kulon",
     "partition": 32, "dim": 1, "pod": "-sg-survival-1-pod-32",
     "pvp": True, "closed": True, "tracked": True, "mode": "PvP",
     "label": "Kulon (closed, now Amtal)", "aliases": ("kulon",)},
    {"key": "amtal", "name": "Amtal", "map": "hagga", "inst": "amtal",
     "partition": 33, "dim": 2, "pod": "-sg-survival-1-pod-33",
     "pvp": True, "closed": False, "tracked": False, "mode": "Full PvP",
     "label": "Amtal-Full-PvP", "aliases": ("amtal", "pvp")},
    {"key": "dd-pve", "name": "Deep Desert", "map": "deep-desert", "inst": "pve",
     "partition": None, "dim": 0, "pod": None,
     "pvp": False, "closed": False, "tracked": True, "mode": "PvE",
     "label": "Deep Desert", "aliases": ("deep desert", "deepdesert", "dd", "the deep")},
    {"key": "dd-pvp", "name": "Deep Desert", "map": "deep-desert", "inst": "pvp",
     "partition": None, "dim": 1, "pod": None,
     "pvp": True, "closed": False, "tracked": True, "mode": "PvP",
     "label": "Deep Desert", "aliases": ()},
)

_FIELDS = ("key", "name", "map", "inst", "partition", "dim", "pod", "pvp",
           "closed", "tracked", "mode", "label", "aliases")
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_POD_RE = re.compile(r"^-[a-z0-9-]{1,63}$")


def _text(row, field, limit, where):
    value = row.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("%s: %s must be a non-empty string of at most %d chars" % (where, field, limit))
    return value


def _int_or_none(row, field, low, high, where):
    value = row.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError("%s: %s must be null or an integer %d..%d" % (where, field, low, high))
    return value


def _bool(row, field, default, where):
    value = row.get(field, default)
    if not isinstance(value, bool):
        raise ValueError("%s: %s must be true or false" % (where, field))
    return value


def _instance(row, index):
    where = "instances[%d]" % index
    if not isinstance(row, dict):
        raise ValueError("%s: must be an object" % where)
    unknown = sorted(set(row) - set(_FIELDS))
    if unknown:
        raise ValueError("%s: unknown field(s) %s" % (where, ", ".join(unknown)))
    key = _text(row, "key", 32, where)
    if not _KEY_RE.match(key):
        raise ValueError("%s: key %r must be a-z, 0-9 and -" % (where, key))
    where = "instance %r" % key
    name = _text(row, "name", 64, where)
    map_key = _text(row, "map", 32, where)
    if not _KEY_RE.match(map_key):
        raise ValueError("%s: map %r must be a-z, 0-9 and -" % (where, map_key))
    inst = row.get("inst", key)
    if not isinstance(inst, str) or not _KEY_RE.match(inst):
        raise ValueError("%s: inst must be a-z, 0-9 and -" % where)
    pod = row.get("pod")
    if pod is not None and (not isinstance(pod, str) or not _POD_RE.match(pod)):
        raise ValueError("%s: pod must be null or a suffix like \"-sg-survival-1-pod-33\"" % where)
    pvp = _bool(row, "pvp", False, where)
    mode = row.get("mode", "PvP" if pvp else "PvE")
    if not isinstance(mode, str) or not mode.strip() or len(mode) > 32:
        raise ValueError("%s: mode must be a non-empty string of at most 32 chars" % where)
    label = row.get("label", name)
    if not isinstance(label, str) or not label.strip() or len(label) > 64:
        raise ValueError("%s: label must be a non-empty string of at most 64 chars" % where)
    aliases = row.get("aliases", [])
    if not isinstance(aliases, (list, tuple)) or not all(
            isinstance(a, str) and a.strip() and a == a.lower() and len(a) <= 32 for a in aliases):
        raise ValueError("%s: aliases must be a list of lower-case strings" % where)
    return {
        "key": key, "name": name, "map": map_key, "inst": inst,
        "partition": _int_or_none(row, "partition", 0, 9999, where),
        "dim": _int_or_none(row, "dim", 0, 99, where),
        "pod": pod, "pvp": pvp,
        "closed": _bool(row, "closed", False, where),
        "tracked": _bool(row, "tracked", True, where),
        "mode": mode, "label": label, "aliases": tuple(aliases),
    }


def validate(data):
    """The normalised instance list, or ValueError naming the first problem."""
    if not isinstance(data, dict) or not isinstance(data.get("instances"), list):
        raise ValueError('the file must be an object with an "instances" list')
    # Top-level keys starting with "_" are notes for the reader and are ignored.
    unknown = sorted(k for k in data if k != "instances" and not str(k).startswith("_"))
    if unknown:
        raise ValueError("unknown top-level field(s) %s" % ", ".join(unknown))
    rows = [_instance(row, i) for i, row in enumerate(data["instances"])]
    if not any(not r["closed"] for r in rows):
        raise ValueError("at least one instance must be open")
    seen = {}
    for r in rows:
        claims = [("key", r["key"]), ("map/inst", (r["map"], r["inst"]))]
        if r["partition"] is not None:
            claims.append(("partition", r["partition"]))
        if r["dim"] is not None:
            claims.append(("map/dim", (r["map"], r["dim"])))
        if r["pod"] is not None:
            claims.append(("pod", r["pod"]))
        for claim in claims:
            if claim in seen:
                raise ValueError("instances %r and %r share %s %r" % (seen[claim], r["key"], claim[0], claim[1]))
            seen[claim] = r["key"]
    return rows


def defaults():
    return [dict(row) for row in DEFAULTS]


def load(path=None):
    """The configured instances, or DEFAULTS when the file is absent or invalid."""
    path = path or os.environ.get("LASTSIETCH_MAP_INSTANCES_FILE") or DEFAULT_PATH
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return defaults()
    except (OSError, ValueError) as exc:
        logger.error("map instances: cannot read %s (%s); using built-in defaults", path, exc)
        return defaults()
    try:
        return validate(data)
    except ValueError as exc:
        logger.error("map instances: %s is invalid (%s); using built-in defaults", path, exc)
        return defaults()


_cache = []


def instances():
    """load(), read once per process. A changed file needs a restart."""
    if not _cache:
        _cache.append(load())
    return _cache[0]


def open_instances(map_key=None):
    return [r for r in instances() if not r["closed"] and (map_key is None or r["map"] == map_key)]


def sietches(include_closed=False):
    """Partitioned instances (the sietches), in config order."""
    return [r for r in instances() if r["partition"] is not None and (include_closed or not r["closed"])]


def by_key(key):
    for r in instances():
        if r["key"] == key:
            return r
    return None


def mode_phrase(mode):
    """A mode as it reads mid-sentence: 'Full PvP' -> 'full PvP'; 'PvE' and 'PvP' stay."""
    return mode if mode[:2] == "Pv" else mode[:1].lower() + mode[1:]


def untracked_partitions():
    return frozenset(r["partition"] for r in instances() if not r["tracked"] and r["partition"] is not None)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--check":
        sys.exit("usage: map_instances.py --check FILE")
    with open(sys.argv[2], encoding="utf-8") as fh:
        try:
            checked = validate(json.load(fh))
        except ValueError as exc:
            sys.exit("INVALID: %s" % exc)
    for r in checked:
        print("%-12s %-14s map=%s inst=%s partition=%s dim=%s%s%s" % (
            r["key"], r["name"], r["map"], r["inst"], r["partition"], r["dim"],
            " closed" if r["closed"] else "", " untracked" if not r["tracked"] else ""))
