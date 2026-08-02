#!/usr/bin/env bash
# STAGED — apply during a maintenance/update window only (patching the CR rolls
# the affected map pods). Aligns Last Sietch per-map memory limits to Funcom's official
# per-map memory chart (zing5780, build 1973075; see MEMORY-SIZING-AND-
# EQUILIBRIUM-PANEL-2026-06-03.md). Robust to set reordering: resolves each set
# index by map name from the live CR, then JSON-patches resources.
#
# Changes (limit = OOM ceiling; official max in parens):
#   DeepDesert_1  16Gi -> 18Gi  (official max 17 GB) ; request 3Gi -> 4Gi
#   Survival_1    20Gi -> 24Gi  (official max 25-32 GB, formula 0.390852*CCU+11.5594) ; request 5Gi -> 6Gi
# Others already have ample headroom vs official (Overmap 6Gi vs 2.4; smalls 3Gi vs ~1.7).
# The postgres pod is intentionally left unbounded (no OOM-from-limit); monitor node pressure.
set -euo pipefail

NS="${DUNE_NS:-funcom-seabass-sh-<your-hostid>-<random>}"
BG="${DUNE_BG:-sh-<your-hostid>-<random>}"

# map -> "requestGi limitGi"
declare -A WANT=(
  [DeepDesert_1]="4 18"
  [Survival_1]="6 24"
)

CR_JSON=$(sudo kubectl -n "$NS" get igwbg "$BG" -o json)

PATCH="["
first=1
for map in "${!WANT[@]}"; do
  read -r reqGi limGi <<<"${WANT[$map]}"
  idx=$(printf '%s' "$CR_JSON" | python3 -c "
import json,sys
d=json.load(sys.stdin)
sets=d['spec']['serverGroup']['template']['spec']['sets']
print(next(i for i,s in enumerate(sets) if s.get('map')=='$map'))
")
  cur=$(printf '%s' "$CR_JSON" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(json.dumps(d['spec']['serverGroup']['template']['spec']['sets'][$idx].get('resources',{})))
")
  echo "  $map: set index $idx, current resources = $cur -> requests ${reqGi}Gi / limits ${limGi}Gi"
  [ "$first" -eq 1 ] || PATCH+=","
  first=0
  PATCH+="{\"op\":\"replace\",\"path\":\"/spec/serverGroup/template/spec/sets/$idx/resources\",\"value\":{\"limits\":{\"memory\":\"${limGi}Gi\"},\"requests\":{\"memory\":\"${reqGi}Gi\"}}}"
done
PATCH+="]"

if [ "${1:-}" = "--apply" ]; then
  TS=$(date -u +%Y%m%dT%H%M%SZ)
  sudo kubectl -n "$NS" get igwbg "$BG" -o yaml > "/root/jun3/igwbg.pre-memlimits-$TS.yaml"
  echo "CR backed up: /root/jun3/igwbg.pre-memlimits-$TS.yaml"
  echo "$PATCH" | sudo kubectl -n "$NS" patch igwbg "$BG" --type=json -p "$(cat -)"
  echo "Patched. The operator (igw) will roll the DeepDesert_1 + Survival_1 pods. Watch:"
  echo "  sudo kubectl -n $NS get pods -l role=igw-server -w"
else
  echo
  echo "DRY RUN. Patch that WOULD be applied:"
  echo "$PATCH" | python3 -m json.tool
  echo
  echo "Re-run with --apply DURING A WINDOW to patch (rolls DD + Hagga pods)."
fi
