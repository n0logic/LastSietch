#!/usr/bin/env bash
# Map close: move everything on one map instance to another on the same map, or undo it. Runs from the operator box
# next to map-close.py and the *.sql.in templates. Proven on a live map close in October 2026.
# The procedure it implements is docs/08-updates.md, "Closing a map instance".
#
#   overlap        --from KEY --to KEY   read-only: base and loose-actor overlap scans (run before choosing holds)
#   print          --from KEY --to KEY   show the exact rendered SQL and variables, touch nothing
#   dry-run        --from KEY --to KEY   run the whole move and ROLLBACK (guards, counts, overlap gate, read-back)
#   apply          --from KEY --to KEY --i-have-owner-approval   run the move and COMMIT
#   revert-dry-run --from KEY --to KEY   run the revert and ROLLBACK
#   revert         --from KEY --to KEY --i-have-owner-approval   run the revert and COMMIT
#
# KEY is a map-instance "key" in LASTSIETCH_MAP_INSTANCES_FILE (required here; partitions, dims and names come from it).
# --target live (default) | scratch:<docker container>   (scratch = a throwaway postgres)
# Live needs LASTSIETCH_BG_NAMESPACE and LASTSIETCH_BG_NAME (the BattleGroup) and refuses anything but print and overlap unless
# the BattleGroup is stopped with no game pods: even a rolled-back run takes row locks on every moving actor row
# that a running server of that map would block on while saving.
#
# Variables (env): EXPECT_ACTORS (required for dry-run/apply; from inventory.sh "move_set" line),
# HOLD_TOTEMS (default 0 = none), RECOVERY_VEHICLES (default 0 = none), PAWN_OVERRIDES (default {}).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
MODE="${1:-}"; shift || true
TARGET=live; APPROVED=""; SRC=""; DST=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --from) SRC="$2"; shift 2;;
    --to) DST="$2"; shift 2;;
    --target) TARGET="$2"; shift 2;;
    --i-have-owner-approval) APPROVED=1; shift;;
    *) echo "unknown argument: $1" >&2; exit 2;;
  esac
done
case "$MODE" in
  overlap) TEMPLATES=(overlap overlap-loose); END=ROLLBACK;;
  print|dry-run) TEMPLATES=(move); END=ROLLBACK;;
  apply) TEMPLATES=(move); END=COMMIT;;
  revert-dry-run) TEMPLATES=(revert); END=ROLLBACK;;
  revert) TEMPLATES=(revert); END=COMMIT;;
  *) echo "usage: $0 overlap|print|dry-run|apply|revert-dry-run|revert --from KEY --to KEY [--target live|scratch:NAME] [--i-have-owner-approval]" >&2; exit 2;;
esac
[[ -n "$SRC" && -n "$DST" ]] || { echo "REFUSE: --from and --to name the closing and destination instances" >&2; exit 2; }
[[ "$END" == ROLLBACK || -n "$APPROVED" ]] || { echo "REFUSE: $MODE commits; it needs --i-have-owner-approval" >&2; exit 2; }

HOLD_TOTEMS=${HOLD_TOTEMS:-0}
RECOVERY_VEHICLES=${RECOVERY_VEHICLES:-0}
PAWN_OVERRIDES=${PAWN_OVERRIDES:-'{}'}
EXPECT_ACTORS=${EXPECT_ACTORS:-}
[[ "$HOLD_TOTEMS" =~ ^[0-9]+(,[0-9]+)*$ && "$RECOVERY_VEHICLES" =~ ^[0-9]+(,[0-9]+)*$ ]] || { echo "REFUSE: HOLD_TOTEMS / RECOVERY_VEHICLES must be comma lists of ids" >&2; exit 2; }
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert isinstance(d, dict) and all(k.isdigit() and len(v) == 3 for k, v in d.items())' "$PAWN_OVERRIDES" \
  || { echo "REFUSE: PAWN_OVERRIDES must be {\"<pawn id>\": [x, y, z]}" >&2; exit 2; }
if [[ "$MODE" == dry-run || "$MODE" == apply ]]; then
  [[ "$EXPECT_ACTORS" =~ ^[0-9]+$ ]] || { echo "REFUSE: set EXPECT_ACTORS from the post-stop inventory.sh run" >&2; exit 2; }
fi
SQL=$(mktemp); trap 'rm -f "$SQL"' EXIT
for t in "${TEMPLATES[@]}"; do
  if [[ "$t" == overlap* ]]; then
    { echo "BEGIN READ ONLY;"; python3 "$HERE/map-close.py" render "$t" --from "$SRC" --to "$DST"; echo "ROLLBACK;"; } >> "$SQL"
  else
    python3 "$HERE/map-close.py" render "$t" --from "$SRC" --to "$DST" >> "$SQL"
  fi
done
VARS=(-v "expect_actors=${EXPECT_ACTORS:-0}" -v "hold_totems=$HOLD_TOTEMS" -v "recovery_vehicles=$RECOVERY_VEHICLES"
      -v "pawn_overrides=$PAWN_OVERRIDES" -v "end=$END")

echo "mode=$MODE from=$SRC to=$DST target=$TARGET sha256=$(sha256sum "$SQL" | cut -c1-16) end=$END"
echo "expect_actors=${EXPECT_ACTORS:-?} hold_totems=$HOLD_TOTEMS recovery_vehicles=$RECOVERY_VEHICLES pawn_overrides=$PAWN_OVERRIDES"
if [[ "$MODE" == print ]]; then cat "$SQL"; exit 0; fi

if [[ "$TARGET" == live ]]; then
  NS=${LASTSIETCH_BG_NAMESPACE:?set LASTSIETCH_BG_NAMESPACE to the BattleGroup namespace}
  BG=${LASTSIETCH_BG_NAME:?set LASTSIETCH_BG_NAME to the BattleGroup name}
  if [[ "$MODE" != overlap ]]; then
    state=$(ssh <game-host> "sudo kubectl get battlegroup $BG -n $NS -o jsonpath='{.spec.stop} {.status.phase}'; echo -n ' '; sudo kubectl get pods -n $NS --no-headers 2>/dev/null | grep -c -- '-pod-' || true")
    read -r stop phase pods <<< "$state"
    echo "battlegroup stop=$stop phase=$phase game_pods=$pods"
    [[ "$stop" == true && "$pods" == 0 ]] || { echo "REFUSE: live runs need the BattleGroup stopped and 0 game pods" >&2; exit 3; }
  fi
  ssh <game-host> "sudo /root/dq.sh -X -q -v ON_ERROR_STOP=1 -P pager=off $(printf '%q ' "${VARS[@]}")" < "$SQL"
elif [[ "$TARGET" == scratch:* ]]; then
  docker exec -i "${TARGET#scratch:}" psql -U postgres -d dune -X -q -v ON_ERROR_STOP=1 -P pager=off "${VARS[@]}" < "$SQL"
else
  echo "unknown target: $TARGET" >&2; exit 2
fi
