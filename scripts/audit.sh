#!/bin/bash
# audit.sh - verify your canonical config survived a Funcom image update or full reboot.
# Edit the NS / BG variables at the top to match your deployment.

set -uo pipefail

# === Edit these for your deployment ===
NS="${NS:-funcom-seabass-sh-<your-hostid>-<random>}"
BG="${BG:-sh-<your-hostid>-<random>}"
EXPECTED_TITLE="${EXPECTED_TITLE:-<your-server-title>}"
EXPECTED_SIETCH="${EXPECTED_SIETCH:-<your-sietch-name>}"
EXPECTED_PVP_PARTITION="${EXPECTED_PVP_PARTITION:-8}"
EXPECTED_PVE_PARTITION="${EXPECTED_PVE_PARTITION:-31}"
EXPECTED_DD_INDEX="${EXPECTED_DD_INDEX:-7}"

# === Don't edit below here unless you know your CR is structured differently ===

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'

pass() { printf "${GREEN}✓${NC} %s\n" "$1"; }
fail() { printf "${RED}✗${NC} %s\n" "$1"; ERRORS=$((${ERRORS:-0} + 1)); }
warn() { printf "${YELLOW}!${NC} %s\n" "$1"; }
ERRORS=0

echo "=== Dune Server Canonical Config Audit ==="
echo "Namespace: $NS"
echo "BG name:   $BG"
echo

# 1. BG exists and is Healthy
PHASE=$(sudo kubectl get igwbg -n "$NS" "$BG" -o jsonpath='{.status.phase}' 2>/dev/null)
if [ -z "$PHASE" ]; then
  fail "BG resource not found - check NS and BG variables"
  exit 1
elif [ "$PHASE" != "Healthy" ]; then
  warn "BG phase is '$PHASE' (expected Healthy) - audit continues but server may not be online"
else
  pass "BG phase = Healthy"
fi

# 2. Title check
ACTUAL_TITLE=$(sudo kubectl get igwbg -n "$NS" "$BG" -o jsonpath='{.spec.title}' 2>/dev/null)
[ "$ACTUAL_TITLE" = "$EXPECTED_TITLE" ] && pass "World title = '$ACTUAL_TITLE'" || fail "World title is '$ACTUAL_TITLE', expected '$EXPECTED_TITLE'"

# 3. Dual DD worldPartitions
DD_PART_COUNT=$(sudo kubectl get igwbg -n "$NS" "$BG" -o json | jq ".spec.database.template.spec.deployment.spec.worldPartitions[$EXPECTED_DD_INDEX].partitions | length")
if [ "$DD_PART_COUNT" = "2" ]; then
  pass "DeepDesert_1 has 2 worldPartitions (dual-flavor)"
else
  fail "DeepDesert_1 has $DD_PART_COUNT worldPartitions, expected 2"
fi

# 4. DD ServerSet has both partitions
DD_SS_PARTS=$(sudo kubectl get igwbg -n "$NS" "$BG" -o json | jq -c ".spec.serverGroup.template.spec.sets[$EXPECTED_DD_INDEX].partitions")
EXPECTED_PARTS_JSON="[$EXPECTED_PVP_PARTITION,$EXPECTED_PVE_PARTITION]"
if [ "$DD_SS_PARTS" = "$EXPECTED_PARTS_JSON" ]; then
  pass "DD ServerSet partitions = $DD_SS_PARTS"
else
  fail "DD ServerSet partitions = $DD_SS_PARTS, expected $EXPECTED_PARTS_JSON"
fi

# 5. DD always-on
DD_SCALING=$(sudo kubectl get igwbg -n "$NS" "$BG" -o jsonpath="{.spec.serverGroup.template.spec.sets[$EXPECTED_DD_INDEX].dedicatedScaling}")
[ "$DD_SCALING" = "false" ] && pass "DD dedicatedScaling=false (always-on)" || fail "DD dedicatedScaling=$DD_SCALING (expected false for dual-flavor)"

DD_REPLICAS=$(sudo kubectl get igwbg -n "$NS" "$BG" -o jsonpath="{.spec.serverGroup.template.spec.sets[$EXPECTED_DD_INDEX].replicas}")
[ "$DD_REPLICAS" = "2" ] && pass "DD replicas=2" || fail "DD replicas=$DD_REPLICAS (expected 2)"

# 6. Find the game data PVC mount
PVC=$(sudo ls -d /var/lib/rancher/k3s/storage/pvc-*_${NS}_${BG}-pvc 2>/dev/null | grep -v db-pvc | head -1)
if [ -z "$PVC" ]; then
  warn "Could not find game data PVC mount - skipping UserSettings checks"
else
  USERSETTINGS="$PVC/Saved/UserSettings"

  # 7. UserGame.ini PvP partition
  if sudo grep -q "^+m_PvpEnabledPartitions=$EXPECTED_PVP_PARTITION" "$USERSETTINGS/UserGame.ini" 2>/dev/null; then
    pass "UserGame.ini: +m_PvpEnabledPartitions=$EXPECTED_PVP_PARTITION"
  else
    fail "UserGame.ini: missing +m_PvpEnabledPartitions=$EXPECTED_PVP_PARTITION"
  fi

  # 8. UserEngine.ini sietch display name
  if sudo grep -q "^Bgd.ServerDisplayName=\"$EXPECTED_SIETCH\"" "$USERSETTINGS/UserEngine.ini" 2>/dev/null; then
    pass "UserEngine.ini: Bgd.ServerDisplayName=\"$EXPECTED_SIETCH\""
  else
    fail "UserEngine.ini: missing Bgd.ServerDisplayName=\"$EXPECTED_SIETCH\""
  fi
fi

# 9. Game pods running
RUNNING=$(sudo kubectl get pods -n "$NS" --no-headers 2>/dev/null | grep -cE "sg-.*Running")
EXPECTED_SG_PODS=6   # Survival_1 + Overmap + SH_Arrakeen + SH_HarkoVillage + 2x DD
if [ "$RUNNING" -ge "$EXPECTED_SG_PODS" ]; then
  pass "$RUNNING game pods running (>=$EXPECTED_SG_PODS expected)"
else
  warn "$RUNNING game pods running, expected at least $EXPECTED_SG_PODS"
fi

# 10. Director publishing Declare with non-empty UpDeclarations
BGD=$(sudo kubectl get pods -n "$NS" -l role=igw-battlegroup-director -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -n "$BGD" ]; then
  RECENT_DECL=$(sudo kubectl logs -n "$NS" "$BGD" --tail=200 2>/dev/null | grep "DeclareBattlegroupUpdates" | tail -1)
  if [ -z "$RECENT_DECL" ]; then
    warn "No DeclareBattlegroupUpdates calls in recent BGD logs - server may not yet be browser-visible"
  elif echo "$RECENT_DECL" | grep -q '"UpDeclarationsByPartitionId":{}'; then
    fail "BGD broadcasting Declare with EMPTY UpDecls - server will be invisible in browser"
  else
    pass "BGD broadcasting Declare with populated UpDecls"

    # Verify display name in Declare payload
    DECL_NAME=$(echo "$RECENT_DECL" | grep -oE '"DisplayName":"[^"]*"' | head -1)
    if echo "$DECL_NAME" | grep -q "$EXPECTED_SIETCH"; then
      pass "Declare payload DisplayName matches '$EXPECTED_SIETCH'"
    else
      warn "Declare DisplayName is $DECL_NAME (expected reference to '$EXPECTED_SIETCH')"
    fi
  fi
else
  warn "BGD pod not found"
fi

echo
if [ "$ERRORS" -eq 0 ]; then
  printf "${GREEN}=== All checks passed ===${NC}\n"
  exit 0
else
  printf "${RED}=== $ERRORS check(s) failed ===${NC}\n"
  exit 1
fi
