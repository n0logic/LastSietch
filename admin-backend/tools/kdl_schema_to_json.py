#!/usr/bin/env python3
# P8 — da-tweakables config-schema.kdl → cvars-schema.json one-shot converter.
#
# Constrained KDL-1.0 subset (per da-tweakables conventions):
#   - One `category "<id>" label="<label>" { ... }` block.
#   - Inside: one `field "<key>" prop=val prop=val ...` per line.
#   - No children on field nodes, no slashdashes, no multi-line strings.
#   - String values are double-quoted; numbers and bools are bare.
#   - `//` line comments and blank lines outside blocks.
#
# Stdlib only (owner constraint Q2). No new admin-backend dep.

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# category "<id>" label="<label>" {
CAT_RE = re.compile(r'^\s*category\s+"([^"]+)"\s+label="([^"]+)"\s*\{\s*$')
# field "<key>" <props...>
FIELD_RE = re.compile(r'^\s*field\s+"([^"]+)"\s+(.*?)\s*$')
# Close brace alone on a line
CLOSE_RE = re.compile(r'^\s*\}\s*$')
# Property tokenizer: name="quoted" | name=number | name=true|false
# Numbers may be negative, decimal, scientific.
PROP_RE = re.compile(
    r'(\w+)\s*=\s*'
    r'(?:"((?:[^"\\]|\\.)*)"'           # group 2: quoted string (escapes preserved)
    r'|(true|false)'                     # group 3: bool
    r'|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?))'  # group 4: number
)


def parse_props(blob):
    """Tokenize the property tail of a field/category line. Returns dict."""
    out = {}
    for m in PROP_RE.finditer(blob):
        name = m.group(1)
        if m.group(2) is not None:
            out[name] = m.group(2)
        elif m.group(3) is not None:
            out[name] = (m.group(3) == "true")
        else:
            num = m.group(4)
            out[name] = float(num) if ("." in num or "e" in num or "E" in num) else int(num)
    return out


def convert(kdl_text, source_path):
    sha256 = hashlib.sha256(kdl_text.encode("utf-8")).hexdigest()
    categories = []
    current_cat = None
    field_count = 0

    for lineno, raw in enumerate(kdl_text.splitlines(), 1):
        line = raw.rstrip("\r")
        # Strip line comments (but not "//" inside a quoted string — the schema
        # has no such case, so a naive split is safe here).
        if "//" in line:
            # only strip if the // is outside any quotes
            idx = line.find("//")
            in_quote = False
            for i, ch in enumerate(line):
                if ch == '"' and (i == 0 or line[i-1] != "\\"):
                    in_quote = not in_quote
                if i == idx:
                    if not in_quote:
                        line = line[:idx]
                    break
        stripped = line.strip()
        if not stripped:
            continue

        m = CAT_RE.match(line)
        if m:
            if current_cat is not None:
                raise ValueError(f"line {lineno}: nested category not supported")
            current_cat = {"id": m.group(1), "label": m.group(2), "fields": []}
            continue

        if CLOSE_RE.match(line):
            if current_cat is None:
                raise ValueError(f"line {lineno}: unmatched }}")
            categories.append(current_cat)
            current_cat = None
            continue

        m = FIELD_RE.match(line)
        if m:
            if current_cat is None:
                raise ValueError(f"line {lineno}: field outside category")
            key = m.group(1)
            props = parse_props(m.group(2))
            # Normalize the field record — stable key order eases diffing.
            field = {
                "key": key,
                "label": props.get("label", key),
                "desc": props.get("desc", ""),
                "type": props.get("type", "string"),
                "source": props.get("source", "engine"),
                "section": props.get("section"),  # None for engine-source fields
                "default": props.get("default", ""),
                "min": props.get("min"),
                "max": props.get("max"),
                "step": props.get("step"),
                "unit": props.get("unit"),
                "confirmed": bool(props.get("confirmed", False)),
            }
            current_cat["fields"].append(field)
            field_count += 1
            continue

        raise ValueError(f"line {lineno}: unrecognized: {stripped!r}")

    if current_cat is not None:
        raise ValueError("EOF: unclosed category block")

    return {
        "version": 1,
        "source": str(source_path),
        "source_sha256": sha256,
        "source_upstream": "https://gitlab.com/da-tweakables/da-tweakables",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kdl_field_count": field_count,
        "kdl_category_count": len(categories),
        "categories": categories,
    }


def main():
    if len(sys.argv) != 2:
        sys.stderr.write("usage: kdl_schema_to_json.py <config-schema.kdl>\n")
        sys.exit(2)
    path = Path(sys.argv[1])
    text = path.read_text(encoding="utf-8")
    result = convert(text, path.name)
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
