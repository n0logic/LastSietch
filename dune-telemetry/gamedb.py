"""
Game-DB access for the Last Sietch Dune telemetry logger.

INVARIANT - PASSIVE READ-ONLY ACCESS:
  Every dune.* query issued through this module is a SELECT. There is no
  write method, and one must never be added. The logger NEVER runs
  INSERT/UPDATE/DELETE/DDL against the dune.* schema. k8s is touched only via
  read verbs (`kubectl get`, `kubectl exec ... printenv`, `kubectl logs`);
  the logger NEVER restarts, deletes, scales, or patches any k8s object.

The game DB (`dune`) is reached over a direct ClusterIP psycopg2 connection
(the market-bot pattern). discover_gamedb() resolves the namespace, DB pod,
DB service, ClusterIP, port, password and survival game pod fresh from k3s, so
a battlegroup rebuild (which changes the sh-... hash) needs no code edit.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time

import psycopg2
import psycopg2.extras

log = logging.getLogger("telemetry.gamedb")


def _kubectl(args, timeout=20):
    """Run a read-only kubectl command, return stdout stripped."""
    res = subprocess.run(
        ["kubectl"] + args, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(
            "kubectl %s failed: %s" % (" ".join(args), res.stderr.strip()))
    return res.stdout.strip()


def discover_gamedb():
    """Resolve game-DB connection details + the survival pod from k3s.

    Returns a dict of KEY -> value. Raises a clear error if the funcom-*
    namespace is missing or ambiguous.
    """
    # The game battlegroup namespace is funcom-seabass-sh-*. There is also a
    # funcom-operators namespace (the k8s operator) — match only the former.
    ns_lines = [l for l in _kubectl(["get", "ns", "-o", "name"]).splitlines()
                if l.startswith("namespace/funcom-seabass")]
    if len(ns_lines) != 1:
        raise RuntimeError(
            "expected exactly one funcom-seabass-* namespace, found %d: %s"
            % (len(ns_lines), ns_lines))
    ns = ns_lines[0].split("/", 1)[1]

    pod_lines = _kubectl(["get", "pods", "-n", ns, "-o", "name"]).splitlines()
    db_pods = [l for l in pod_lines if l.rstrip().endswith("db-dbdepl-sts-0")]
    if len(db_pods) != 1:
        raise RuntimeError(
            "expected one db-dbdepl-sts-0 pod in %s, found %s" % (ns, db_pods))
    db_pod = db_pods[0].split("/", 1)[1]

    svc_lines = _kubectl(["get", "svc", "-n", ns, "-o", "name"]).splitlines()
    db_svcs = [l for l in svc_lines if l.rstrip().endswith("db-dbdepl-svc")]
    if len(db_svcs) != 1:
        raise RuntimeError(
            "expected one db-dbdepl-svc service in %s, found %s" % (ns, db_svcs))
    db_svc = db_svcs[0].split("/", 1)[1]

    # Survival game pod - source of the kubectl-logs connection parse. The
    # survival server pods are named ...-sg-survival-N-pod-M. Match sg-survival
    # specifically: a bare "game" match also hits the mq-game-sts-0 RabbitMQ
    # pod, which is not a game server.
    game_pods = [l.split("/", 1)[1] for l in pod_lines
                 if "sg-survival" in l]
    game_pod = sorted(game_pods)[0] if game_pods else ""

    host = _kubectl(["get", "svc", "-n", ns, db_svc,
                     "-o", "jsonpath={.spec.clusterIP}"])
    port = _kubectl(["get", "svc", "-n", ns, db_svc,
                     "-o", "jsonpath={.spec.ports[0].port}"])
    password = _kubectl(["exec", "-n", ns, db_pod,
                         "--", "printenv", "POSTGRES_PASSWORD"])

    return {
        "GAME_NS": ns,
        "GAME_POD": game_pod,
        "DB_HOST": host,
        "DB_PORT": port,
        "DB_USER": "postgres",
        "DB_PASS": password,
        "DB_NAME": "dune",
    }


class GameDB:
    """psycopg2 wrapper for read-only access to the dune.* schema."""

    def __init__(self, config):
        self.config = config
        self.conn = None

    def connect(self):
        # autocommit: read-only access, no transactions needed, never holds
        # locks. search_path includes dune so unqualified refs resolve.
        conn = psycopg2.connect(
            host=self.config.db_host, port=self.config.db_port,
            user=self.config.db_user, password=self.config.db_pass,
            dbname=self.config.db_name, connect_timeout=10,
            application_name="lastsietch-telemetry",
            options="-c search_path=dune,public")
        conn.autocommit = True
        self.conn = conn
        log.info("connected to game DB %s:%s/%s",
                 self.config.db_host, self.config.db_port, self.config.db_name)

    def ensure_connected(self):
        """Lazy (re)connect. On repeated failure, re-discover a moved ClusterIP."""
        if self.conn is not None and self.conn.closed == 0:
            return
        for attempt in range(1, 6):
            try:
                self.connect()
                return
            except psycopg2.Error as exc:
                log.warning("game-DB connect attempt %d failed: %s", attempt, exc)
                # On a later attempt, the ClusterIP may have moved - re-discover.
                if attempt >= 2:
                    try:
                        info = discover_gamedb()
                        object.__setattr__(self.config, "db_host", info["DB_HOST"])
                        object.__setattr__(self.config, "db_port", int(info["DB_PORT"]))
                        object.__setattr__(self.config, "db_pass", info["DB_PASS"])
                        log.info("re-discovered game DB at %s:%s",
                                 info["DB_HOST"], info["DB_PORT"])
                    except Exception as derr:  # noqa: BLE001
                        log.warning("re-discovery failed: %s", derr)
                time.sleep(min(5 * attempt, 30))
        raise RuntimeError("could not connect to game DB after 5 attempts")

    def query(self, sql, params=None):
        """Run a SELECT, return rows as a list of dicts. Marks the connection
        dead on a psycopg2 error so the caller can isolate and retry."""
        self.ensure_connected()
        try:
            with self.conn.cursor(
                    cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params or {})
                return [dict(r) for r in cur.fetchall()]
        except psycopg2.Error:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass
            self.conn = None
            raise

    def query_scalar(self, sql, params=None):
        """Run a SELECT, return the first column of the first row (or None if no
        rows). Used by the read-model builders whose SQL returns one json/jsonb
        value (psycopg2 parses json/jsonb to a Python object). Marks the
        connection dead on a psycopg2 error so the caller can isolate and retry."""
        self.ensure_connected()
        try:
            with self.conn.cursor() as cur:
                # No params -> execute(sql) with no second arg, so psycopg2 does
                # NOT treat literal % (e.g. LIKE '%x%') as a parameter marker.
                if params is None:
                    cur.execute(sql)
                else:
                    cur.execute(sql, params)
                row = cur.fetchone()
                return row[0] if row else None
        except psycopg2.Error:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass
            self.conn = None
            raise

    def query_rows(self, sql, params=None):
        """Run a SELECT, return rows as a list of plain tuples (positional).
        Used by the landsraad read-model builder, which unpacks rows positionally
        the way its kubectl `-F|` runner did."""
        self.ensure_connected()
        try:
            with self.conn.cursor() as cur:
                if params is None:
                    cur.execute(sql)
                else:
                    cur.execute(sql, params)
                return cur.fetchall()
        except psycopg2.Error:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass
            self.conn = None
            raise

    def kubectl_logs(self, pod, since):
        """Read pod logs (read-only) for the connections stream."""
        res = subprocess.run(
            ["kubectl", "logs", "-n", self.config.game_ns, pod, "--since", since],
            capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            raise RuntimeError("kubectl logs failed: %s" % res.stderr.strip())
        return res.stdout

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass
            self.conn = None


def _print_discovery():
    """`python3 -m gamedb --discover` - print KEY=VALUE lines for the launcher."""
    info = discover_gamedb()
    for key, value in info.items():
        print("%s=%s" % (key, value))


if __name__ == "__main__":
    if "--discover" in sys.argv:
        _print_discovery()
    else:
        print("usage: python3 -m gamedb --discover", file=sys.stderr)
        sys.exit(1)
