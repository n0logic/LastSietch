#!/usr/bin/env python3
# Build the compare-and-swap JSON patch for per-map rates (docs/21-custom-settings-and-rates.md).
#   permap-patch.py <bg-before.json> > patch.json
# Tests uid + the full spec, then replaces spec with the per-map args added. Prints a summary to stderr.
import copy, json, sys

GA = "-ini:ServerCustomSettings:[/Script/DuneSandbox.UserServerCustomSettings]:GatheringAmount=%s"
# Amtal 33 follows poll AM8 (1.5 base, 3.75x in the open); DD PvP 31 stays 2.5 (owner 2026-10-07)
# Kulon 32 (1.75) left with the map on 2026-10-28; a podSpec 32 now fails loudly on the lookup
HAGGA = {1: "1.5", 33: "1.5"}
DD = {8: "1.5", 31: "2.5"}
INSTANCE = "1.5"
SKIP_SETS = {"Survival_1", "DeepDesert_1", "Overmap"}
AMTAL_DROP = ("Dune.GlobalMiningOutputMultiplier=", "Dune.GlobalVehicleMiningOutputMultiplier=")

snap = json.load(open(sys.argv[1]))
before = snap["spec"]
spec = copy.deepcopy(before)
sets = spec["serverGroup"]["template"]["spec"]["sets"]
log = []
for s in sets:
    m = s["map"]
    if m == "Survival_1":
        for ps in s["podSpecs"]:
            idx = ps["index"]
            args = [a for a in ps.get("arguments", []) if "GatheringAmount=" not in a]
            if idx == 33:
                args = [a for a in args if not any(k in a for k in AMTAL_DROP)]
            args.append(GA % HAGGA[idx])
            ps["arguments"] = args
            log.append("Survival_1 pod %d: %d args, GatheringAmount=%s" % (idx, len(args), HAGGA[idx]))
    elif m == "DeepDesert_1":
        assert not s.get("podSpecs"), "DeepDesert_1 already has podSpecs; review by hand"
        s["podSpecs"] = [{"index": i, "arguments": [GA % v]} for i, v in sorted(DD.items())]
        log.append("DeepDesert_1 podSpecs %s" % DD)
    elif m not in SKIP_SETS:
        args = [a for a in s.get("arguments", []) if "GatheringAmount=" not in a]
        args.append(GA % INSTANCE)
        s["arguments"] = args
        log.append("set %s: %d args" % (m, len(args)))
sys.stderr.write("\n".join(log) + "\n")
json.dump([{"op": "test", "path": "/metadata/uid", "value": snap["metadata"]["uid"]},
           {"op": "test", "path": "/spec", "value": before},
           {"op": "replace", "path": "/spec", "value": spec}], sys.stdout)
