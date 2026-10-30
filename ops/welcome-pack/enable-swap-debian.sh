#!/usr/bin/env bash
# Swap Memory enablement for lastsietch-dune — DEBIAN/systemd adaptation of Funcom's
# Alpine-targeted scripts/setup/experimental_swap.sh.
#
# WHY: the stock script uses rc-update/rc-service/k3s-killall and OVERWRITES
# /etc/rancher/k3s/config.yaml — on lastsietch-dune that destroys node-ip/external-ip/
# scheduler config. This version uses systemd and MERGES config.yaml.
#
# RUN ONLY IN A MAINTENANCE WINDOW — it restarts k3s; every pod bounces.
# Pre-req: server should be empty (announce + confirm 0 players online).
set -euo pipefail

BG=${LASTSIETCH_GAME_BATTLEGROUP:?set LASTSIETCH_GAME_BATTLEGROUP}
NS=funcom-seabass-$BG
SWAP_GIB=${SWAP_GIB:-64}
TEMPLATE=/home/dune/.dune/download/scripts/setup/templates/kubelet-config.yaml
K3S_CONF=/etc/rancher/k3s/config.yaml
KUBELET_CONF=/etc/rancher/k3s/kubelet-config.yaml
TS=$(date -u +%Y%m%d-%H%M%S)
KC="kubectl"

[[ $EUID -eq 0 ]] || { echo "must run as root"; exit 1; }

echo "===== 1/5  swap file (${SWAP_GIB} GiB) ====="
if swapon --show=NAME --noheadings 2>/dev/null | grep -qx /swapfile; then
    echo "/swapfile already active — skipping"
else
    fallocate -l "${SWAP_GIB}G" /swapfile || \
        dd if=/dev/zero of=/swapfile bs=1M count=$((SWAP_GIB*1024)) status=progress
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    grep -qF '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
swapon --show

echo "===== 2/5  kubelet config ====="
[[ -f "$TEMPLATE" ]] || { echo "template missing: $TEMPLATE"; exit 1; }
cp -v "$TEMPLATE" "$KUBELET_CONF"

echo "===== 3/5  merge k3s config.yaml (backup + append — never overwrite) ====="
cp -v "$K3S_CONF" "${K3S_CONF}.bak-${TS}"
if grep -q 'kubelet-config.yaml' "$K3S_CONF"; then
    echo "config.yaml already references kubelet-config.yaml — skipping merge"
else
    python3 - "$K3S_CONF" <<'PYEOF'
import sys
p = sys.argv[1]
lines = open(p).read().splitlines()
out, injected = [], False
for l in lines:
    out.append(l)
    if l.strip() == 'kubelet-arg:' and not injected:
        out.append('- "config=/etc/rancher/k3s/kubelet-config.yaml"')
        injected = True
if not injected:                       # no existing kubelet-arg: block
    out += ['kubelet-arg:', '- "config=/etc/rancher/k3s/kubelet-config.yaml"']
open(p, 'w').write("\n".join(out) + "\n")
print("merged kubelet-arg config reference")
PYEOF
fi
echo "--- resulting config.yaml ---"; cat "$K3S_CONF"; echo "----------------------------"

echo "===== 4/5  restart k3s with swap support ====="
systemctl stop k3s
[[ -x /usr/local/bin/k3s-killall.sh ]] && /usr/local/bin/k3s-killall.sh || true
sleep 5
systemctl start k3s
# keep the k3s control plane itself out of swap (best-effort, cgroup v2)
echo 0 > /sys/fs/cgroup/system.slice/k3s.service/memory.swap.max 2>/dev/null || \
    echo "(note: could not pin k3s.service memory.swap.max — non-fatal)"
echo "waiting 150s for k3s + operators to come back ..."
sleep 150
$KC get nodes

echo "===== 5/5  rebalance battlegroup memory (low requests so all maps schedule) ====="
# limit = usage cap, request = what the scheduler reserves.
# Low requests => everything schedules; generous limits on populated maps;
# swap absorbs spikes.  Format: {"limits":{"memory":"L"},"requests":{"memory":"R"}}
declare -A res
res["Survival_1"]='{"limits":{"memory":"20Gi"},"requests":{"memory":"5Gi"}}'
res["DeepDesert_1"]='{"limits":{"memory":"16Gi"},"requests":{"memory":"3Gi"}}'
res["Overmap"]='{"limits":{"memory":"6Gi"},"requests":{"memory":"1Gi"}}'
res["SH_Arrakeen"]='{"limits":{"memory":"6Gi"},"requests":{"memory":"1Gi"}}'
res["SH_HarkoVillage"]='{"limits":{"memory":"6Gi"},"requests":{"memory":"1Gi"}}'
res["__DEFAULT__"]='{"limits":{"memory":"3Gi"},"requests":{"memory":"300Mi"}}'

mapfile -t SETS < <($KC get battlegroup -n "$NS" "$BG" -o json \
    | jq -r '.spec.serverGroup.template.spec.sets | to_entries[] | "\(.key)\t\(.value.map)"')
for row in "${SETS[@]}"; do
    idx="${row%%$'\t'*}"; map="${row#*$'\t'}"
    val="${res[$map]:-${res[__DEFAULT__]}}"
    echo "  set[$idx] $map -> $val"
    $KC patch battlegroup -n "$NS" "$BG" --type=json \
        -p="[{\"op\":\"replace\",\"path\":\"/spec/serverGroup/template/spec/sets/${idx}/resources\",\"value\":${val}}]"
done

echo "===== done. watch:  kubectl -n $NS get pods -w ====="
echo "rollback k3s config: cp ${K3S_CONF}.bak-${TS} ${K3S_CONF} && systemctl restart k3s"
