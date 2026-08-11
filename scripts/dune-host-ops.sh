#!/usr/bin/env bash
# Read-only ops telemetry for the public landing-page panel (lastsietch.com).
# Invoked ONLY via the lastsietch-relay forced-command dispatcher (action: host-ops).
# Emits {bg,players,host,pods} JSON on stdout. No writes, no game-state touch.
# Lifted verbatim from the old dune-status-pull.sh REMOTE block (which used to
# run over the now-dead `lastsietch-dune` SSH loop). Source of truth: this host.
set -e

bg_json() { curl -fsS --max-time 5 http://127.0.0.1:31282/v0/battlegroup 2>/dev/null || echo '{}'; }
players_json() { curl -fsS --max-time 5 http://127.0.0.1:31282/v0/players/online 2>/dev/null || echo '[]'; }

# Count game-server pods grouped by always-on (the 7 partition pods, incl. the 2nd Hagga sietch on survival-1 partition 32) vs on-demand.
NS="${NS:-funcom-seabass-sh-<your-hostid>-<random>}"
ALWAYS_ON_RE='sg-(deepdesert-1|overmap|sh-arrakeen|sh-harkovillage|survival-1)-pod-'
pods_json() {
  /usr/local/bin/kubectl -n "$NS" get pods --no-headers 2>/dev/null | awk '/-sg-.*-pod-/ && $2=="1/1" && $3=="Running" {print $1}' || true
}
all_pods=$(pods_json)
always_on_running=$(echo "$all_pods" | grep -cE "$ALWAYS_ON_RE" || true)
on_demand_running=$(echo "$all_pods" | grep -cvE "$ALWAYS_ON_RE" || true)
# Empty-string guard
[[ -z "$always_on_running" ]] && always_on_running=0
[[ -z "$on_demand_running" ]] && on_demand_running=0
# Subtract empty-line counts
total_running=$(echo "$all_pods" | grep -c . || true)
[[ -z "$total_running" ]] && total_running=0

# Host telemetry
uptime_secs=$(awk '{print int($1)}' /proc/uptime)
load=$(awk '{print $1}' /proc/loadavg)
# CPU usage % over a short sample
read cpu user nice system idle iowait irq softirq steal _ < /proc/stat
total1=$((user+nice+system+idle+iowait+irq+softirq+steal))
idle1=$((idle+iowait))
# Scheduler churn, sampled across the same 1s window as cpu_pct (added 2026-08-04:
# context-switch rate was measured at 30-60x this box's own since-boot average while
# CPU sat idle, which is the signature we need a time series for).
ctxt1=$(awk '/^ctxt /{print $2}' /proc/stat); ctxt1=${ctxt1:-0}
intr1=$(awk '/^intr /{print $2}' /proc/stat); intr1=${intr1:-0}
sleep 1
read cpu user nice system idle iowait irq softirq steal _ < /proc/stat
total2=$((user+nice+system+idle+iowait+irq+softirq+steal))
idle2=$((idle+iowait))
ctxt2=$(awk '/^ctxt /{print $2}' /proc/stat); ctxt2=${ctxt2:-0}
intr2=$(awk '/^intr /{print $2}' /proc/stat); intr2=${intr2:-0}
total_diff=$((total2-total1)); idle_diff=$((idle2-idle1))
cpu_pct=$(awk -v t=$total_diff -v i=$idle_diff 'BEGIN { if (t>0) printf "%.1f", 100.0*(t-i)/t; else print "0.0" }')
ctxt_per_sec=$((ctxt2-ctxt1))
intr_per_sec=$((intr2-intr1))
procs_running=$(awk '/^procs_running/{print $2}' /proc/stat); procs_running=${procs_running:-0}
procs_blocked=$(awk '/^procs_blocked/{print $2}' /proc/stat); procs_blocked=${procs_blocked:-0}

cpu_count=$(grep -c '^processor' /proc/cpuinfo)

# Memory
mem_total_kb=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
mem_avail_kb=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
mem_used_kb=$((mem_total_kb - mem_avail_kb))
mem_pct=$(awk -v u=$mem_used_kb -v t=$mem_total_kb 'BEGIN { if (t>0) printf "%.1f", 100.0*u/t; else print "0.0" }')

# Disk (root)
disk_line=$(df -B1 / | tail -n1)
disk_total=$(echo $disk_line | awk '{print $2}')
disk_used=$(echo $disk_line | awk '{print $3}')
disk_pct=$(awk -v u=$disk_used -v t=$disk_total 'BEGIN { if (t>0) printf "%.1f", 100.0*u/t; else print "0.0" }')

# Build JSON
bg=$(bg_json)
players=$(players_json)

# Compose host JSON
host=$(printf '{"cpu_pct":%s,"cpu_count":%s,"load":%s,"uptime_secs":%s,"mem_used_bytes":%s,"mem_total_bytes":%s,"mem_pct":%s,"disk_used_bytes":%s,"disk_total_bytes":%s,"disk_pct":%s,"ctxt_per_sec":%s,"intr_per_sec":%s,"procs_running":%s,"procs_blocked":%s,"hostname":"%s"}' \
  "$cpu_pct" "$cpu_count" "$load" "$uptime_secs" \
  "$((mem_used_kb*1024))" "$((mem_total_kb*1024))" "$mem_pct" \
  "$disk_used" "$disk_total" "$disk_pct" \
  "$ctxt_per_sec" "$intr_per_sec" "$procs_running" "$procs_blocked" \
  "$(hostname)")

# Pod counts
pods=$(printf '{"always_on_running":%s,"on_demand_running":%s,"total_running":%s,"always_on_expected":7}' \
  "$always_on_running" "$on_demand_running" "$total_running")

printf '{"bg":%s,"players":%s,"host":%s,"pods":%s}' "$bg" "$players" "$host" "$pods"
