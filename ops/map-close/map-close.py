#!/usr/bin/env python3
# Map close: render the move / revert / overlap SQL for closing one map instance into another, from the
# map-instance config (LASTSIETCH_MAP_INSTANCES_FILE, schema in admin-backend/map_instances.py). Runs on the operator box
# next to the *.sql.in templates; map-close.sh and inventory.sh call it.
#
#   map-close.py resolve --from KEY --to KEY   "<src part> <src dim> <dst part> <dst dim> <record prefix> <actors.map>"
#   map-close.py render move|revert|overlap|overlap-loose --from KEY --to KEY
#
# Both instances must be on the same map with a partition and a dim (instances of one map share coordinates, which
# is what makes the move a pure re-key). The file is required: unlike the long-running watchers there is no
# default to fall back to, so a missing or broken file refuses rather than guessing which world to rewrite.
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = ("move", "revert", "overlap", "overlap-loose")
# Engine names per map_model key: dune.world_partition.map and dune.actors.map. Fixed by the game, not the server.
ENGINE_MAPS = {
    "hagga": ("Survival_1", "HaggaBasin"),
    "deep-desert": ("DeepDesert_1", "DeepDesert"),
}
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
NAME_RE = re.compile(r"^[A-Za-z0-9 ._-]{1,64}$")


def fail(msg):
    sys.exit("REFUSE: " + msg)


def read_instances(path):
    try:
        with open(path) as fh:
            rows = json.load(fh)["instances"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        fail("cannot read the map-instance config %s (%s)" % (path, exc))
    if not isinstance(rows, list):
        fail("%s: instances must be a list" % path)
    return rows


def instance(rows, key):
    found = [r for r in rows if isinstance(r, dict) and r.get("key") == key]
    if len(found) != 1:
        fail("instance %r: %d entries in the config, need exactly 1" % (key, len(found)))
    r = found[0]
    part, dim, map_key, name = r.get("partition"), r.get("dim"), r.get("map"), r.get("name")
    if type(part) is not int or not 0 <= part <= 9999 or type(dim) is not int or not 0 <= dim <= 99:
        fail("instance %r needs an integer partition and dim" % key)
    if map_key not in ENGINE_MAPS:
        fail("instance %r: map %r has no engine names here (known: %s)" % (key, map_key, ", ".join(ENGINE_MAPS)))
    if not isinstance(name, str) or not NAME_RE.match(name):
        fail("instance %r: name must be 1-64 plain characters" % key)
    return r


def resolve(path, src_key, dst_key):
    for key in (src_key, dst_key):
        if not KEY_RE.match(key or ""):
            fail("instance key %r must be a-z, 0-9 and -" % key)
    if src_key == dst_key:
        fail("--from and --to are the same instance")
    rows = read_instances(path)
    src, dst = instance(rows, src_key), instance(rows, dst_key)
    if src["map"] != dst["map"]:
        fail("%s is on %s and %s on %s; a close moves within one map" % (src_key, src["map"], dst_key, dst["map"]))
    if src["partition"] == dst["partition"] or src["dim"] == dst["dim"]:
        fail("%s and %s share a partition or a dim" % (src_key, dst_key))
    part_map, world_map = ENGINE_MAPS[src["map"]]
    return {
        "SRC_PART": str(src["partition"]), "SRC_DIM": str(src["dim"]), "SRC_NAME": src["name"],
        "DST_PART": str(dst["partition"]), "DST_DIM": str(dst["dim"]), "DST_NAME": dst["name"],
        "PART_MAP": part_map, "WORLD_MAP": world_map,
        # The record tables the move writes and the revert reads: one set per closed instance.
        "REC": "zz_map_close_" + src_key.replace("-", "_"),
    }


def render(template, values):
    with open(os.path.join(HERE, template + ".sql.in")) as fh:
        sql = fh.read()
    for token, value in values.items():
        sql = sql.replace("@%s@" % token, value)
    left = re.findall(r"@[A-Z_]+@", sql)
    if left:
        fail("%s.sql.in has unfilled tokens: %s" % (template, ", ".join(sorted(set(left)))))
    return sql


def main(argv=None):
    ap = argparse.ArgumentParser(description="Render map-close SQL from the map-instance config.")
    ap.add_argument("command", choices=["resolve", "render"])
    ap.add_argument("template", nargs="?", choices=TEMPLATES)
    ap.add_argument("--from", dest="src", required=True)
    ap.add_argument("--to", dest="dst", required=True)
    args = ap.parse_args(argv)
    path = os.environ.get("LASTSIETCH_MAP_INSTANCES_FILE", "/etc/lastsietch/map-instances.json")
    values = resolve(path, args.src, args.dst)
    if args.command == "resolve":
        print(values["SRC_PART"], values["SRC_DIM"], values["DST_PART"], values["DST_DIM"], values["REC"], values["WORLD_MAP"])
    else:
        if not args.template:
            fail("render needs one of: " + ", ".join(TEMPLATES))
        sys.stdout.write(render(args.template, values))


if __name__ == "__main__":
    main()
