"""Vehicle-package expander for the Dune admin grant system (2026-06-26).

Reads admin-backend/data/vehicle-packages.json (curated Mk6 vehicle parts
bundles) and expands a selection of (package, quantity) into the flat items
array consumed by the `container_items_batch` / `bank_items_batch` grant types
(each element {template_id, stack_size, quality}).

Reused by routers/dune_grant.py (M2 panel wiring) and by the M1 CLI for testing:
    python3 vehicle_packages.py list
    python3 vehicle_packages.py expand buggy_mk6:5 scout_ornithopter_mk6:1
"""
import json
import os

_DATA = os.path.join(os.path.dirname(__file__), "data", "vehicle-packages.json")


def load_packages() -> dict:
    with open(_DATA) as fh:
        return json.load(fh)["packages"]


def expand_selection(selection: list[tuple[str, int]]) -> list[dict]:
    """selection = [(package_key, quantity), ...] -> flat items array.

    Each package contributes its items * quantity. A package item with count>1
    becomes `count` separate slots (vehicle parts are stack_size=1 each), except
    explicitly stacked entries (e.g. RocketAmmo stack_size=250) which stay one
    slot per unit-of-count. Raises KeyError on an unknown package, ValueError on
    a non-positive quantity.
    """
    packages = load_packages()
    items: list[dict] = []
    for key, qty in selection:
        if key not in packages:
            raise KeyError(f"unknown vehicle package: {key}")
        if qty < 1:
            raise ValueError(f"quantity for {key} must be >= 1 (got {qty})")
        for _ in range(qty):
            for it in packages[key]["items"]:
                for _slot in range(it["count"]):
                    items.append({
                        "template_id": it["template_id"],
                        "stack_size": it["stack_size"],
                        "quality": it["quality"],
                    })
    return items


def _parse_cli_selection(argv: list[str]) -> list[tuple[str, int]]:
    sel = []
    for tok in argv:
        if ":" in tok:
            key, q = tok.rsplit(":", 1)
            sel.append((key, int(q)))
        else:
            sel.append((tok, 1))
    return sel


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == "list":
        for k, p in load_packages().items():
            print(f"{k:26} {p['slots']:2} slots  {p['display']}")
    elif len(sys.argv) >= 3 and sys.argv[1] == "expand":
        sel = _parse_cli_selection(sys.argv[2:])
        items = expand_selection(sel)
        print(json.dumps(items))
        # summary to stderr
        from collections import Counter
        c = Counter(i["template_id"] for i in items)
        print(f"\n-- {len(items)} slots from {sel}", file=sys.stderr)
        for t, n in sorted(c.items()):
            print(f"--   {t:42} x{n}", file=sys.stderr)
    else:
        print("usage: vehicle_packages.py list | expand <pkg[:qty]> ...", file=sys.stderr)
        sys.exit(2)
