#!/usr/bin/env python3
"""VAULT-04 mode C: translate a pre-1.5.3 (v1) archive into the 1.5.3 (v2) layout.

The result is a NEW v2 archive; the v1 archive is never changed. Every schema
difference between the two layouts must be one of the reviewed 1.5.3 changes below,
or the translation refuses:

  * dune.actor_state is gone: its state moves onto actors.state. v1 wrote a row only
    for a non-default state, so an actor without one is 'Default'.
  * building_instances lost its three stabilization columns. Dropped only when every
    captured value is 0 (nothing to lose); anything else refuses.
  * three *_by_player_id columns are new. Filled exactly as the 1.5.3 migration filled
    existing rows: last_placed_by_player_id is NOT NULL DEFAULT 0 (both 1.5.3 captures
    carry 0 on every pre-1.5.3 piece), edited_by_player_id is nullable (NULL).
  * the actorstate enum gained labels at the end: v1 labels must be a prefix.

The target schema comes from a verified v2 or v3 archive (the same 1.5.3 catalog the
live game runs; a v3 schema is read through its v2 tables), so the translated archive
carries the schema restore compares against. The output stays v2: a v1 capture could
not hold a blueprint item (incoming coverage refused it), so it has no blueprint rows.

Run as root on <web-host>:
  translate.py --v1 UUID --schema-from UUID            # dry run: report only
  translate.py --v1 UUID --schema-from UUID --ingest   # write the new archive into the vault
"""
import argparse
from decimal import Decimal
import hashlib
import importlib.util
import json
import os
from pathlib import Path

spec = importlib.util.spec_from_file_location('base_vault', Path(__file__).with_name('vault.py'))
vault = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vault)

V1 = 'lastsietch-base-snapshot-v1'
V2 = vault.exporter.FORMAT_V2
V3 = vault.exporter.FORMAT_V3
DROPPED = {('actor_state', 'actor_id'), ('actor_state', 'state'),
           ('building_instances', 'stabilization_begin_timespan'),
           ('building_instances', 'stabilization_end_timespan'),
           ('building_instances', 'stabilization_state')}
# (table, column) -> (value for existing rows, target NOT NULL), from the live catalog.
ADDED = {('building_instances', 'last_placed_by_player_id'): (0, True),
         ('placeables', 'last_placed_by_player_id'): (0, True),
         ('permission_actor', 'edited_by_player_id'): (None, False)}
STABILIZATION = sorted(name for table, name in DROPPED if table == 'building_instances')


def v2_schema(archive):
    """The v2 target schema from a verified v2 or v3 archive. A v3 catalog read is the v2 read
    plus the blueprint tables' own columns and constraints; the FKs from building_blueprints
    into items/actors are already in a v2 read (it lists constraints targeting v2 tables)."""
    if archive['format'] == V2:
        return archive['schema']
    if archive['format'] != V3:
        raise ValueError('schema source is not a v2 or v3 archive')
    tables = set(vault.exporter.TABLES_V2)
    schema = archive['schema']
    return {'columns': [c for c in schema['columns'] if c['table'] in tables],
            'constraints': [c for c in schema['constraints']
                            if c['table'] in tables or (c.get('target') or '').removeprefix('dune.') in tables],
            'types': schema['types']}


def columns(schema):
    return {(c['table'], c['name']): c for c in schema['columns']}


def check_schemas(source, target):
    """Refuse unless the v1 -> v2 difference is exactly the reviewed one."""
    old, new = columns(source), columns(target)
    if set(old) - set(new) != DROPPED:
        raise ValueError('unreviewed dropped columns: %s' % sorted(set(old) - set(new) - DROPPED))
    added = set(new) - set(old)
    if added != set(ADDED) | {('actors', 'state')}:
        raise ValueError('unreviewed added columns: %s' % sorted(added ^ (set(ADDED) | {('actors', 'state')})))
    if (any(new[key]['not_null'] is not not_null or new[key]['type'] != 'bigint' for key, (_, not_null) in ADDED.items())
            or new[('actors', 'state')]['type'] != 'dune.actorstate' or not new[('actors', 'state')]['not_null']):
        raise ValueError('a new column is not the reviewed shape')
    kept = [key for key in old if key in new]
    changed = [key for key in kept if (old[key]['type'], old[key]['not_null']) != (new[key]['type'], new[key]['not_null'])]
    if changed:
        raise ValueError('column type or nullability changed: %s' % changed)
    types_old = {t['name']: t for t in source['types']}
    types_new = {t['name']: t for t in target['types']}
    if set(types_old) != set(types_new):
        raise ValueError('custom type set changed')
    for name, item in types_old.items():
        other = types_new[name]
        if name == 'actorstate':
            if item['labels'] != other['labels'][:len(item['labels'])]:
                raise ValueError('actorstate labels were reordered or removed')
        elif item != other:
            raise ValueError('custom type %s changed' % name)
    tables = set(vault.exporter.TABLES_V2)
    own = lambda schema: sorted(json.dumps(c, sort_keys=True) for c in schema['constraints'] if c['table'] in tables)
    if own(source) != own(target):
        raise ValueError('constraints on the captured tables changed')
    return types_old['actorstate']['labels']


def translate(source, target_schema, provenance):
    """(v2 archive dict, report). `source` is a validated v1 archive, parsed with Decimal."""
    if source.get('format') != V1:
        raise ValueError('source is not a v1 archive')
    labels = check_schemas(source['schema'], target_schema)
    tables = {name: [dict(row) for row in rows] for name, rows in source['tables'].items()}
    states = {}
    for row in tables.pop('actor_state'):
        if row['actor_id'] in states or row['state'] not in labels:
            raise ValueError('invalid actor_state row')
        states[row['actor_id']] = row['state']
    actor_ids = {row['id'] for row in tables['actors']}
    if set(states) - actor_ids:
        raise ValueError('actor_state row for an uncaptured actor')
    for row in tables['actors']:
        row['state'] = states.get(row['id'], 'Default')
    for row in tables['building_instances']:
        if any(row[name] != 0 for name in STABILIZATION):
            raise ValueError('a piece carries stabilization state; dropping it would lose data')
        for name in STABILIZATION:
            del row[name]
    for (table, name), (value, _) in ADDED.items():
        for row in tables[table]:
            row[name] = value
    result = {key: value for key, value in source.items() if key not in ('tables', 'counts', 'schema', 'format')}
    result.update(format=V2, schema=target_schema, tables=tables,
                  counts={name: len(rows) for name, rows in tables.items()},
                  translation={'from_format': V1, **provenance,
                               'rules': ['actor_state -> actors.state (absent = Default)',
                                         'building_instances stabilization columns dropped (all 0)',
                                         'last_placed_by_player_id = 0, edited_by_player_id = NULL (the 1.5.3 migration values)',
                                         'actorstate enum labels appended']})
    report = {'actors': len(tables['actors']), 'non_default_states': len(states),
              'pieces': len(tables['building_instances']), 'counts': result['counts']}
    return result, report


class _Exact(float):
    """A Decimal carried through the JSON encoder as its own source text."""
    def __new__(cls, value):
        number = super().__new__(cls, value)
        number.text = str(value)
        return number


def encode(data):
    """Numbers out exactly as they came in: no float round trip for any value."""
    def walk(value):
        if isinstance(value, Decimal):
            return _Exact(value)
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    def refuse(value):
        raise TypeError('unserialisable ' + type(value).__name__)
    # The pure-Python iterencode takes the float formatter as an argument.
    iterencode = json.encoder._make_iterencode(
        {}, refuse, json.encoder.encode_basestring, None, lambda number: number.text,
        ':', ',', False, False, True)
    return ''.join(iterencode(walk(data), 0)).encode()


def self_check(source, raw):
    """Every value the translation did not touch must come back identical (as Decimal)."""
    data, _ = vault.validate(raw)
    touched = {('actors', 'state')} | set(ADDED)
    for table, rows in data['tables'].items():
        if len(rows) != len(source['tables'][table]):
            raise ValueError('row count changed: ' + table)
        for old, new in zip(source['tables'][table], rows):
            for key, value in new.items():
                if (table, key) in touched:
                    continue
                if old[key] != value:
                    raise ValueError('value changed in %s.%s' % (table, key))
    return data


def main():
    parser = argparse.ArgumentParser(description='Translate a v1 base archive into a new v2 archive. Never changes the source.')
    parser.add_argument('--v1', required=True, help='snapshot id of the verified v1 archive')
    parser.add_argument('--schema-from', required=True, help='snapshot id of a verified v2 or v3 archive (target schema)')
    parser.add_argument('--ingest', action='store_true', help='write the translated archive into the vault')
    parser.add_argument('--label')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('operator-only translation')
    os.umask(0o077)
    store = vault.Vault(vault.ROOT)
    manifest, raw = store.verify(args.v1)
    source, _ = vault.validate(raw)
    _, target_raw = store.verify(args.schema_from)
    target, _ = vault.validate(target_raw)
    result, report = translate(source, v2_schema(target), {
        'from_snapshot_id': args.v1, 'from_payload_sha256': manifest['payload_sha256'],
        'schema_from_snapshot_id': args.schema_from})
    encoded = encode(result)
    self_check(source, encoded)
    report.update(status='translated_dry_run', payload_sha256=hashlib.sha256(encoded).hexdigest(),
                  payload_bytes=len(encoded), source=args.v1)
    if args.ingest:
        label = args.label or ('v2 translation of %s (%s)' % (args.v1[:8], manifest['label']))[:200]
        new = store.ingest(encoded, label)
        report.update(status='translated_and_ingested', snapshot_id=new['snapshot_id'])
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'status': 'translate_failed', 'error_class': type(error).__name__, 'reason': str(error)[:300]}))
        raise SystemExit(1)
