#!/usr/bin/env bash
# test-watchdog.sh -- offline test suite for the BattleGroup auto-recovery watchdog.
# Stubs kubectl + the notifier, so NOTHING touches the live server. Run from anywhere:
#   bash ops/lastsietch-bg-watchdog/test-watchdog.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
WD="$HERE/watchdog.sh"
PAUSE="$HERE/pause.sh"
PASS=0; FAIL=0
TMPROOT=$(mktemp -d)
trap 'rm -rf "$TMPROOT"' EXIT

ok()   { PASS=$((PASS+1)); echo "  ok   - $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL - $1"; echo "         $2"; }
chk()  { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "expected='$3' got='$2'"; fi; }
has()  { case "$2" in *"$3"*) ok "$1";; *) bad "$1" "expected to contain '$3' in: $2";; esac; }
hasnt(){ case "$2" in *"$3"*) bad "$1" "expected NOT to contain '$3'";; *) ok "$1";; esac; }

# --- stub factory -------------------------------------------------------------
# makes a fake kubectl that reports $PHASE and records patch calls to $CALLS
mk_env() {
  local name=$1 phase=$2 patch_rc=${3:-0}
  local d="$TMPROOT/$name"; mkdir -p "$d/bin"
  cat > "$d/bin/kubectl" <<EOF
#!/usr/bin/env bash
if [ "\$1" = "get" ]; then
  [ "$phase" = "__FAIL__" ] && exit 1
  printf '%s' "$phase"; exit 0
fi
if [ "\$1" = "patch" ]; then
  echo "PATCH \$*" >> "$d/calls.txt"; exit $patch_rc
fi
exit 0
EOF
  chmod +x "$d/bin/kubectl"
  cat > "$d/bin/notify" <<EOF
#!/usr/bin/env bash
cat >> "$d/notify.txt"; echo >> "$d/notify.txt"
EOF
  chmod +x "$d/bin/notify"
  echo "$d"
}
run() { # run <dir> [extra env...]
  local d=$1; shift
  env BGW_STATE_DIR="$d/state" BGW_KUBECTL="$d/bin/kubectl" BGW_NOTIFY="$d/bin/notify" "$@" bash "$WD" 2>&1
}
calls()  { cat "$1/calls.txt" 2>/dev/null; }
notes()  { cat "$1/notify.txt" 2>/dev/null; }

echo "== 1. Healthy phase -> no action, no alert =="
d=$(mk_env healthy Healthy)
out=$(run "$d")
has  "logs healthy" "$out" "healthy, nothing to do"
chk  "no patch issued" "$(calls "$d")" ""
chk  "no alert" "$(notes "$d")" ""

echo "== 2. Stopped, first tick -> CONFIRM only, no patch =="
d=$(mk_env confirm Stopped)
out=$(run "$d")
has  "confirming" "$out" "confirming before acting"
chk  "no patch on tick 1" "$(calls "$d")" ""

echo "== 3. Stopped, second tick -> ACTS (patch + alert) =="
out=$(run "$d")
has  "patch issued" "$(calls "$d")" 'patch battlegroup'
has  "patch sets stop=false" "$(calls "$d")" '{"spec":{"stop":false}}'
has  "alerts auto-recovery" "$(notes "$d")" "AUTO-RECOVERY"
has  "alert is plain-language" "$(notes "$d")" "players can then log in"

echo "== 4. Recovery -> RECOVERED alert, state resets =="
d2=$(mk_env recov Healthy)
cp -r "$d/state" "$d2/state" 2>/dev/null   # carry acted=true
out=$(run "$d2")
has "recovered alert" "$(notes "$d2")" "SERVER RECOVERED"
chk "consecutive reset" "$(jq -r '.consecutive' "$d2/state/state.json")" "0"
chk "acted reset" "$(jq -r '.acted' "$d2/state/state.json")" "false"

echo "== 5. PAUSED (intentional stop) -> detects but does NOT act =="
d=$(mk_env paused Stopped)
mkdir -p "$d/state"; echo $(( $(date -u +%s) + 600 )) > "$d/state/pause"
out=$(run "$d"); out2=$(run "$d")   # twice: would have acted by now if not paused
has  "logs paused" "$out2" "PAUSED"
chk  "no patch while paused" "$(calls "$d")" ""
chk  "no alert while paused" "$(notes "$d")" ""

echo "== 6. EXPIRED pause self-heals -> acts again =="
d=$(mk_env expired Stopped)
mkdir -p "$d/state"; echo $(( $(date -u +%s) - 60 )) > "$d/state/pause"
run "$d" >/dev/null; run "$d" >/dev/null
has  "patched after pause expiry" "$(calls "$d")" 'patch battlegroup'
[ -f "$d/state/pause" ] && bad "expired pause removed" "still present" || ok "expired pause removed"

echo "== 7. kubectl read FAILS -> never treated as Stopped =="
d=$(mk_env apifail __FAIL__)
out=$(run "$d"); out=$(run "$d")
has  "logs unreadable" "$out" "phase unreadable"
chk  "no patch on API failure" "$(calls "$d")" ""
chk  "no alert on API failure" "$(notes "$d")" ""

echo "== 8. Rate limit -> stops after MAX_ATTEMPTS, alerts once =="
d=$(mk_env ratelimit Stopped)
for i in 1 2 3 4 5 6 7 8 9 10; do run "$d" BGW_MAX_ATTEMPTS=3 >/dev/null; done
n=$(grep -c "PATCH" "$d/calls.txt" 2>/dev/null || echo 0)
chk  "patches capped at 3" "$n" "3"
n_ex=$(grep -c "MANUAL ACTION NEEDED" "$d/notify.txt" 2>/dev/null || echo 0)
chk  "exhausted alert sent exactly once" "$n_ex" "1"

echo "== 9. Rate-limit window prunes old attempts -> acts again later =="
d=$(mk_env prune Stopped)
for i in 1 2 3 4 5 6; do run "$d" BGW_MAX_ATTEMPTS=2 >/dev/null; done
n1=$(grep -c "PATCH" "$d/calls.txt")
# jump 7h into the future: old attempts fall out of the 6h window
run "$d" BGW_MAX_ATTEMPTS=2 BGW_NOW=$(( $(date -u +%s) + 25200 )) >/dev/null
run "$d" BGW_MAX_ATTEMPTS=2 BGW_NOW=$(( $(date -u +%s) + 25200 )) >/dev/null
n2=$(grep -c "PATCH" "$d/calls.txt")
if [ "$n2" -gt "$n1" ]; then ok "acts again after window expiry ($n1 -> $n2)"; else bad "window prune" "no new attempt after 7h ($n1 -> $n2)"; fi

echo "== 10. patch FAILS -> alerts failure, does not claim success =="
d=$(mk_env patchfail Stopped 1)
run "$d" >/dev/null; run "$d" >/dev/null
has   "failure alert" "$(notes "$d")" "AUTO-RECOVERY FAILED"
hasnt "no success claim" "$(notes "$d")" "🛠️ **AUTO-RECOVERY - Dune server was STOPPED"

echo "== 11. non-Stopped phases never trigger (Reconciling/Starting/Stopping) =="
for ph in Reconciling Starting Stopping Healthy; do
  d=$(mk_env "ph$ph" "$ph")
  run "$d" >/dev/null; run "$d" >/dev/null
  chk "no patch on phase=$ph" "$(calls "$d")" ""
done

echo "== 12. pause.sh on/off/status =="
export BGW_STATE_DIR="$TMPROOT/pausecli"
has "status inactive" "$(bash "$PAUSE" status)" "active"
has "on arms"        "$(bash "$PAUSE" on 5)"    "PAUSED for 5 min"
has "status paused"  "$(bash "$PAUSE" status)"  "PAUSED"
has "off resumes"    "$(bash "$PAUSE" off)"     "RESUMED"
has "status active"  "$(bash "$PAUSE" status)"  "active"
has "cap enforced"   "$(bash "$PAUSE" on 99999 2>&1)" "capping"
bash "$PAUSE" off >/dev/null
unset BGW_STATE_DIR

echo
echo "================ RESULT: $PASS passed, $FAIL failed ================"
[ "$FAIL" -eq 0 ] || exit 1
