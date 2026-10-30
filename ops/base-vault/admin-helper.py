#!/usr/bin/env python3
from contextlib import contextmanager
import datetime
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import resource
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid

spec = importlib.util.spec_from_file_location('base_vault', Path(__file__).with_name('vault.py'))
vault = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vault)
restore_spec = importlib.util.spec_from_file_location('base_restore', Path(__file__).with_name('restore.py'))
restore = importlib.util.module_from_spec(restore_spec)
restore_spec.loader.exec_module(restore)
ROOT = vault.ROOT
REMOTE = ['/usr/bin/ssh', '-T', '-oBatchMode=yes', '-oConnectTimeout=10',
          '-oStrictHostKeyChecking=yes', '-oUpdateHostKeys=no',
          '-oServerAliveInterval=15', '-oServerAliveCountMax=2', 'lastsietch-dune',
          '/usr/bin/python3 -I /opt/lastsietch-base-vault/current/admin-export.py']

# The admin backend refuses a helper answer over 128 KiB; 200 catalog entries stay well under.
CATALOG_TOTEMS = 200


def capture_enabled():
    try:
        return vault.read_private(Path('/etc/lastsietch/base-vault-admin.enabled'), 16).strip() == b'1'
    except (ValueError, OSError):
        return False


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def request_id(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('invalid request ID')
    return value


def valid_backup_request(value):
    expected = {'action', 'backup_id'}
    if value['action'] == 'backup_capture':
        expected |= {'request_id', 'admin_id'}
        request_id(value.get('request_id'))
        if type(value.get('admin_id')) is not int or value['admin_id'] <= 0:
            raise ValueError('invalid administrator')
    if set(value) != expected or type(value.get('backup_id')) is not int or not 0 < value['backup_id'] < 2 ** 63:
        raise ValueError('invalid target')
    return value


def valid_request(value):
    if isinstance(value, dict) and value.get('action') in ('backup_history', 'backup_capture'):
        return valid_backup_request(value)
    if isinstance(value, dict) and value.get('action') == 'catalog':
        if set(value) != {'action'}:
            raise ValueError('invalid request')
        return value
    if not isinstance(value, dict) or value.get('action') not in ('history', 'capture', 'restore_plan', 'restore_read'):
        raise ValueError('invalid request')
    expected = {'action', 'totem_id'}
    if value['action'] in ('capture', 'restore_plan'):
        expected |= {'request_id', 'admin_id'}
        request_id(value.get('request_id'))
        if type(value.get('admin_id')) is not int or value['admin_id'] <= 0:
            raise ValueError('invalid administrator')
    if value['action'] == 'restore_plan':
        expected.add('snapshot_id')
        request_id(value.get('snapshot_id'))
    if value['action'] == 'restore_read':
        expected.add('plan_id')
        request_id(value.get('plan_id'))
    if (set(value) != expected or type(value.get('totem_id')) is not int
            or not 0 < value['totem_id'] < 2 ** 63):
        raise ValueError('invalid target')
    return value


def connect():
    vault.private_dir(ROOT)
    path = ROOT / 'admin-operations.sqlite'
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    info = os.fstat(descriptor)
    os.close(descriptor)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError('unsafe operation store')
    con = sqlite3.connect(path, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA synchronous=FULL')
    con.executescript('''CREATE TABLE IF NOT EXISTS operations (
        request_id TEXT PRIMARY KEY, totem_id INTEGER NOT NULL, admin_id INTEGER NOT NULL,
        status TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT,
        snapshot_id TEXT, error_code TEXT);
        CREATE TABLE IF NOT EXISTS verification (
        snapshot_id TEXT PRIMARY KEY, status TEXT NOT NULL, checked_at TEXT NOT NULL,
        manifest_sha256 TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS base_context (
        totem_id INTEGER PRIMARY KEY, checked_at REAL NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS native_operations (
        request_id TEXT PRIMARY KEY, backup_id INTEGER NOT NULL, admin_id INTEGER NOT NULL,
        status TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT,
        snapshot_id TEXT, error_code TEXT);''')
    con.commit()
    return con


@contextmanager
def locked(name):
    descriptor = os.open(ROOT / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as target:
        info = os.fstat(target.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError('unsafe operation lock')
        fcntl.flock(target, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def operation(row):
    if row is None:
        return None
    return {key: row[key] for key in ('request_id', 'status', 'started_at', 'completed_at', 'snapshot_id', 'error_code')}


def remote(action, target=None, key='totem_id'):
    # The payload stays in a root-only, unnamed temporary file until validated.
    with tempfile.TemporaryFile(dir=ROOT) as output:
        capturing = action in ('capture', 'capture_backup')
        limit = vault.MAX_BYTES if capturing else 65536
        body = {'action': action} if target is None else {'action': action, key: target}
        def bound_output():
            resource.setrlimit(resource.RLIMIT_FSIZE, (limit + 1, limit + 1))
        result = subprocess.run(REMOTE, input=json.dumps(body).encode(),
                                stdout=output, stderr=subprocess.DEVNULL,
                                timeout=245 if capturing else 20, preexec_fn=bound_output)
        if result.returncode or output.tell() > limit:
            raise ValueError('source unavailable')
        output.seek(0)
        raw = output.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('source exceeds bound')
        return raw


def expire(con):
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=350)).isoformat()
    for table in ('operations', 'native_operations'):
        con.execute("UPDATE " + table + " SET status='uncertain',error_code='capture_uncertain' "
                    "WHERE status IN ('pending','running') AND started_at<?", (cutoff,))
    con.commit()


def capture_active(con):
    """One capture at a time across live bases and packed backups."""
    return (con.execute("SELECT * FROM operations WHERE status IN ('pending','running','uncertain') ORDER BY started_at DESC LIMIT 1").fetchone()
            or con.execute("SELECT * FROM native_operations WHERE status IN ('pending','running','uncertain') ORDER BY started_at DESC LIMIT 1").fetchone())


def plan_directory():
    path = ROOT / 'restore-plans'
    vault.private_dir(path)
    return path


def public_plan(plan):
    return {key: value for key, value in plan.items() if key != 'admin_id'}


def read_plan(identity, totem):
    request_id(identity)
    plan = json.loads(vault.read_private(plan_directory() / (identity + '.json'), 65536))
    if plan['plan_id'] != identity or plan['source']['totem_id'] != totem or plan['apply_allowed'] is not False:
        raise ValueError('plan binding mismatch')
    return plan


def probe_restore(raw):
    command = REMOTE[:-1] + ['/usr/bin/python3 -I /opt/lastsietch-base-vault/current/restore-probe.py']
    with tempfile.TemporaryFile(dir=ROOT) as output:
        def bound_output():
            resource.setrlimit(resource.RLIMIT_FSIZE, (65537, 65537))
        result = subprocess.run(command, input=raw, stdout=output, stderr=subprocess.DEVNULL,
                                timeout=75, preexec_fn=bound_output)
        if result.returncode or output.tell() > 65536:
            raise ValueError('restore preflight unavailable')
        output.seek(0)
        result = json.loads(output.read(65537))
        if not isinstance(result, dict):
            raise ValueError('restore preflight unavailable')
        return result


def stage_restore(request):
    store = vault.Vault(ROOT)
    directory = plan_directory()
    path = directory / (request['request_id'] + '.json')
    with locked('restore-plan.lock'):
        if path.exists():
            plan = read_plan(request['request_id'], request['totem_id'])
            if plan['snapshot_id'] != request['snapshot_id'] or plan['admin_id'] != request['admin_id']:
                return {'error': 'request_conflict'}
            index_plan(plan)
            return public_plan(plan)
        manifest, raw = store.verify(request['snapshot_id'])
        if manifest['root_totem_id'] != request['totem_id']:
            return {'error': 'request_conflict'}
        if manifest.get('kind') == 'native_backup':
            return {'error': 'native_restore_unavailable'}
        try:
            restore.validate(raw)
            checks = probe_restore(raw)
        except Exception:
            checks = {}
        plan = restore.make_plan(manifest, checks, request['request_id'], request['admin_id'])
        catalog = store.connect()
        try:
            row = catalog.execute('SELECT completed_at,result_json FROM validations WHERE snapshot_id=? ORDER BY completed_at DESC LIMIT 1',
                                  (request['snapshot_id'],)).fetchone()
            result = json.loads(row[1]) if row else {}
            if (result.get('status') == 'lab_restore_verified' and result.get('source_payload_sha256') == manifest['payload_sha256']
                    and result.get('exact_table_roundtrip') is True
                    and result.get('tables_checked') == len(vault.tables_for(vault.parse(raw)))):
                plan['lab'] = {'status': 'passed', 'completed_at': row[0], 'scope': 'typed_restore_transaction'}
        finally:
            catalog.close()
        vault.write_new(path, json.dumps(plan, sort_keys=True).encode())
        index_plan(plan)
        return public_plan(plan)


def index_plan(plan):
    con = connect()
    try:
        con.execute('CREATE TABLE IF NOT EXISTS restore_plans (plan_id TEXT PRIMARY KEY,totem_id INTEGER NOT NULL,created_at TEXT NOT NULL)')
        row = con.execute('SELECT totem_id,created_at FROM restore_plans WHERE plan_id=?', (plan['plan_id'],)).fetchone()
        expected = (plan['source']['totem_id'], plan['created_at'])
        if row is None:
            con.execute('INSERT INTO restore_plans VALUES (?,?,?)', (plan['plan_id'], *expected))
        elif tuple(row) != expected:
            raise ValueError('restore plan index mismatch')
        con.commit()
    finally:
        con.close()


def capture(request):
    con = connect()
    try:
        with locked('admin.lock'):
            expire(con)
            old = con.execute('SELECT * FROM operations WHERE request_id=?', (request['request_id'],)).fetchone()
            if old:
                if old['totem_id'] != request['totem_id'] or old['admin_id'] != request['admin_id']:
                    return {'error': 'request_conflict'}
                return {'operation': operation(old)}
            if not capture_enabled():
                return {'error': 'capture_disabled'}
            if capture_active(con) or con.execute('SELECT 1 FROM native_operations WHERE request_id=?', (request['request_id'],)).fetchone():
                return {'error': 'capture_busy'}
            con.execute('INSERT INTO operations VALUES (?,?,?,?,?,NULL,NULL,NULL)',
                        (request['request_id'], request['totem_id'], request['admin_id'], 'pending', now()))
            con.commit()
            try:
                result = subprocess.run(['/usr/bin/systemctl', 'start', '--no-block',
                    'lastsietch-base-vault-capture@' + request['request_id'] + '.service'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                if result.returncode:
                    raise ValueError('worker start failed')
            except Exception:
                con.execute("UPDATE operations SET status='uncertain',error_code='capture_uncertain' WHERE request_id=?",
                            (request['request_id'],))
                con.commit()
            return {'operation': operation(con.execute('SELECT * FROM operations WHERE request_id=?',
                                                        (request['request_id'],)).fetchone())}
    finally:
        con.close()


def worker(identity):
    request_id(identity)
    con = connect()
    ingest_started = False
    owns_operation = False
    try:
        with locked('capture.lock'):
            row = con.execute('SELECT * FROM operations WHERE request_id=?', (identity,)).fetchone()
            table = 'operations'
            if row is None:
                row = con.execute('SELECT * FROM native_operations WHERE request_id=?', (identity,)).fetchone()
                table = 'native_operations'
            if row is None or row['status'] != 'pending':
                raise ValueError('worker request unavailable')
            con.execute("UPDATE " + table + " SET status='running' WHERE request_id=?", (identity,))
            con.commit()
            owns_operation = True
            if not capture_enabled():
                raise ValueError('capture disabled')
            if table == 'native_operations':
                raw = remote('capture_backup', row['backup_id'], 'backup_id')
                data, _ = vault.validate(raw)
                if data['format'] != vault.NATIVE_FORMAT or data['root_backup_id'] != row['backup_id']:
                    raise ValueError('source target mismatch')
                label = 'Admin packed backup snapshot ' + identity
            else:
                raw = remote('capture', row['totem_id'])
                data, _ = vault.validate(raw)
                if data['format'] == vault.NATIVE_FORMAT or data['root_totem_id'] != row['totem_id']:
                    raise ValueError('source target mismatch')
                label = 'Admin snapshot ' + identity
            ingest_started = True
            store = vault.Vault(ROOT)
            manifest = store.ingest(raw, label)
            store.verify(manifest['snapshot_id'])
            con.execute('INSERT OR REPLACE INTO verification VALUES (?,?,?,?)',
                        (manifest['snapshot_id'], 'verified', now(), vault.digest(json.dumps(manifest, sort_keys=True).encode())))
            con.execute("UPDATE " + table + " SET status='completed',completed_at=?,snapshot_id=?,error_code=NULL WHERE request_id=?",
                        (now(), manifest['snapshot_id'], identity))
            con.commit()
    except Exception:
        # Once ingest starts, files or a catalog row may exist. Never recapture automatically.
        if owns_operation:
            con.execute("UPDATE " + table + " SET status=?,completed_at=?,error_code=? WHERE request_id=? AND status IN ('pending','running')",
                        ('uncertain' if ingest_started else 'failed', now(),
                         'capture_uncertain' if ingest_started else 'capture_refused', identity))
            con.commit()
    finally:
        con.close()


def snapshot_entries(store, con, catalog, rows):
    """History rows for manifests, newest first. At most one unverified archive is checked
    on demand per request. Live and packed-backup captures render alike, with their kind."""
    snapshots = []
    verified_one = False
    for row in rows:
        manifest = json.loads(row[0])
        identity = manifest['snapshot_id']
        manifest_hash = vault.digest(json.dumps(manifest, sort_keys=True).encode())
        checked = con.execute('SELECT * FROM verification WHERE snapshot_id=?', (identity,)).fetchone()
        if checked and checked['manifest_sha256'] != manifest_hash:
            checked = None
        if not checked and not verified_one:
            verified_one = True
            try:
                verified, _ = store.verify(identity)
                if verified != manifest:
                    raise ValueError('catalog manifest mismatch')
                status = 'verified'
            except Exception:
                status = 'failed'
            con.execute('INSERT OR REPLACE INTO verification VALUES (?,?,?,?)', (identity, status, now(), manifest_hash))
            con.commit()
            checked = con.execute('SELECT * FROM verification WHERE snapshot_id=?', (identity,)).fetchone()
        validation = catalog.execute('SELECT completed_at,result_json FROM validations WHERE snapshot_id=? ORDER BY completed_at DESC LIMIT 1',
                                     (identity,)).fetchone()
        result = json.loads(validation[1]) if validation else {}
        snapshots.append({key: manifest[key] for key in ('snapshot_id', 'captured_at', 'label', 'counts',
            'archive_bytes', 'payload_sha256', 'archive_sha256')} | {
            'kind': manifest.get('kind', 'live_base'), 'root_backup_id': manifest.get('root_backup_id'),
            'origin': manifest.get('origin'),
            'verification': {'status': checked['status'] if checked else 'not_checked',
                             'checked_at': checked['checked_at'] if checked else None},
            'validation': {'status': 'passed' if result.get('status') in ('isolated_database_roundtrip_verified', 'lab_restore_verified') else 'failed' if validation else 'not_run',
                           'completed_at': validation[0] if validation else None}})
    return snapshots


def history(totem):
    store = vault.Vault(ROOT)
    con = connect()
    catalog = store.connect()
    try:
        expire(con)
        cached = con.execute('SELECT * FROM base_context WHERE totem_id=?', (totem,)).fetchone()
        if cached and time.time() - cached['checked_at'] < 30:
            base = json.loads(cached['data'])
        else:
            try:
                base = json.loads(remote('describe', totem))['base']
                if base is not None and base.get('totem_id') != totem:
                    raise ValueError('target mismatch')
                con.execute('INSERT OR REPLACE INTO base_context VALUES (?,?,?)', (totem, time.time(), json.dumps(base)))
                con.commit()
            except Exception:
                base = None
        rows = catalog.execute('SELECT manifest_json FROM snapshots WHERE root_totem_id=? ORDER BY captured_at DESC LIMIT 50',
                               (totem,)).fetchall()
        count = catalog.execute('SELECT count(*) FROM snapshots WHERE root_totem_id=?', (totem,)).fetchone()[0]
        snapshots = snapshot_entries(store, con, catalog, rows)
        active = capture_active(con)
        latest = con.execute('SELECT * FROM operations WHERE totem_id=? ORDER BY started_at DESC LIMIT 1', (totem,)).fetchone()
        safe_base = {key: base.get(key) for key in ('totem_id', 'label', 'map', 'dimension_index', 'owner_name', 'owner_controller_id', 'backup_id')} if base else None
        plans = []
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='restore_plans'").fetchone():
            plans = [public_plan(read_plan(row[0], totem)) for row in con.execute(
                'SELECT plan_id FROM restore_plans WHERE totem_id=? ORDER BY created_at DESC LIMIT 10', (totem,))]
        return {'available': True, 'totem_id': totem, 'base': safe_base,
                'capture_allowed': bool(capture_enabled() and base and base.get('capture_allowed') and not active),
                'capture_blocked': active['status'] if active else None,
                'snapshots': snapshots, 'total_snapshots': count, 'truncated': count > len(snapshots),
                'operation': operation(latest), 'consistency': 'persisted_database_state_not_unsaved_RAM',
                'restore_enabled': False, 'restore_planning_enabled': True, 'restore_plans': plans}
    finally:
        catalog.close()
        con.close()


def catalog():
    """Vault coverage per totem for the admin base directory: newest capture of any kind,
    newest live-base capture and the packed backups seen. Catalog and journal reads only:
    no remote call and no on-demand verification, so it is cheap enough to back a list."""
    store = vault.Vault(ROOT)
    con = connect()
    catalog_db = store.connect()
    try:
        checked = {row['snapshot_id']: (row['status'], row['manifest_sha256'])
                   for row in con.execute('SELECT snapshot_id,status,manifest_sha256 FROM verification')}
        totems = {}
        for (raw,) in catalog_db.execute('SELECT manifest_json FROM snapshots ORDER BY captured_at DESC LIMIT 1000'):
            manifest = json.loads(raw)
            status, manifest_hash = checked.get(manifest['snapshot_id'], ('not_checked', None))
            if manifest_hash is not None and manifest_hash != vault.digest(json.dumps(manifest, sort_keys=True).encode()):
                status = 'not_checked'
            kind = manifest.get('kind', 'live_base')
            entry = totems.get(manifest['root_totem_id'])
            if entry is None:
                if len(totems) >= CATALOG_TOTEMS:
                    continue
                entry = totems[manifest['root_totem_id']] = {
                    'totem_id': manifest['root_totem_id'], 'snapshots': 0,
                    'last_captured_at': manifest['captured_at'], 'last_kind': kind, 'last_verification': status,
                    'last_live_captured_at': None, 'last_live_snapshot_id': None, 'last_live_verification': None,
                    'live_source_map': None, 'live_source_dimension': None, 'backup_ids': []}
            entry['snapshots'] += 1
            if kind == 'native_backup':
                if manifest.get('root_backup_id') not in entry['backup_ids']:
                    entry['backup_ids'].append(manifest.get('root_backup_id'))
            elif entry['last_live_snapshot_id'] is None:
                entry.update(last_live_captured_at=manifest['captured_at'], last_live_snapshot_id=manifest['snapshot_id'],
                             last_live_verification=status, live_source_map=manifest.get('source_map'),
                             live_source_dimension=manifest.get('source_dimension'))
        total = catalog_db.execute('SELECT count(*) FROM snapshots').fetchone()[0]
        return {'available': True, 'totems': list(totems.values()), 'total_snapshots': total,
                'truncated': total > 1000 or len(totems) >= CATALOG_TOTEMS}
    finally:
        catalog_db.close()
        con.close()


def origin_snapshots(con, catalog, totem):
    """Live-base captures of the packed backup's own totem, newest first. A native capture
    records only the parked position and the claim origin; ranks and the placed layout at
    the original site exist only in a live capture, so a Deep Desert replant needs both."""
    found = []
    for (raw,) in catalog.execute('SELECT manifest_json FROM snapshots WHERE root_totem_id=? ORDER BY captured_at DESC LIMIT 50',
                                  (totem,)).fetchall():
        manifest = json.loads(raw)
        if manifest.get('kind') == 'native_backup':
            continue
        checked = con.execute('SELECT status,manifest_sha256 FROM verification WHERE snapshot_id=?',
                              (manifest['snapshot_id'],)).fetchone()
        status = 'not_checked'
        if checked and checked['manifest_sha256'] == vault.digest(json.dumps(manifest, sort_keys=True).encode()):
            status = checked['status']
        found.append({'snapshot_id': manifest['snapshot_id'], 'captured_at': manifest['captured_at'],
                      'source_map': manifest.get('source_map'), 'source_dimension': manifest.get('source_dimension'),
                      'verification': status})
        if len(found) == 5:
            break
    return found


def backup_history(backup):
    store = vault.Vault(ROOT)
    con = connect()
    catalog = store.connect()
    try:
        expire(con)
        try:
            described = json.loads(remote('describe_backup', backup, 'backup_id'))['backup']
            if described is not None and described.get('backup_id') != backup:
                raise ValueError('target mismatch')
        except Exception:
            described = None
        rows = []
        for row in catalog.execute("SELECT manifest_json FROM snapshots WHERE manifest_json LIKE '%native_backup%' "
                                   'ORDER BY captured_at DESC LIMIT 500').fetchall():
            if json.loads(row[0]).get('root_backup_id') == backup:
                rows.append(row)
        active = capture_active(con)
        latest = con.execute('SELECT * FROM native_operations WHERE backup_id=? ORDER BY started_at DESC LIMIT 1', (backup,)).fetchone()
        safe = {key: described.get(key) for key in ('backup_id', 'label', 'owner_name', 'owner_controller_id',
                'linked_actors', 'totem_id', 'map')} if described else None
        totem = described.get('totem_id') if described else None
        if type(totem) is not int and rows:
            totem = json.loads(rows[0][0]).get('root_totem_id')
        origins = origin_snapshots(con, catalog, totem) if type(totem) is int else []
        return {'available': True, 'backup_id': backup, 'backup': safe, 'origin_totem_id': totem,
                'origin_snapshots': origins, 'origin_snapshot_available': bool(origins),
                'capture_allowed': bool(capture_enabled() and described and described.get('capture_allowed') and not active),
                'capture_blocked': active['status'] if active else None,
                'snapshots': snapshot_entries(store, con, catalog, rows[:50]), 'total_snapshots': len(rows),
                'truncated': len(rows) > 50, 'operation': operation(latest),
                'consistency': 'persisted_database_state_not_unsaved_RAM', 'location_kind': 'parked_backup',
                'restore_enabled': False}
    finally:
        catalog.close()
        con.close()


def backup_capture(request):
    con = connect()
    try:
        with locked('admin.lock'):
            expire(con)
            old = con.execute('SELECT * FROM native_operations WHERE request_id=?', (request['request_id'],)).fetchone()
            if old:
                if old['backup_id'] != request['backup_id'] or old['admin_id'] != request['admin_id']:
                    return {'error': 'request_conflict'}
                return {'operation': operation(old)}
            if not capture_enabled():
                return {'error': 'capture_disabled'}
            if capture_active(con) or con.execute('SELECT 1 FROM operations WHERE request_id=?', (request['request_id'],)).fetchone():
                return {'error': 'capture_busy'}
            con.execute('INSERT INTO native_operations VALUES (?,?,?,?,?,NULL,NULL,NULL)',
                        (request['request_id'], request['backup_id'], request['admin_id'], 'pending', now()))
            con.commit()
            try:
                result = subprocess.run(['/usr/bin/systemctl', 'start', '--no-block',
                    'lastsietch-base-vault-capture@' + request['request_id'] + '.service'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                if result.returncode:
                    raise ValueError('worker start failed')
            except Exception:
                con.execute("UPDATE native_operations SET status='uncertain',error_code='capture_uncertain' WHERE request_id=?",
                            (request['request_id'],))
                con.commit()
            return {'operation': operation(con.execute('SELECT * FROM native_operations WHERE request_id=?',
                                                        (request['request_id'],)).fetchone())}
    finally:
        con.close()


if __name__ == '__main__':
    try:
        if os.geteuid() != 0:
            raise ValueError('operator-only helper')
        os.umask(0o077)
        if len(sys.argv) == 3 and sys.argv[1] == '--worker':
            worker(sys.argv[2])
        elif len(sys.argv) == 1:
            raw = sys.stdin.buffer.read(2049)
            if len(raw) > 2048:
                raise ValueError('request too large')
            request = valid_request(json.loads(raw))
            if request['action'] == 'catalog':
                result = catalog()
            elif request['action'] == 'backup_history':
                result = backup_history(request['backup_id'])
            elif request['action'] == 'backup_capture':
                result = backup_capture(request)
            elif request['action'] == 'history':
                result = history(request['totem_id'])
            elif request['action'] == 'capture':
                result = capture(request)
            elif request['action'] == 'restore_plan':
                result = stage_restore(request)
            else:
                try:
                    result = public_plan(read_plan(request['plan_id'], request['totem_id']))
                except FileNotFoundError:
                    result = {'error': 'plan_not_found'}
            print(json.dumps(result))
        else:
            raise ValueError('invalid command')
    except Exception:
        print('{"error":"vault_unavailable"}')
        raise SystemExit(1)
