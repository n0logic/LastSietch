#!/usr/bin/env python3
"""The values of record live in two files: values-of-record.json and, optionally, the
operator-local values-of-record.private.json. Config watch reads one manifest
(/etc/lastsietch/config-values-of-record.json), which is the two merged.

The private file has the same shape ({"files": {<file>: {<section>: {...}}}}) plus a
"prefixes" list: a section whose name starts with one of them belongs in the private
file. A section present in both files is an error, so neither can silently override
the other.

  ./merge-record.py > merged.json              print the merged manifest
  dune-config-hash.py --print-manifest | ./merge-record.py --split
                                                write a full manifest back as the two files
"""
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
PUBLIC = HERE / "values-of-record.json"
PRIVATE = HERE / "values-of-record.private.json"


def _write(path, record):
    path.write_text(json.dumps(record, indent=2) + "\n")


def merged():
    record = json.loads(PUBLIC.read_text())
    if not PRIVATE.exists():
        return record
    extra = json.loads(PRIVATE.read_text())
    for name, sections in extra["files"].items():
        target = record["files"].setdefault(name, {})
        clash = sorted(set(target) & set(sections))
        if clash:
            raise SystemExit("section in both records: %s %s" % (name, ", ".join(clash[:5])))
        target.update(sections)
        record["files"][name] = dict(sorted(target.items()))
    return record


def split(full):
    prefixes = tuple(json.loads(PRIVATE.read_text()).get("prefixes", [])) if PRIVATE.exists() else ()
    private = {}
    for name, sections in full["files"].items():
        mine = {k: v for k, v in sections.items() if prefixes and k.startswith(prefixes)}
        if mine:
            private[name] = mine
            full["files"][name] = {k: v for k, v in sections.items() if k not in mine}
    _write(PUBLIC, full)
    if prefixes:
        _write(PRIVATE, {"prefixes": list(prefixes), "files": private})
    print("public sections: %d, private sections: %d" % (
        sum(len(s) for s in full["files"].values()), sum(len(s) for s in private.values())), file=sys.stderr)


if __name__ == "__main__":
    if sys.argv[1:] == ["--split"]:
        split(json.load(sys.stdin))
    elif sys.argv[1:]:
        raise SystemExit("usage: merge-record.py [--split]")
    else:
        sys.stdout.write(json.dumps(merged(), indent=2) + "\n")
