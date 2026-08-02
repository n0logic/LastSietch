#!/usr/bin/env bash
# Launcher for the Last Sietch Dune telemetry logger.
# Resolves the dune Postgres ClusterIP + password + survival pod fresh from k3s
# each start (a battlegroup rebuild changes the sh-... hash and the ClusterIP),
# exports them as env, then execs the logger.
#
# This launcher is PASSIVE: it only runs read-only kubectl/psql verbs. It never
# restarts, deletes, scales, or patches any k8s object.
set -euo pipefail

export KUBECONFIG=${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
APP=${APP_DIR:-/opt/lastsietch-telemetry}
VENV=${VENV:-/opt/lastsietch-telemetry/venv}

PY=python3
[[ -x "$VENV/bin/python3" ]] && PY="$VENV/bin/python3"

# gamedb.py --discover prints KEY=VALUE lines for DB_HOST/DB_PORT/DB_USER/
# DB_PASS/DB_NAME/GAME_NS/GAME_POD. Run it once at start and export the result.
DISCOVERY=$("$PY" "$APP/gamedb.py" --discover)
if [[ -z "$DISCOVERY" ]]; then
  echo "telemetry: game-DB discovery returned nothing" >&2
  exit 1
fi

while IFS='=' read -r key value; do
  [[ -n "$key" ]] && export "$key=$value"
done <<< "$DISCOVERY"

if [[ -z "${DB_HOST:-}" || -z "${DB_PASS:-}" ]]; then
  echo "telemetry: failed to resolve DB connection details" >&2
  exit 1
fi

exec "$PY" "$APP/logger_service.py" "$@"
