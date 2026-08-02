#!/usr/bin/env bash
# dq.sh: run psql against the Funcom game database inside its own pod.
#
# Almost every other tool in this repository shells out to this one. Deploy it
# to /root/dq.sh on the game host and the rest will find it.
#
#   ./dq.sh -c "SELECT count(*) FROM dune.player_state;"
#   printf 'SELECT ...' | ./dq.sh -At
#
# Configuration, all overridable by environment:
#   DUNE_NS    kubernetes namespace of your battlegroup
#   DUNE_BG    battlegroup name, used to derive the database pod
#   DUNE_POD   database pod, if you would rather not derive it
#   DUNE_DB    database name, default "dune"
#   DUNE_PORT  in-pod postgres port, default 15432
#
# ---------------------------------------------------------------------------
# CONTRACT. Other tools depend on these behaviours, and breaking them fails
# quietly rather than loudly. Read before editing.
#
# 1. IT BEHAVES EXACTLY LIKE psql. In particular it does NOT filter psql's
#    command tags. If you pipe in "SET search_path TO dune, public;" ahead of
#    your query, psql echoes a "SET" line first, and under -At that arrives as
#    line 1 of your output. That is psql being correct, not a bug here.
#    Strip it caller-side if it is in your way:
#        drop_set_tag(){ awk 'NR==1 && $0=="SET"{next} {print}'; }
#    Do NOT make this script swallow command tags. Write paths print and check
#    "UPDATE 1" versus "UPDATE 0", and suppressing tags would hide that.
#
# 2. psql -v SUBSTITUTION DOES NOT WORK WITH -c UNDER kubectl exec. Use stdin
#    or -f instead. This is a real constraint and costs an afternoon to
#    rediscover.
#
# 3. THIS IS NOT READ-ONLY, and it connects as the postgres SUPERUSER. It is a
#    psql wrapper: it will run DDL, DELETE, or anything else you hand it.
#    Treat every invocation as direct database access against a live game
#    server, and gate your own write paths. Nothing here will stop you.
#
# 4. It stores no credential. The password is read from the database pod's own
#    environment at call time, so there is nothing here to leak or rotate.
# ---------------------------------------------------------------------------

set -euo pipefail

NS="${DUNE_NS:-funcom-seabass-sh-<your-hostid>-<random>}"
BG="${DUNE_BG:-sh-<your-hostid>-<random>}"
DB="${DUNE_DB:-dune}"
PORT="${DUNE_PORT:-15432}"

if [[ "$NS" == *"<your-hostid>"* ]]; then
    echo "dq.sh: DUNE_NS is still the placeholder." >&2
    echo "  Export it, or edit the default at the top of this file." >&2
    echo "  Find yours with: kubectl get ns | grep funcom-seabass" >&2
    exit 2
fi

# Derive the database pod unless told otherwise. Deriving beats hardcoding: the
# name changes when a battlegroup is rebuilt, and a stale literal then fails in
# a confusing way instead of an obvious one.
POD="${DUNE_POD:-${BG}-db-dbdepl-sts-0}"

if ! sudo kubectl get pod -n "$NS" "$POD" >/dev/null 2>&1; then
    echo "dq.sh: database pod '$POD' not found in namespace '$NS'." >&2
    echo "  Candidates:" >&2
    sudo kubectl get pods -n "$NS" --no-headers 2>/dev/null \
        | awk '$1 ~ /db-dbdepl/ {print "    " $1}' >&2 || true
    echo "  Override with DUNE_POD once you have identified it." >&2
    exit 3
fi

PGPASS="$(sudo kubectl exec -n "$NS" "$POD" -- printenv POSTGRES_PASSWORD)"

exec sudo kubectl exec -i -n "$NS" "$POD" -- \
    env PGPASSWORD="$PGPASS" \
    psql -h localhost -p "$PORT" -U postgres -d "$DB" "$@"
