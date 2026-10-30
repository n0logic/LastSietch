#!/usr/bin/env python3
import argparse
import datetime
from decimal import Decimal
from contextlib import contextmanager
import fcntl
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import uuid

spec = importlib.util.spec_from_file_location('base_export', Path(__file__).with_name('export-base.py'))
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)
FORMAT = exporter.FORMAT
MAX_BYTES = exporter.MAX_BYTES
ROOT = Path('/opt/backups/dune/base-vault')
EXPECTED_CHECKS = {'bounded', 'not_packed', 'root_exists', 'one_location', 'single_totem',
    'expected_owner', 'schema_unchanged', 'no_shared_entities', 'single_root_entity',
    'no_shared_building_actor', 'no_unselected_owned_pieces', 'no_unsupported_incoming_rows',
    'no_unselected_travel_children', 'no_unselected_owned_placeables', 'no_exchange_or_vehicle_inventory',
    'no_pending_item_operations'}
NATIVE_FORMAT = exporter.NATIVE_FORMAT


def expected_checks(data):
    if data.get('format') == NATIVE_FORMAT:
        return set(exporter.NATIVE_CHECKS)
    if data.get('format') == exporter.FORMAT_V3:
        return EXPECTED_CHECKS | set(exporter.BLUEPRINT_CHECKS)
    return EXPECTED_CHECKS


def digest(data):
    return hashlib.sha256(data).hexdigest()


def parse(raw):
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError('snapshot size out of bounds')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    def invalid(value):
        raise ValueError('nonfinite JSON number')
    return json.loads(raw, parse_float=Decimal, parse_constant=invalid, object_pairs_hook=unique)


def tables_for(data):
    """The table set of this archive's own layout (v1 before 1.5.3, v2 after, v3 with blueprints)."""
    tables = exporter.LAYOUTS.get(data.get('format')) if isinstance(data, dict) else None
    if tables is None:
        raise ValueError('unsupported snapshot or consistency claim')
    return tables


def validate(raw):
    data = parse(raw)
    tables = tables_for(data)
    if (data.get('consistency') != 'persisted_database_state_not_unsaved_RAM'
            or data.get('transaction_isolation') != 'repeatable read' or data.get('transaction_read_only') != 'on'):
        raise ValueError('unsupported snapshot or consistency claim')
    native = data['format'] == NATIVE_FORMAT
    if set(data.get('checks', {})) != expected_checks(data) or any(value is not True for value in data['checks'].values()):
        raise ValueError('source coverage checks did not all pass')
    if set(data.get('tables', {})) != set(tables) or set(data.get('counts', {})) != set(tables):
        raise ValueError('incomplete table set')
    total = 0
    columns = {}
    for item in data['schema']['columns']:
        if item['table'] not in tables or item.get('generated') or item.get('identity'):
            raise ValueError('unsupported generated or identity column')
        columns.setdefault(item['table'], []).append(item)
    if set(columns) != set(tables):
        raise ValueError('schema table mismatch')
    for table, rows in data['tables'].items():
        if not isinstance(rows, list) or type(data['counts'][table]) is not int or len(rows) != data['counts'][table]:
            raise ValueError('count mismatch')
        total += len(rows)
        names = {item['name'] for item in columns[table]}
        required = {item['name'] for item in columns[table] if item['not_null']}
        for row in rows:
            if not isinstance(row, dict) or set(row) != names or any(row[key] is None for key in required):
                raise ValueError('row shape or nullability mismatch')
    if total > exporter.MAX_ROWS:
        raise ValueError('snapshot row limit exceeded')
    actors = {row['id']: row for row in data['tables']['actors']}
    root, owner = data['root_totem_id'], data['owner_controller_id']
    if type(root) is not int or type(owner) is not int or root <= 0 or owner <= 0 or root not in actors:
        raise ValueError('invalid root identity')
    if [row['id'] for row in data['tables']['totems']] != [root]:
        raise ValueError('root totem mismatch')
    if data['format'] in exporter.LAYOUT_ARRAY_BOUNDS:
        validate_array_bounds(data, exporter.LAYOUT_ARRAY_BOUNDS[data['format']])
    if native:
        validate_native(data, actors, root, owner)
    else:
        owners = [row['player_id'] for row in data['tables']['permission_actor_rank'] if row['permission_actor_id'] == root and row['rank'] == 1]
        if owners != [owner]:
            raise ValueError('owner mismatch')
    external = {'actors': set(), 'world_partition': set(), 'encrypted_player_state': set(),
                'dune_exchanges': set(), 'vehicle_modules': set()}
    for constraint in data['schema']['constraints']:
        table = constraint['table']
        if table not in tables:
            continue
        keys = constraint['columns'] or []
        if constraint['kind'] in ('p', 'u'):
            values = [tuple(row[key] for key in keys) for row in data['tables'][table]]
            nonnull = [value for value in values if all(part is not None for part in value)]
            if len(set(nonnull)) != len(nonnull):
                raise ValueError('duplicate primary or unique key')
        elif constraint['kind'] == 'f':
            target = constraint['target'].removeprefix('dune.')
            target_keys = constraint['target_columns']
            values = {tuple(row[key] for key in keys) for row in data['tables'][table] if all(row[key] is not None for key in keys)}
            available = {tuple(row[key] for key in target_keys) for row in data['tables'].get(target, [])}
            missing = values - available
            if not missing:
                continue
            allowed = ((table == 'permission_actor_rank' and keys == ['player_id'] and target == 'actors')
                or (table == 'actors' and keys == ['partition_id'] and target == 'world_partition')
                or (table == 'player_respawn_locations' and keys == ['character_id'] and target == 'encrypted_player_state')
                or (native and table == 'base_backups' and keys == ['player_id'] and target == 'actors'))
            if not allowed or len(target_keys) != 1:
                raise ValueError('missing internal dependency')
            external[target].update(value[0] for value in missing)
    location = data['source_location']
    for actor in actors.values():
        if (actor['map'], actor['partition_id'], actor['dimension_index']) != (location['map'], location['partition_id'], location['dimension_index']):
            raise ValueError('cross-map actor in base')
    if 'actor_state' not in tables and 'state' not in {item['name'] for item in columns['actors']}:
        raise ValueError('actor state column missing')
    state_rows = data['tables']['actor_state'] if 'actor_state' in tables else data['tables']['actors']
    if native:
        if any(row['state'] != 'BaseBackup' for row in state_rows):
            raise ValueError('unpacked actor in native backup')
    elif any(row['state'] == 'BaseBackup' for row in state_rows):
        raise ValueError('packed actor in live snapshot')
    return data, {table: sorted(values) for table, values in external.items()}


def validate_native(data, actors, root, owner):
    """A packed backup: one header owned by the expected controller, links that are exactly the
    captured actors, a parked (not original) location and the array lower bounds."""
    backup = data.get('root_backup_id')
    if type(backup) is not int or backup <= 0 or data.get('location_kind') != 'parked_backup':
        raise ValueError('invalid native backup identity')
    headers = data['tables']['base_backups']
    if len(headers) != 1 or headers[0]['id'] != backup or headers[0]['player_id'] != owner:
        raise ValueError('native backup owner mismatch')
    links = data['tables']['base_backup_linked_actors']
    linked = [row['actor_id'] for row in links]
    if (any(row['id'] != backup for row in links) or len(set(linked)) != len(linked)
            or set(linked) != set(actors) or root not in linked):
        raise ValueError('native backup links do not match the captured actors')
    if any(row['permission_actor_id'] == root for row in data['tables']['permission_actor_rank']):
        raise ValueError('packed totem still has permissions')
    if not isinstance(data.get('native_function_fingerprint'), str) or len(data['native_function_fingerprint']) != 32:
        raise ValueError('native function fingerprint missing')


def native_origin(data):
    """Where a packed backup's base will rebuild: the totem's stored claim origin. A native
    capture's source_location is the PARKED position, which can sit far from the origin
    (backup 112 was parked on Hagga with a Deep Desert origin)."""
    totem = data['tables']['totems'][0]
    point = totem.get('landclaim_original_global_location')
    if not isinstance(point, list) or len(point) != 3:
        point = None
    else:
        point = [float(value) for value in point]
    return {'totem_id': data['root_totem_id'], 'claim_origin': point}


def validate_array_bounds(data, columns):
    """The recorded lower bound of every captured non-empty array in the listed columns
    (NULL and '{}' have no lower bound)."""
    bounds = data.get('array_bounds')
    if not isinstance(bounds, dict) or set(bounds) != {table for table, _, _ in columns}:
        raise ValueError('array bounds missing')
    for table, keys, column in columns:
        expected = {':'.join(str(row[key]) for key in keys) for row in data['tables'][table] if row.get(column) not in (None, [])}
        if (not isinstance(bounds[table], dict) or set(bounds[table]) != expected
                or any(type(v) is not int for v in bounds[table].values())):
            raise ValueError('array bounds do not cover the captured rows')


def private_dir(path):
    path.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('unsafe vault directory')


def write_new(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as target:
        target.write(data)
        target.flush()
        os.fsync(target.fileno())
    sync_dir(path.parent)


def sync_dir(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_private(path, limit):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('unsafe vault file')
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError('vault file exceeds bound')
    return data


class Vault:
    def __init__(self, root):
        self.root = Path(root)
        private_dir(self.root)
        private_dir(self.root / 'snapshots')
        private_dir(self.root / 'validations')

    @contextmanager
    def locked(self):
        descriptor = os.open(self.root / 'vault.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'a') as lock:
            info = os.fstat(lock.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError('unsafe vault lock')
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def connect(self):
        path = self.root / 'catalog.sqlite'
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        info = os.fstat(descriptor)
        os.close(descriptor)
        if info.st_uid != os.geteuid() or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('unsafe catalog')
        con = sqlite3.connect(path, timeout=10)
        version = con.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1):
            con.close()
            raise ValueError('unknown catalog version')
        con.execute('PRAGMA journal_mode=DELETE')
        con.execute('PRAGMA synchronous=FULL')
        con.executescript('''CREATE TABLE IF NOT EXISTS snapshots (
            snapshot_id TEXT PRIMARY KEY, root_totem_id INTEGER NOT NULL, owner_controller_id INTEGER NOT NULL,
            captured_at TEXT NOT NULL, label TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
            archive_sha256 TEXT NOT NULL, counts_json TEXT NOT NULL, manifest_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS validations (validation_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL,
            completed_at TEXT NOT NULL, result_json TEXT NOT NULL);
            PRAGMA user_version=1;''')
        con.commit()
        return con

    def checkpoint(self, con):
        descriptor, path = tempfile.mkstemp(prefix='catalog-', suffix='.sqlite', dir=self.root)
        os.close(descriptor)
        target = sqlite3.connect(path)
        try:
            con.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('catalog snapshot integrity failure')
        finally:
            target.close()
        with open(path, 'rb') as file:
            os.fsync(file.fileno())
        os.replace(path, self.root / 'catalog.consistent.sqlite')
        sync_dir(self.root)

    def ingest(self, raw, label):
        data, external = validate(raw)
        if not isinstance(label, str) or not 1 <= len(label) <= 200:
            raise ValueError('invalid snapshot label')
        snapshot_id = str(uuid.uuid4())
        packed = gzip.compress(raw, compresslevel=6, mtime=0)
        manifest = {'format': 'lastsietch-base-vault-manifest-v1', 'snapshot_id': snapshot_id,
            'root_totem_id': data['root_totem_id'], 'owner_controller_id': data['owner_controller_id'],
            'captured_at': data['captured_at'], 'label': label, 'consistency': data['consistency'],
            'payload_sha256': digest(raw), 'archive_sha256': digest(packed),
            'payload_bytes': len(raw), 'archive_bytes': len(packed), 'counts': data['counts'],
            'source_map': data['source_location']['map'], 'source_partition': data['source_location']['partition_id'],
            'source_dimension': data['source_location']['dimension_index'],
            'external_reference_ids': external, 'production_restore_enabled': False,
            'automatic_deletion_enabled': False}
        if data['format'] == NATIVE_FORMAT:
            manifest.update(kind='native_backup', root_backup_id=data['root_backup_id'],
                            location_kind=data['location_kind'], origin=native_origin(data))
        encoded = json.dumps(manifest, sort_keys=True, indent=2).encode()
        with self.locked():
            write_new(self.root / 'snapshots' / (snapshot_id + '.json.gz'), packed)
            write_new(self.root / 'snapshots' / (snapshot_id + '.manifest.json'), encoded)
            con = self.connect()
            try:
                con.execute('INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?)', (snapshot_id,
                    manifest['root_totem_id'], manifest['owner_controller_id'], manifest['captured_at'], label,
                    manifest['payload_sha256'], manifest['archive_sha256'], json.dumps(manifest['counts']), encoded.decode()))
                con.commit()
                self.checkpoint(con)
            finally:
                con.close()
        self.verify(snapshot_id)
        return manifest

    def verify(self, snapshot_id):
        if not isinstance(snapshot_id, str) or str(uuid.UUID(snapshot_id)) != snapshot_id:
            raise ValueError('invalid snapshot ID')
        manifest = json.loads(read_private(self.root / 'snapshots' / (snapshot_id + '.manifest.json'), 65536))
        if manifest.get('snapshot_id') != snapshot_id or manifest.get('format') != 'lastsietch-base-vault-manifest-v1':
            raise ValueError('manifest identity mismatch')
        packed = read_private(self.root / 'snapshots' / (snapshot_id + '.json.gz'), MAX_BYTES)
        if len(packed) != manifest['archive_bytes'] or digest(packed) != manifest['archive_sha256']:
            raise ValueError('archive checksum mismatch')
        import io
        with gzip.GzipFile(fileobj=io.BytesIO(packed)) as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) != manifest['payload_bytes'] or digest(raw) != manifest['payload_sha256']:
            raise ValueError('payload checksum mismatch')
        data, external = validate(raw)
        if (data['counts'] != manifest['counts'] or data['root_totem_id'] != manifest['root_totem_id']
                or data['owner_controller_id'] != manifest['owner_controller_id']
                or data['captured_at'] != manifest['captured_at'] or data['consistency'] != manifest['consistency']
                or data['source_location']['map'] != manifest['source_map']
                or data['source_location']['partition_id'] != manifest['source_partition']
                or data['source_location']['dimension_index'] != manifest['source_dimension']
                or external != manifest['external_reference_ids']
                or manifest['production_restore_enabled'] is not False
                or manifest['automatic_deletion_enabled'] is not False
                or manifest.get('kind') != ('native_backup' if data['format'] == NATIVE_FORMAT else None)
                or manifest.get('root_backup_id') != data.get('root_backup_id')
                or manifest.get('location_kind') != data.get('location_kind')
                # Native manifests written before origin metadata carry none; any that do must match.
                or ('origin' in manifest and (data['format'] != NATIVE_FORMAT
                                              or manifest['origin'] != native_origin(data)))):
            raise ValueError('manifest data mismatch')
        return manifest, raw

    def list(self):
        con = self.connect()
        try:
            return [json.loads(row[0]) for row in con.execute('SELECT manifest_json FROM snapshots ORDER BY captured_at DESC LIMIT 1000')]
        finally:
            con.close()

    def record_validation(self, snapshot_id, result):
        self.verify(snapshot_id)
        validation_id = str(uuid.uuid4())
        receipt = {**result, 'validation_id': validation_id, 'snapshot_id': snapshot_id,
                   'completed_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
        with self.locked():
            write_new(self.root / 'validations' / (validation_id + '.json'), json.dumps(receipt, sort_keys=True, indent=2).encode())
            con = self.connect()
            try:
                con.execute('INSERT INTO validations VALUES (?,?,?,?)', (validation_id, snapshot_id, receipt['completed_at'], json.dumps(receipt)))
                con.commit()
                self.checkpoint(con)
            finally:
                con.close()
        return receipt


def main():
    parser = argparse.ArgumentParser(description='Admin-only non-destructive base snapshot vault. No production restore or delete command.')
    parser.add_argument('action', choices=('ingest', 'verify', 'list'))
    parser.add_argument('--snapshot')
    parser.add_argument('--input', type=Path)
    parser.add_argument('--label', default='Base snapshot')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('operator-only vault')
    os.umask(0o077)
    vault = Vault(ROOT)
    if args.action == 'ingest':
        if args.input is None:
            raise ValueError('snapshot input required')
        result = vault.ingest(read_private(args.input, MAX_BYTES), args.label)
    elif args.action == 'verify':
        result, _ = vault.verify(args.snapshot)
    else:
        result = vault.list()
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'status': 'vault_failed', 'error_class': type(error).__name__}))
        raise SystemExit(1)
