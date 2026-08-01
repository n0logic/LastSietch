#!/bin/bash
# apply-canonical.sh - apply the canonical config to a fresh Funcom-wizard-generated BG.
#
# Run this AFTER the wizard completes and BEFORE running `battlegroup start` for the first time.
# Or after a Funcom image update if the audit script flags drift.
#
# Edit the configuration block below for your deployment.

set -euo pipefail

# === Configure these for your deployment ===
NS="${NS:-funcom-seabass-sh-<your-hostid>-<random>}"
BG="${BG:-sh-<your-hostid>-<random>}"

# Your sietch's in-game name (max ~50 chars, no single-quotes or pipe chars)
SIETCH_DISPLAY_NAME="${SIETCH_DISPLAY_NAME:-My Sietch}"

# Dual-Deep-Desert configuration
ENABLE_DUAL_DD="${ENABLE_DUAL_DD:-true}"
DD_PVP_PARTITION_ID="${DD_PVP_PARTITION_ID:-8}"   # default Funcom partition for DeepDesert_1
DD_PVE_PARTITION_ID="${DD_PVE_PARTITION_ID:-31}"  # next free ID after Funcom GA defaults (1-30)
DD_SERVERSET_INDEX="${DD_SERVERSET_INDEX:-7}"     # array index of DeepDesert_1 in spec.serverGroup.template.spec.sets

# Memory tuning (limits, not reservations)
MEM_SURVIVAL="${MEM_SURVIVAL:-24Gi}"
MEM_OVERMAP="${MEM_OVERMAP:-8Gi}"
MEM_DD="${MEM_DD:-20Gi}"
MEM_SIETCH="${MEM_SIETCH:-12Gi}"     # social hubs (SH_Arrakeen, SH_HarkoVillage)

# Promote social hubs to always-on
ENABLE_ALWAYSON_SIETCHES="${ENABLE_ALWAYSON_SIETCHES:-true}"

# Stop BG before applying (recommended for clean reconciliation)
STOP_BG_FIRST="${STOP_BG_FIRST:-true}"

# === End config ===

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'

info()  { printf "${GREEN}==>${NC} %s\n" "$1"; }
warn()  { printf "${YELLOW}!${NC}   %s\n" "$1"; }
fail()  { printf "${RED}ERROR:${NC} %s\n" "$1"; exit 1; }

# Sanity checks
sudo kubectl get igwbg -n "$NS" "$BG" >/dev/null 2>&1 || fail "BG $BG not found in namespace $NS. Edit NS/BG at top of script."

if [ "$STOP_BG_FIRST" = "true" ]; then
  info "Stopping BG (will restart at end)"
  sudo -u dune /home/dune/.dune/bin/battlegroup stop || warn "battlegroup stop returned non-zero (may already be stopped)"
fi

# === BG CR patches ===

info "Patch 1/4: BG title (already set by wizard, but make sure it persists across audits)"
# This is no-op if the wizard set the title from the world name. Skip if you want a different title.

info "Patch 2/4: Memory limits"
sudo kubectl patch igwbg -n "$NS" "$BG" --type=json -p='[
  {"op": "replace", "path": "/spec/serverGroup/template/spec/sets/0/resources/limits/memory", "value": "'"$MEM_SURVIVAL"'"},
  {"op": "replace", "path": "/spec/serverGroup/template/spec/sets/1/resources/limits/memory", "value": "'"$MEM_OVERMAP"'"},
  {"op": "replace", "path": "/spec/serverGroup/template/spec/sets/'$DD_SERVERSET_INDEX'/resources/limits/memory", "value": "'"$MEM_DD"'"}
]'

if [ "$ENABLE_DUAL_DD" = "true" ]; then
  info "Patch 3/4: Dual Deep Desert (PvP partition $DD_PVP_PARTITION_ID + PvE partition $DD_PVE_PARTITION_ID)"

  # Check if the PvE partition is already present (don't add twice)
  EXISTING=$(sudo kubectl get igwbg -n "$NS" "$BG" -o json | \
    jq ".spec.database.template.spec.deployment.spec.worldPartitions[$DD_SERVERSET_INDEX].partitions | length")
  if [ "$EXISTING" -lt 2 ]; then
    sudo kubectl patch igwbg -n "$NS" "$BG" --type=json -p='[{
      "op": "add",
      "path": "/spec/database/template/spec/deployment/spec/worldPartitions/'$DD_SERVERSET_INDEX'/partitions/-",
      "value": {"dimension": 1, "disable": false, "id": '$DD_PVE_PARTITION_ID', "maxX": 1, "maxY": 1, "minX": 0, "minY": 0}
    }]'
  else
    info "  (PvE partition already present, skipping worldPartitions add)"
  fi

  sudo kubectl patch igwbg -n "$NS" "$BG" --type=json -p='[
    {"op": "replace", "path": "/spec/serverGroup/template/spec/sets/'$DD_SERVERSET_INDEX'/partitions", "value": ['"$DD_PVP_PARTITION_ID, $DD_PVE_PARTITION_ID"']},
    {"op": "replace", "path": "/spec/serverGroup/template/spec/sets/'$DD_SERVERSET_INDEX'/replicas", "value": 2},
    {"op": "replace", "path": "/spec/serverGroup/template/spec/sets/'$DD_SERVERSET_INDEX'/dedicatedScaling", "value": false}
  ]'
fi

if [ "$ENABLE_ALWAYSON_SIETCHES" = "true" ]; then
  info "Patch 4/4: Promote SH_Arrakeen + SH_HarkoVillage to always-on"
  # SH_Arrakeen is normally serverSets[2]; SH_HarkoVillage is [3]. Adjust if your wizard output differs.
  sudo kubectl patch igwbg -n "$NS" "$BG" --type=json -p='[
    {"op": "replace", "path": "/spec/serverGroup/template/spec/sets/2/partitions", "value": [3]},
    {"op": "replace", "path": "/spec/serverGroup/template/spec/sets/2/replicas", "value": 1},
    {"op": "replace", "path": "/spec/serverGroup/template/spec/sets/2/dedicatedScaling", "value": false},
    {"op": "replace", "path": "/spec/serverGroup/template/spec/sets/2/resources/limits/memory", "value": "'"$MEM_SIETCH"'"},
    {"op": "replace", "path": "/spec/serverGroup/template/spec/sets/3/partitions", "value": [4]},
    {"op": "replace", "path": "/spec/serverGroup/template/spec/sets/3/replicas", "value": 1},
    {"op": "replace", "path": "/spec/serverGroup/template/spec/sets/3/dedicatedScaling", "value": false},
    {"op": "replace", "path": "/spec/serverGroup/template/spec/sets/3/resources/limits/memory", "value": "'"$MEM_SIETCH"'"}
  ]'
fi

# === User*.ini patches ===

info "Locating game data PVC mount"
PVC_PATH=$(sudo ls -d /var/lib/rancher/k3s/storage/pvc-*_${NS}_${BG}-pvc 2>/dev/null | grep -v db-pvc | head -1)
[ -z "$PVC_PATH" ] && fail "Could not find game data PVC mount under /var/lib/rancher/k3s/storage/"

USERSETTINGS="$PVC_PATH/Saved/UserSettings"
info "UserSettings dir: $USERSETTINGS"

info "UserGame.ini: activate PvP partition $DD_PVP_PARTITION_ID"
if ! sudo grep -q "^+m_PvpEnabledPartitions=$DD_PVP_PARTITION_ID" "$USERSETTINGS/UserGame.ini"; then
  sudo sed -i "/^;+m_PvpEnabledPartitions=1$/a +m_PvpEnabledPartitions=$DD_PVP_PARTITION_ID" "$USERSETTINGS/UserGame.ini"
else
  info "  (already present)"
fi

info "UserEngine.ini: set Bgd.ServerDisplayName=\"$SIETCH_DISPLAY_NAME\""
if ! sudo grep -q "^Bgd.ServerDisplayName=\"$SIETCH_DISPLAY_NAME\"" "$USERSETTINGS/UserEngine.ini"; then
  sudo sed -i "s|^;Bgd.ServerDisplayName=\".*\"$|Bgd.ServerDisplayName=\"$SIETCH_DISPLAY_NAME\"|" "$USERSETTINGS/UserEngine.ini"
else
  info "  (already present)"
fi

if [ "$STOP_BG_FIRST" = "true" ]; then
  info "Starting BG"
  sudo -u dune /home/dune/.dune/bin/battlegroup start
fi

info "Done. Run scripts/audit.sh to verify everything took effect."
