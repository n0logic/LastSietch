#!/usr/bin/env python3
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

spec = importlib.util.spec_from_file_location('base_vault', Path(__file__).with_name('vault.py'))
vault = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vault)
TYPES = {'bigint','integer','smallint','real','double precision','text','boolean','jsonb','uuid',
         'real[]','integer[]','bigint[]','smallint[]','dune.vector','dune.quaternion','dune.transform','dune.actorstate'}
CHECKS = {'CHECK ((id > 0))','CHECK ((entity_id <> 0))','CHECK ((position_index >= 0))','CHECK ((stack_size > 0))',
          'CHECK (((actor_id IS NOT NULL) OR (exchange_id IS NOT NULL) OR (item_id IS NOT NULL) OR (vehicle_module_id IS NOT NULL)))'}


def ident(name):
    if not isinstance(name,str) or not re.fullmatch('[a-z_][a-z0-9_]*',name):
        raise ValueError('unsafe identifier')
    return '"' + name + '"'


def typename(name):
    if name not in TYPES:
        raise ValueError('unsupported captured column type')
    return name


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def stub_value(column):
    kind=column['type']
    if kind in ('bigint','integer','smallint','real','double precision'): return 0
    if kind=='boolean': return False
    if kind=='jsonb': return {}
    if kind.endswith('[]'): return []
    if kind=='dune.transform': return {'location':{'x':0,'y':0,'z':0},'rotation':{'x':0,'y':0,'z':0,'w':1}}
    if kind=='text': return 'VAULT_LAB_EXTERNAL_REFERENCE'
    if kind=='dune.actorstate': return 'Default'
    raise ValueError('unsupported required external reference column')


def build_sql(raw, include_snapshot=True):
    data, external=vault.validate(raw)
    tables=vault.tables_for(data)
    columns={table:[c for c in data['schema']['columns'] if c['table']==table] for table in tables}
    statements=['BEGIN;', "SET LOCAL statement_timeout='90s'; SET LOCAL lock_timeout='2s'; SET LOCAL extra_float_digits=3;",
                'CREATE SCHEMA dune;', 'CREATE TEMP TABLE vault_payload(data jsonb);',
                "INSERT INTO vault_payload SELECT convert_from(decode('" + raw.hex() + "','hex'),'UTF8')::jsonb;"]
    custom={item['name']:item for item in data['schema']['types']}
    if set(custom)!={'actorstate','vector','quaternion','transform'}: raise ValueError('incomplete custom types')
    for name in ('actorstate','vector','quaternion','transform'):
        item=custom[name]
        if item['kind']=='e':
            statements.append('CREATE TYPE dune.'+ident(name)+' AS ENUM ('+','.join(literal(label) for label in item['labels'])+');')
        elif item['kind']=='c':
            statements.append('CREATE TYPE dune.'+ident(name)+' AS ('+','.join(ident(c['name'])+' '+typename(c['type']) for c in item['attributes'])+');')
        else: raise ValueError('unsupported custom type kind')
    for name,key in (('world_partition','partition_id'),('encrypted_player_state','id'),('dune_exchanges','id'),('vehicle_modules','id')):
        statements.append('CREATE TABLE dune.'+ident(name)+' ('+ident(key)+' bigint PRIMARY KEY);')
        for value in external[name]:
            if type(value) is not int: raise ValueError('external key must be integer')
            statements.append('INSERT INTO dune.'+ident(name)+' VALUES ('+str(value)+');')
    for table in tables:
        statements.append('CREATE TABLE dune.'+ident(table)+' ('+','.join(ident(c['name'])+' '+typename(c['type'])+(' NOT NULL' if c['not_null'] else '') for c in columns[table])+');')
    constraints = sorted(data['schema']['constraints'], key=lambda item: item['kind'] == 'f')
    for constraint in constraints:
        table=constraint['table']
        if table not in tables: continue
        names=','.join(ident(key) for key in constraint['columns'] or [])
        kind=constraint['kind']
        if kind in ('p','u'):
            expression=('PRIMARY KEY' if kind=='p' else 'UNIQUE')+' ('+names+')'
        elif kind=='c':
            if constraint['definition'] not in CHECKS: raise ValueError('unreviewed check constraint')
            expression=constraint['definition']
        elif kind=='f':
            target=constraint['target'].removeprefix('dune.')
            if target not in (*tables,'world_partition','encrypted_player_state','dune_exchanges','vehicle_modules'):
                raise ValueError('unreviewed external table')
            actions = ''
            if constraint['definition']:
                match = re.fullmatch(r'FOREIGN KEY \([a-z0-9_, ]+\) REFERENCES (?:dune\.)?[a-z_][a-z0-9_]*\([a-z0-9_, ]+\)((?: MATCH (?:FULL|PARTIAL|SIMPLE))?(?: ON UPDATE (?:NO ACTION|RESTRICT|CASCADE|SET NULL|SET DEFAULT))?(?: ON DELETE (?:NO ACTION|RESTRICT|CASCADE|SET NULL|SET DEFAULT))?)(?: DEFERRABLE(?: INITIALLY (?:DEFERRED|IMMEDIATE))?)?', constraint['definition'])
                if not match:
                    raise ValueError('unreviewed foreign key definition')
                actions = match.group(1)
            expression='FOREIGN KEY ('+names+') REFERENCES dune.'+ident(target)+' ('+','.join(ident(key) for key in constraint['target_columns'])+')'+actions+' DEFERRABLE INITIALLY DEFERRED'
        else: raise ValueError('unreviewed constraint type')
        statements.append('ALTER TABLE dune.'+ident(table)+' ADD CONSTRAINT '+ident(constraint['name'])+' '+expression+';')
    stubs=[]
    for actor_id in external['actors']:
        row={c['name']:(stub_value(c) if c['not_null'] else None) for c in columns['actors']}
        row['id']=actor_id
        if 'partition_id' in row:
            row['partition_id']=data['source_location']['partition_id']
        stubs.append(row)
    if stubs:
        encoded=json.dumps(stubs).encode().hex()
        statements.append("INSERT INTO dune.actors SELECT * FROM jsonb_populate_recordset(NULL::dune.actors,convert_from(decode('"+encoded+"','hex'),'UTF8')::jsonb);")
    if include_snapshot:
        for table in tables:
            statements.append('INSERT INTO dune.'+ident(table)+" SELECT * FROM jsonb_populate_recordset(NULL::dune."+ident(table)+",(SELECT data->'tables'->"+literal(table)+' FROM vault_payload));')
    statements.append('SET CONSTRAINTS ALL IMMEDIATE;')
    comparisons=[]
    for table in tables:
        where=" WHERE t.id IN (SELECT (value->>'id')::bigint FROM vault_payload,jsonb_array_elements(data->'tables'->'actors'))" if table=='actors' else ''
        comparisons.append('SELECT '+literal(table)+" AS table_name,(SELECT data->'tables'->"+literal(table)+" FROM vault_payload)=coalesce((SELECT jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text) FROM dune."+ident(table)+' t'+where+"),'[]'::jsonb) AS exact_match")
    if include_snapshot:
        statements.append("SELECT jsonb_build_object('exact_table_roundtrip',bool_and(exact_match),'tables_checked',count(*),'results',jsonb_object_agg(table_name,exact_match)) FROM ("+' UNION ALL '.join(comparisons)+') results;')
    statements.append('COMMIT;')
    return '\n'.join(statements).encode(), {key:len(value) for key,value in external.items()}


def inspect_lab(name):
    if not re.fullmatch('ls-base-vault-check-[a-z0-9-]+',name): raise ValueError('not a vault lab container')
    result=subprocess.run(['docker','inspect',name],capture_output=True,check=True,timeout=10)
    info=json.loads(result.stdout)[0]
    if (info['HostConfig']['NetworkMode']!='none' or info['HostConfig'].get('PortBindings')
            or info['HostConfig'].get('Privileged') or info['HostConfig']['Memory']>1073741824
            or info['HostConfig']['Memory']<=0 or not info['State']['Running']
            or info['Config'].get('Labels',{}).get('lastsietch.role')!='base-vault-lab'
            or any(mount['Type']!='tmpfs' for mount in info['Mounts'])
            or info['Config']['Image']!='sha256:979c4379dd698aba0b890599a6104e082035f98ef31d9b9291ec22f2b13059ca'):
        raise ValueError('container isolation or image mismatch')
    return info['Id']


def main():
    parser=argparse.ArgumentParser(description='Validate a vault snapshot only in a labelled, isolated disposable PostgreSQL container.')
    parser.add_argument('--snapshot',required=True)
    parser.add_argument('--container',required=True)
    args=parser.parse_args()
    if os.geteuid()!=0: raise ValueError('operator-only lab validation')
    store=vault.Vault(vault.ROOT)
    manifest,raw=store.verify(args.snapshot)
    container=inspect_lab(args.container)
    sql,stubs=build_sql(raw)
    table_count=len(vault.tables_for(vault.parse(raw)))
    result=subprocess.run(['docker','exec','-i',container,'psql','-X','-q','-t','-A','-U','postgres','-d','vaultcheck','-v','ON_ERROR_STOP=1'],
                          input=sql,capture_output=True,timeout=120)
    if result.returncode or len(result.stdout)>65536:
        error_id = str(uuid.uuid4())
        vault.write_new(store.root / 'validations' / (error_id + '.stderr'), result.stderr[:1048576])
        raise RuntimeError('isolated reconstruction failed; bounded error retained in protected validations directory')
    report=json.loads(result.stdout)
    if report.get('exact_table_roundtrip') is not True or report.get('tables_checked')!=table_count:
        raise ValueError('reconstructed data differs')
    report.update({'status':'isolated_database_roundtrip_verified','external_ref_stub_counts':stubs,
                   'production_restore_enabled':False,'in_game_spawn_tested':False,
                   'validation_scope':'captured_column_types_keys_checks_and_foreign_keys',
                   'source_payload_sha256':manifest['payload_sha256']})
    print(json.dumps(store.record_validation(args.snapshot,report),indent=2))


if __name__=='__main__':
    try: main()
    except Exception as error:
        print(json.dumps({'status':'lab_validation_failed','error_class':type(error).__name__}))
        raise SystemExit(1)
