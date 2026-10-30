#!/usr/bin/env python3
import argparse
import datetime
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess

spec = importlib.util.spec_from_file_location('vault_lab', Path(__file__).with_name('lab-check.py'))
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)
vault = lab.vault
TABLES = vault.exporter.TABLES  # the current game layout; each archive is read with its own set
PARENT_SCOPES = {'actor_inventories': ('inventory_id', 'inventories', 'id'),
                 'landclaim_segments': ('totem_id', 'totems', 'id'),
                 'building_blueprint_instances': ('building_blueprint_id', 'building_blueprints', 'id'),
                 'building_blueprint_pentashields': ('building_blueprint_id', 'building_blueprints', 'id'),
                 'building_blueprint_placeables': ('building_blueprint_id', 'building_blueprints', 'id')}
BLOCKERS = [
    'Production apply is disabled: no approved production restore target or maintenance window is bound.',
    'A shared maintenance lease does not prove shutdown-save completion, that the map is unloaded, or a fence preventing startup during the transaction.',
    'The archive has no trusted engine-build provenance; matching database schema does not prove game-build compatibility.',
    'In-game restoration, terrain and Coriolis-cycle compatibility, spatial overlap and live trigger/function behavior remain unverified.',
]


def identities(data, external):
    rows = data.get('external_actor_identities')
    if not isinstance(rows, list) or len(rows) != len(external['actors']):
        raise ValueError('ownership identity evidence is incomplete')
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'controller_id', 'account_id', 'character_id', 'character_name', 'funcom_id'}
                or any(type(row.get(key)) is not int or not 0 < row[key] < 2 ** 63
                       for key in ('controller_id', 'account_id', 'character_id'))
                or not isinstance(row.get('funcom_id'), str) or not row['funcom_id']
                or not isinstance(row.get('character_name'), str)
                or row['controller_id'] in seen):
            raise ValueError('ownership identity evidence is incomplete')
        seen.add(row['controller_id'])
    if seen != set(external['actors']) or data['owner_controller_id'] not in seen:
        raise ValueError('owner identity evidence does not match dependencies')
    return rows


def validate(raw):
    data, external = vault.validate(raw)
    if data['format'] == vault.NATIVE_FORMAT:
        # Packed backups restore through their own modes (Hagga: native backup the owner places
        # with the BRT; Deep Desert: scheduled-restart replant), not the in-place live planner.
        raise ValueError('native backup restore is not available yet')
    identities(data, external)
    location = data['source_location']
    if (type(location.get('partition_id')) is not int or not 0 < location['partition_id'] < 2 ** 63
            or type(location.get('dimension_index')) is not int or not 0 <= location['dimension_index'] <= 99
            or not isinstance(location.get('map'), str) or not re.fullmatch('[A-Za-z0-9_]{1,80}', location['map'])
            or any(type(value) is not int or not 0 < value < 2 ** 63 for values in external.values() for value in values)):
        raise ValueError('source location or external key is invalid')
    for table in vault.tables_for(data):
        row_key(data, table)
    return data, external


def row_key(data, table):
    constraints = sorted((c for c in data['schema']['constraints'] if c['table'] == table and c['kind'] in ('p', 'u')),
                         key=lambda c: c['kind'] != 'p')
    for constraint in constraints:
        keys = constraint['columns']
        if keys and all(all(row.get(key) is not None for key in keys) for row in data['tables'][table]):
            return keys
    if table in PARENT_SCOPES:
        field, parent, parent_key = PARENT_SCOPES[table]
        foreign_parent = 'actors' if table == 'landclaim_segments' else parent
        foreign = any(c['table'] == table and c['kind'] == 'f' and c['columns'] == [field]
                      and c['target'].removeprefix('dune.') == foreign_parent and c['target_columns'] == [parent_key]
                      for c in data['schema']['constraints'])
        parents = {row[parent_key] for row in data['tables'][parent]}
        if not foreign or any(row.get(field) not in parents for row in data['tables'][table]):
            raise ValueError('keyless table parent scope is unverified')
        return None
    if data['tables'][table]:
        raise ValueError('captured rows need a nonnull unique key or reviewed parent scope')
    return None


def parent_scope(table, payload):
    field, parent, parent_key = PARENT_SCOPES[table]
    return ('EXISTS (SELECT 1 FROM jsonb_populate_recordset(NULL::dune.' + lab.ident(parent) +
            ", (SELECT data->'tables'->" + lab.literal(parent) + ' FROM ' + payload + ')) parent WHERE t.' +
            lab.ident(field) + '=parent.' + lab.ident(parent_key) + ')')


def payload_sql(raw):
    return "SELECT convert_from(decode('" + raw.hex() + "','hex'),'UTF8')::jsonb AS data"


def selected(table):
    return 'jsonb_populate_recordset(NULL::dune.' + lab.ident(table) + ", (SELECT data->'tables'->" + lab.literal(table) + ' FROM payload))'


def partition_map_names(actor_map):
    native_groups = {'HaggaBasin': 'Survival_1', 'DeepDesert': 'DeepDesert_1'}
    return (actor_map, native_groups[actor_map]) if actor_map in native_groups else (actor_map,)


def preflight_select(raw):
    data, external = validate(raw)
    tables = vault.tables_for(data)
    collisions = []
    for table in tables:
        predicates = []
        for constraint in data['schema']['constraints']:
            if constraint['table'] == table and constraint['kind'] in ('p', 'u'):
                keys = constraint['columns']
                if not keys:
                    raise ValueError('empty captured key')
                predicates.append('(' + ' AND '.join('t.' + lab.ident(key) + '=s.' + lab.ident(key) for key in keys) + ')')
        expression = ' OR '.join(predicates) or 'false'
        where = 'EXISTS (SELECT 1 FROM ' + selected(table) + ' s WHERE ' + expression + ')'
        if row_key(data, table) is None and table in PARENT_SCOPES:
            where += ' OR ' + parent_scope(table, 'payload')
        collisions.append(lab.literal(table) + ',(SELECT count(*) FROM dune.' + lab.ident(table) + ' t WHERE ' + where + ')')
    missing = []
    for table, key in (('actors', 'id'), ('world_partition', 'partition_id'), ('encrypted_player_state', 'id'),
                       ('dune_exchanges', 'id'), ('vehicle_modules', 'id')):
        values = external[table]
        ids = ','.join(str(value) for value in values) or 'NULL'
        missing.append(lab.literal(table) + ',(' + str(len(values)) + '-(SELECT count(*) FROM dune.' +
                       lab.ident(table) + ' WHERE ' + lab.ident(key) + ' IN (' + ids + ')))')
    actual, typed = schema_parts(tables)
    return ("WITH payload AS (" + payload_sql(raw) + "), actual AS (" + actual + ") "
        "SELECT jsonb_build_object('schema_match',a.data=p.data->'schema','typed_schema_match'," + typed + ","
        "'root_missing',NOT EXISTS(SELECT 1 FROM dune.actors WHERE id=" + str(data['root_totem_id']) + "),"
        "'id_collisions',jsonb_build_object(" + ','.join(collisions) + "),"
        "'missing_dependencies',jsonb_build_object(" + ','.join(missing) + "),"
        "'owner_bindings_match',NOT EXISTS(SELECT 1 FROM jsonb_array_elements(p.data->'external_actor_identities') expected "
        "LEFT JOIN dune.player_state ps ON ps.player_controller_id=(expected->>'controller_id')::bigint "
        "LEFT JOIN dune.encrypted_player_state eps ON eps.id=(expected->>'character_id')::bigint "
        "LEFT JOIN dune.accounts ac ON ac.id=ps.account_id WHERE ps.account_id IS DISTINCT FROM (expected->>'account_id')::bigint "
        "OR eps.player_controller_id IS DISTINCT FROM (expected->>'controller_id')::bigint OR eps.account_id IS DISTINCT FROM (expected->>'account_id')::bigint OR ac.funcom_id::text IS DISTINCT FROM expected->>'funcom_id'),"
        "'target_partition_exists',EXISTS(SELECT 1 FROM dune.world_partition WHERE partition_id=" + str(data['source_location']['partition_id']) +
        ' AND map IN (' + ','.join(lab.literal(name) for name in partition_map_names(data['source_location']['map'])) +
        ') AND dimension_index=' + str(data['source_location']['dimension_index']) + "),"
        "'no_user_triggers',NOT EXISTS(SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='dune' AND NOT t.tgisinternal AND c.relname IN (" + ','.join(lab.literal(t) for t in tables) + "))) "
        "FROM payload p CROSS JOIN actual AS a(data)")


def schema_parts(tables):
    """(target metadata query, typed comparison expression). The target is read with the
    ARCHIVE's table set and only through the catalog, so a v1 archive against a 1.5.3
    target (no actor_state, extra actors.state) compares cleanly as a mismatch."""
    actual = vault.exporter.metadata_query(tables).strip().removesuffix(';')
    keys = "(SELECT jsonb_agg(jsonb_strip_nulls(CASE WHEN entry->>'kind'='f' THEN jsonb_set(entry,'{definition}',to_jsonb(regexp_replace(entry->>'definition',' DEFERRABLE( INITIALLY (DEFERRED|IMMEDIATE))?$','','g'))) ELSE entry END) ORDER BY entry->>'table',entry->>'name') FROM jsonb_array_elements(%s->'constraints') entry WHERE entry->>'table' IN (" + ','.join(lab.literal(t) for t in tables) + '))'
    columns = "(SELECT jsonb_agg(jsonb_strip_nulls(entry) ORDER BY entry->>'table',entry->>'name') FROM jsonb_array_elements(%s->'columns') entry)"
    types = "(SELECT jsonb_agg(jsonb_strip_nulls(entry) ORDER BY entry->>'name') FROM jsonb_array_elements(%s->'types') entry)"
    typed = '(' + ' AND '.join((expression % 'a.data') + '=' + (expression % "(p.data->'schema')") for expression in (columns, types, keys)) + ')'
    return actual, typed


def schema_select(raw):
    """Catalog-only schema comparison: safe to run when archive tables are absent from the target."""
    data, _ = validate(raw)
    actual, typed = schema_parts(vault.tables_for(data))
    return ("WITH payload AS (" + payload_sql(raw) + "), actual AS (" + actual + ") "
            "SELECT jsonb_build_object('schema_match',a.data=p.data->'schema','typed_schema_match'," + typed + ") AS result "
            "FROM payload p CROSS JOIN actual AS a(data)")


def build_schema_check(raw):
    return (schema_select(raw) + ';').encode()


def build_preflight(raw):
    return (preflight_select(raw) + ';').encode()


def build_restore_sql(raw):
    data, _ = validate(raw)
    tables = vault.tables_for(data)
    statements = ['BEGIN ISOLATION LEVEL SERIALIZABLE;',
        "SET LOCAL statement_timeout='90s'; SET LOCAL lock_timeout='2s'; SET LOCAL extra_float_digits=3;",
        "DO $$ BEGIN IF current_database()<>'vaultcheck' THEN RAISE EXCEPTION 'vault_restore_lab_only'; END IF; END $$;",
        'CREATE TEMP TABLE vault_restore_schema ON COMMIT DROP AS ' + schema_select(raw) + ';',
        "DO $$ BEGIN IF (SELECT (result->>'typed_schema_match')::boolean FROM vault_restore_schema) IS DISTINCT FROM TRUE "
        "THEN RAISE EXCEPTION 'vault_restore_schema'; END IF; END $$;",
        'LOCK TABLE ' + ','.join('dune.' + lab.ident(table) for table in sorted((*tables, 'world_partition', 'encrypted_player_state', 'dune_exchanges', 'vehicle_modules', 'accounts', 'player_state'))) + ' IN SHARE ROW EXCLUSIVE MODE;',
        'CREATE TEMP TABLE vault_restore_checks ON COMMIT DROP AS ' + preflight_select(raw) + ';',
        "DO $$ DECLARE result jsonb; BEGIN SELECT * INTO result FROM vault_restore_checks; "
        "IF (result->>'typed_schema_match')::boolean IS DISTINCT FROM TRUE THEN RAISE EXCEPTION 'vault_restore_schema'; END IF; "
        "IF (result->>'root_missing')::boolean IS DISTINCT FROM TRUE THEN RAISE EXCEPTION 'vault_restore_root_present'; END IF; "
        "IF jsonb_typeof(result->'id_collisions') IS DISTINCT FROM 'object' OR jsonb_typeof(result->'missing_dependencies') IS DISTINCT FROM 'object' THEN RAISE EXCEPTION 'vault_restore_unknown_checks'; END IF; "
        "IF (SELECT count(*) FROM jsonb_object_keys(result->'id_collisions'))<>" + str(len(tables)) + " OR (SELECT count(*) FROM jsonb_object_keys(result->'missing_dependencies'))<>5 THEN RAISE EXCEPTION 'vault_restore_unknown_checks'; END IF; "
        "IF EXISTS(SELECT 1 FROM jsonb_each(result->'id_collisions') e WHERE e.value<>'0'::jsonb) THEN RAISE EXCEPTION 'vault_restore_id_collision'; END IF; "
        "IF EXISTS(SELECT 1 FROM jsonb_each(result->'missing_dependencies') e WHERE e.value<>'0'::jsonb) THEN RAISE EXCEPTION 'vault_restore_missing_dependency'; END IF; "
        "IF (result->>'owner_bindings_match')::boolean IS DISTINCT FROM TRUE THEN RAISE EXCEPTION 'vault_restore_owner_changed'; END IF; "
        "IF (result->>'target_partition_exists')::boolean IS DISTINCT FROM TRUE THEN RAISE EXCEPTION 'vault_restore_partition'; END IF; "
        "IF (result->>'no_user_triggers')::boolean IS DISTINCT FROM TRUE THEN RAISE EXCEPTION 'vault_restore_unreviewed_trigger'; END IF; END $$;",
        'CREATE TEMP TABLE vault_restore_payload ON COMMIT DROP AS ' + payload_sql(raw) + ';', 'SET CONSTRAINTS ALL DEFERRED;']
    comparisons = []
    for table in tables:
        names = ','.join(lab.ident(c['name']) for c in data['schema']['columns'] if c['table'] == table)
        statements.append('INSERT INTO dune.' + lab.ident(table) + ' (' + names + ') SELECT ' + names +
                          ' FROM jsonb_populate_recordset(NULL::dune.' + lab.ident(table) +
                          ", (SELECT data->'tables'->" + lab.literal(table) + ' FROM vault_restore_payload));')
        primary = row_key(data, table)
        where = (' WHERE EXISTS (SELECT 1 FROM jsonb_populate_recordset(NULL::dune.' + lab.ident(table) +
                 ", (SELECT data->'tables'->" + lab.literal(table) + ' FROM vault_restore_payload)) s WHERE ' +
                 ' AND '.join('t.' + lab.ident(key) + '=s.' + lab.ident(key) for key in primary) + ')') if primary else ' WHERE false'
        if primary is None and table in PARENT_SCOPES:
            where = ' WHERE ' + parent_scope(table, 'vault_restore_payload')
        comparisons.append('SELECT ' + lab.literal(table) + " AS table_name,(SELECT data->'tables'->" + lab.literal(table) +
                           " FROM vault_restore_payload)=coalesce((SELECT jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text) FROM dune." +
                           lab.ident(table) + ' t' + where + "),'[]'::jsonb) AS exact_match")
    statements += array_bound_statements(data)
    # jsonb carries no array lower bound, so the insert above writes landclaim_original_global_location 1-based.
    # The game writes it [0:2] and base_backup_get_available_backups reads [0..2] since DA-26115 (1.5.3.4), so a
    # 1-based row lists its backup with X empty and every axis shifted. Rebase only the rows this restore wrote.
    rebase = any(c['table'] == 'totems' and c['name'] == 'landclaim_original_global_location'
                 for c in data['schema']['columns'])
    statements.append(("UPDATE dune.totems SET landclaim_original_global_location=('[0:2]={'||"
                      "array_to_string(landclaim_original_global_location,',')||'}')::real[] "
                      "WHERE array_lower(landclaim_original_global_location,1)=1 AND "
                      "array_length(landclaim_original_global_location,1)=3 AND id IN (SELECT id FROM "
                      "jsonb_populate_recordset(NULL::dune.totems,(SELECT data->'tables'->'totems' FROM vault_restore_payload)));")
                      if rebase else "SELECT 'no captured landclaim location to rebase';")
    statements += ['SET CONSTRAINTS ALL IMMEDIATE;',
        'CREATE TEMP TABLE vault_restore_result ON COMMIT DROP AS SELECT bool_and(exact_match) AS exact_match, count(*) AS tables_checked FROM (' + ' UNION ALL '.join(comparisons) + ') compared;',
        "DO $$ BEGIN IF (SELECT exact_match AND tables_checked=" + str(len(tables)) + " FROM vault_restore_result) IS DISTINCT FROM TRUE THEN RAISE EXCEPTION 'vault_restore_readback_mismatch'; END IF; END $$;",
        "SELECT jsonb_build_object('status','lab_restore_verified','exact_table_roundtrip',exact_match,'tables_checked',tables_checked,'production_restore_enabled',false) FROM vault_restore_result;", 'COMMIT;']
    return '\n'.join(statements).encode()


def array_bound_statements(data):
    """v3 only: the insert writes every array 1-based (jsonb has no lower bound); put each row
    this restore wrote back on the lower bound its archive recorded, then prove it. Rows are
    matched by the recorded row key, which the preflight proved free before the insert."""
    if data['format'] != vault.exporter.FORMAT_V3:
        return []
    types = {(c['table'], c['name']): c['type'] for c in data['schema']['columns']}
    updates, missing, wrong = [], [], []
    for table, keys, column in vault.exporter.LAYOUT_ARRAY_BOUNDS[data['format']]:
        if not data['array_bounds'][table]:
            continue
        kind = lab.typename(types.get((table, column)))
        if not kind.endswith('[]'):
            raise ValueError('recorded array bound on a non-array column')
        # Typed key equality (not a text concat) so the target's own key index can serve the match.
        key = '(' + ' AND '.join('t.' + lab.ident(name) + "=split_part(b.key,':'," + str(index) + ')::bigint'
                                 for index, name in enumerate(keys, 1)) + ')'
        col = 't.' + lab.ident(column)
        bounds = ("(SELECT key,(value::text)::int AS lb FROM jsonb_each((SELECT data->'array_bounds'->" + lab.literal(table) +
                  ' FROM vault_restore_payload)))')
        updates.append('UPDATE dune.' + lab.ident(table) + ' t SET ' + lab.ident(column) + "=('['||b.lb||':'||(b.lb+array_length(" +
                       col + ",1)-1)||']='||" + col + '::text)::' + kind + ' FROM ' + bounds + ' b WHERE ' + key +
                       ' AND array_lower(' + col + ',1)<>b.lb;')
        missing.append('EXISTS(SELECT 1 FROM ' + bounds + ' b WHERE NOT EXISTS(SELECT 1 FROM dune.' + lab.ident(table) +
                       ' t WHERE ' + key + ' AND array_lower(' + col + ',1)=b.lb))')
        wrong.append('EXISTS(SELECT 1 FROM dune.' + lab.ident(table) + ' t JOIN ' + bounds + ' b ON ' + key +
                     ' WHERE array_lower(' + col + ',1) IS DISTINCT FROM b.lb)')
    if not updates:
        return []
    return updates + ['DO $$ BEGIN IF ' + ' OR '.join(missing + wrong) +
                      " THEN RAISE EXCEPTION 'vault_restore_array_bounds'; END IF; END $$;"]


def build_lab_setup(raw):
    data, external = validate(raw)
    sql, _ = lab.build_sql(raw, include_snapshot=False)
    rows = identities(data, external)
    encoded = json.dumps(rows).encode().hex()
    fixture = ["BEGIN; CREATE TABLE dune.accounts(id bigint PRIMARY KEY,funcom_id text);",
        'CREATE TABLE dune.player_state(player_controller_id bigint PRIMARY KEY,account_id bigint,character_name text);',
        'ALTER TABLE dune.encrypted_player_state ADD COLUMN player_controller_id bigint, ADD COLUMN account_id bigint;',
        'ALTER TABLE dune.world_partition ADD COLUMN map text, ADD COLUMN dimension_index integer;',
        'UPDATE dune.world_partition SET map=' + lab.literal(data['source_location']['map']) + ',dimension_index=' + str(data['source_location']['dimension_index']) + ' WHERE partition_id=' + str(data['source_location']['partition_id']) + ';',
        "CREATE TEMP TABLE identity_fixture AS SELECT value FROM jsonb_array_elements(convert_from(decode('" + encoded + "','hex'),'UTF8')::jsonb);",
        "INSERT INTO dune.accounts SELECT DISTINCT (value->>'account_id')::bigint,value->>'funcom_id' FROM identity_fixture;",
        "INSERT INTO dune.player_state SELECT (value->>'controller_id')::bigint,(value->>'account_id')::bigint,value->>'character_name' FROM identity_fixture;",
        "INSERT INTO dune.encrypted_player_state(id) SELECT DISTINCT (value->>'character_id')::bigint FROM identity_fixture WHERE NOT EXISTS(SELECT 1 FROM dune.encrypted_player_state eps WHERE eps.id=(value->>'character_id')::bigint);",
        "UPDATE dune.encrypted_player_state eps SET player_controller_id=(i.value->>'controller_id')::bigint,account_id=(i.value->>'account_id')::bigint FROM identity_fixture i WHERE eps.id=(i.value->>'character_id')::bigint; COMMIT;"]
    return sql + b'\n' + '\n'.join(fixture).encode()


def execute_lab(raw, container):
    identity = lab.inspect_lab(container)
    result = subprocess.run(['docker', 'exec', '-i', identity, 'psql', '-X', '-q', '-t', '-A', '-U', 'postgres',
        '-d', 'vaultcheck', '-v', 'ON_ERROR_STOP=1'], input=build_restore_sql(raw), capture_output=True, timeout=120)
    if result.returncode or len(result.stdout) > 65536:
        raise ValueError('lab restore refused or unverified')
    receipt = json.loads(result.stdout)
    expected = len(vault.tables_for(vault.parse(raw)))
    if receipt.get('status') != 'lab_restore_verified' or receipt.get('exact_table_roundtrip') is not True or receipt.get('tables_checked') != expected:
        raise ValueError('lab restore response mismatch')
    return receipt


def setup_lab(raw, container):
    identity = lab.inspect_lab(container)
    result = subprocess.run(['docker', 'exec', '-i', identity, 'psql', '-X', '-q', '-t', '-A', '-U', 'postgres',
        '-d', 'vaultcheck', '-v', 'ON_ERROR_STOP=1'], input=build_lab_setup(raw), capture_output=True, timeout=120)
    if result.returncode or len(result.stdout) > 65536:
        raise ValueError('lab setup refused')
    return {'status': 'lab_target_ready', 'production_restore_enabled': False}


def make_plan(manifest, checks, identity, admin_id):
    created = datetime.datetime.now(datetime.timezone.utc)
    # A schema mismatch stops the probe after the catalog-only check: the table checks
    # were never evaluated, so they are unknown, not blocked.
    schema_only = bool(checks) and 'root_missing' not in checks
    def status(passed):
        return 'passed' if passed else 'unknown' if schema_only or not checks else 'blocked'
    entries = [
        {'id': 'archive_integrity', 'status': 'passed', 'detail': 'The immutable archive and payload checksums were verified.'},
        {'id': 'schema', 'status': 'passed' if checks.get('schema_match') is True else 'blocked' if checks else 'unknown', 'detail': 'Captured database schema must match the current target.'},
        {'id': 'missing_base', 'status': status(checks.get('root_missing') is True), 'detail': 'The original totem must be absent; existing bases are never overwritten.'},
        {'id': 'id_collisions', 'status': status(bool(checks.get('id_collisions')) and not any(checks['id_collisions'].values())), 'detail': 'Every captured primary/unique key and reviewed keyless parent scope must be free.'},
        {'id': 'dependencies', 'status': status(bool(checks.get('missing_dependencies')) and not any(checks['missing_dependencies'].values()) and checks.get('target_partition_exists') is True), 'detail': 'Every external dependency must exist; the original partition ID and map/dimension identity must still match.'},
        {'id': 'ownership', 'status': status(checks.get('owner_bindings_match') is True), 'detail': 'All original controller, character, account and provider bindings must still match.'},
        {'id': 'triggers', 'status': status(checks.get('no_user_triggers') is True), 'detail': 'Unreviewed target triggers block restoration.'},
        {'id': 'game_build', 'status': 'unknown', 'detail': BLOCKERS[2]},
        {'id': 'map_lifecycle', 'status': 'unknown', 'detail': BLOCKERS[1]},
        {'id': 'production_approval', 'status': 'blocked', 'detail': BLOCKERS[0]},
        {'id': 'gameplay_compatibility', 'status': 'unknown', 'detail': BLOCKERS[3]},
    ]
    source = {key: manifest[key] for key in ('snapshot_id', 'captured_at', 'payload_sha256', 'owner_controller_id')}
    source.update(totem_id=manifest['root_totem_id'], map=manifest['source_map'], partition_id=manifest['source_partition'], dimension_index=manifest['source_dimension'])
    return {'plan_id': identity, 'request_id': identity, 'snapshot_id': manifest['snapshot_id'],
        'created_at': created.isoformat(), 'expires_at': (created + datetime.timedelta(minutes=10)).isoformat(),
        'mode': 'original_location_missing_base', 'source': source,
        'target': {key: source[key] for key in ('map', 'partition_id', 'dimension_index', 'owner_controller_id')},
        'checks': entries, 'apply_allowed': False, 'blocking_reasons': list(BLOCKERS),
        'lab': {'status': 'not_run', 'completed_at': None}, 'admin_id': admin_id}


def apply_production(plan):
    raise ValueError('restore_apply_disabled')


if __name__ == '__main__':
    try:
        parser = argparse.ArgumentParser(description='Original-location recovery lab. Production apply is disabled.')
        parser.add_argument('action', choices=('lab-setup', 'lab-restore'))
        parser.add_argument('--snapshot', required=True)
        parser.add_argument('--container', required=True)
        args = parser.parse_args()
        if os.geteuid() != 0:
            raise ValueError('operator-only lab')
        os.umask(0o077)
        store = vault.Vault(vault.ROOT)
        manifest, raw = store.verify(args.snapshot)
        if args.action == 'lab-setup':
            result = setup_lab(raw, args.container)
        else:
            result = execute_lab(raw, args.container)
            result.update(source_payload_sha256=manifest['payload_sha256'], validation_scope='typed_restore_transaction',
                          in_game_spawn_tested=False)
            result = store.record_validation(args.snapshot, result)
        print(json.dumps(result))
    except Exception:
        print('{"status":"lab_restore_unverified","production_restore_enabled":false}')
        raise SystemExit(1)
