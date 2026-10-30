#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import sys

NAMESPACE = os.environ.get('LASTSIETCH_GAME_NAMESPACE', '')
DATABASE_POD = os.environ.get('LASTSIETCH_GAME_BATTLEGROUP', '') + '-db-dbdepl-sts-0'
# Archive layouts. v1 is the pre-1.5.3 game schema (actor state in its own table);
# 1.5.3 dropped dune.actor_state and moved the state onto dune.actors.state, so v2
# captures 17 tables. v3 adds the building-blueprint graph: a blueprint item in one of
# the base's containers owns a building_blueprints row (item_id -> items.id), which v2
# cannot carry and so refuses as an incoming row. The exporter always writes the CURRENT
# layout; readers pick the table set from each archive's own format, so v1 and v2
# archives stay verifiable.
TABLES_V1 = ('actors', 'fgl_entities', 'actor_fgl_entities', 'actor_state', 'buildings',
             'building_instances', 'placeables', 'totems', 'inventories', 'actor_inventories',
             'items', 'permission_actor', 'permission_actor_rank', 'landclaim_segments',
             'tax_invoice', 'travel_actor_parent', 'player_respawn_locations', 'sinkcharts')
TABLES_V2 = tuple(table for table in TABLES_V1 if table != 'actor_state')
# Tuple order is the restore insert order: items (in v2) before building_blueprints before
# its three children. Undo is the reverse, and the children cascade from their parent.
BLUEPRINT_TABLES = ('building_blueprints', 'building_blueprint_instances',
                    'building_blueprint_pentashields', 'building_blueprint_placeables')
TABLES_V3 = TABLES_V2 + BLUEPRINT_TABLES
# A packed native BRT backup (actors in state BaseBackup, linked by base_backups.id): the
# v2 graph plus its header and link rows. Captured as-is; never changed by capture. v3 does
# not change it: a packed base holding a blueprint item still refuses on incoming coverage,
# and carrying blueprints there needs a new native format name, not a new meaning for v1.
NATIVE_FORMAT = 'lastsietch-native-backup-v1'
NATIVE_TABLES = TABLES_V2 + ('base_backups', 'base_backup_linked_actors')
FORMAT_V2 = 'lastsietch-base-snapshot-v2'
FORMAT_V3 = 'lastsietch-base-snapshot-v3'
LAYOUTS = {'lastsietch-base-snapshot-v1': TABLES_V1, FORMAT_V2: TABLES_V2, FORMAT_V3: TABLES_V3,
           NATIVE_FORMAT: NATIVE_TABLES}
# to_jsonb drops array lower bounds. Game-written rows carry 0- or 1-based arrays (16 live
# backups have 1-based piece transforms), so an archive that records bounds keeps the lower
# bound of every captured row, keyed by the listed columns.
ARRAY_BOUNDS = (('building_instances', ('building_id', 'instance_id'), 'transform'),
                ('totems', ('id',), 'landclaim_original_global_location'))
BLUEPRINT_ARRAY_BOUNDS = (('building_blueprint_instances', ('building_blueprint_id', 'instance_id'), 'transform'),
                          ('building_blueprint_placeables', ('building_blueprint_id', 'placeable_id'), 'transform'),
                          ('building_blueprint_pentashields', ('building_blueprint_id', 'placeable_id'), 'scale'))
# The formats that carry an array_bounds section, and the columns it must cover.
LAYOUT_ARRAY_BOUNDS = {FORMAT_V3: ARRAY_BOUNDS + BLUEPRINT_ARRAY_BOUNDS, NATIVE_FORMAT: ARRAY_BOUNDS}
# Checks a v3 live capture adds to the v1/v2 set.
BLUEPRINT_CHECKS = ('no_external_blueprint_players', 'no_unselected_blueprint_dependents')
FORMAT = FORMAT_V3
TABLES = LAYOUTS[FORMAT]
MAX_BYTES = 72 * 1024 * 1024
MAX_ROWS = 100000


def sql_read(sql):
    request = ("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
               "SET LOCAL statement_timeout='60s'; SET LOCAL lock_timeout='2s';\n"
               "SET LOCAL work_mem='16MB'; SET LOCAL temp_file_limit='256MB';\n"
               "SET LOCAL extra_float_digits=3;\n" + sql + '\nROLLBACK;\n')
    result = subprocess.run(['kubectl', 'exec', '-i', '-n', NAMESPACE, DATABASE_POD, '--',
        'sh', '-c', 'PGPASSWORD=$POSTGRES_PASSWORD exec psql -h localhost -p 15432 -U postgres -d dune -X -q -t -A -v ON_ERROR_STOP=1'],
        input=request.encode(), capture_output=True, timeout=75)
    if result.returncode or len(result.stdout) > MAX_BYTES:
        raise RuntimeError('read-only export query failed or exceeded bound')
    return result.stdout.strip()


def metadata_query(tables=None):
    names = ','.join("'" + name + "'" for name in (tables or TABLES))
    return f"""SELECT jsonb_build_object(
 'columns',(SELECT jsonb_agg(jsonb_build_object('table',c.relname,'name',a.attname,
   'type',format_type(a.atttypid,a.atttypmod),'not_null',a.attnotnull,'generated',a.attgenerated,
   'identity',a.attidentity) ORDER BY c.relname,a.attnum)
   FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_attribute a ON a.attrelid=c.oid
   WHERE n.nspname='dune' AND c.relname IN ({names}) AND a.attnum>0 AND NOT a.attisdropped),
 'constraints',(SELECT jsonb_agg(jsonb_build_object('table',c.relname,'name',k.conname,'kind',k.contype,
   'columns',(SELECT jsonb_agg(a.attname ORDER BY u.ordinality) FROM unnest(k.conkey) WITH ORDINALITY u(attnum,ordinality) JOIN pg_attribute a ON a.attrelid=k.conrelid AND a.attnum=u.attnum),
   'target',CASE WHEN k.contype='f' THEN k.confrelid::regclass::text END,
   'target_columns',(SELECT jsonb_agg(a.attname ORDER BY u.ordinality) FROM unnest(k.confkey) WITH ORDINALITY u(attnum,ordinality) JOIN pg_attribute a ON a.attrelid=k.confrelid AND a.attnum=u.attnum),
   'definition',pg_get_constraintdef(k.oid)) ORDER BY c.relname,k.conname)
   FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname='dune' AND k.contype IN ('p','u','c','f') AND (c.relname IN ({names}) OR
     k.confrelid IN (SELECT x.oid FROM pg_class x JOIN pg_namespace y ON y.oid=x.relnamespace WHERE y.nspname='dune' AND x.relname IN ({names})))),
 'types',(SELECT jsonb_agg(jsonb_build_object('name',t.typname,'kind',t.typtype,
   'labels',(SELECT jsonb_agg(enumlabel ORDER BY enumsortorder) FROM pg_enum WHERE enumtypid=t.oid),
   'attributes',(SELECT jsonb_agg(jsonb_build_object('name',a.attname,'type',format_type(a.atttypid,a.atttypmod)) ORDER BY a.attnum)
     FROM pg_attribute a WHERE a.attrelid=t.typrelid AND a.attnum>0 AND NOT a.attisdropped)) ORDER BY t.typname)
   FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname='dune' AND t.typname IN ('actorstate','vector','quaternion','transform'))
 )"""


def valid_id(value, what):
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError('invalid ' + what)
    return value


def selection_ctes(totem):
    valid_id(totem, 'totem')
    return f"""root AS MATERIALIZED (SELECT a.* FROM dune.actors a JOIN dune.totems t ON t.id=a.id WHERE a.id={totem}),
 root_entities AS MATERIALIZED (SELECT entity_id FROM dune.actor_fgl_entities WHERE actor_id={totem} AND slot_name='Actor' LIMIT 2),
 selected_pieces AS MATERIALIZED (SELECT b.* FROM dune.building_instances b WHERE owner_entity_id IN (SELECT entity_id FROM root_entities) LIMIT 50001),
 selected_placeable_ids AS MATERIALIZED (SELECT id FROM dune.placeables WHERE owner_entity_id IN (SELECT entity_id FROM root_entities) OR id={totem} LIMIT 10001),
 actor_ids AS MATERIALIZED (SELECT {totem}::bigint id UNION SELECT id FROM selected_placeable_ids UNION SELECT building_id FROM selected_pieces),
 """ + graph_ctes()


def native_selection_ctes(backup):
    """Rooted at base_backups.id: the linked actors ARE the base (Funcom's own key), and
    pieces follow building_id as base_backup_get_data does."""
    valid_id(backup, 'backup')
    return f"""header AS MATERIALIZED (SELECT h.* FROM dune.base_backups h WHERE h.id={backup}),
 links AS MATERIALIZED (SELECT l.* FROM dune.base_backup_linked_actors l WHERE l.id={backup} LIMIT 10001),
 actor_ids AS MATERIALIZED (SELECT DISTINCT actor_id AS id FROM links WHERE actor_id IS NOT NULL),
 root AS MATERIALIZED (SELECT a.* FROM dune.actors a JOIN dune.totems t ON t.id=a.id WHERE a.id IN (SELECT id FROM actor_ids)),
 root_entities AS MATERIALIZED (SELECT entity_id FROM dune.actor_fgl_entities WHERE actor_id IN (SELECT id FROM root) AND slot_name='Actor' LIMIT 2),
 selected_pieces AS MATERIALIZED (SELECT b.* FROM dune.building_instances b WHERE building_id IN (SELECT id FROM actor_ids) LIMIT 50001),
 selected_base_backups AS MATERIALIZED (SELECT * FROM header),
 selected_base_backup_linked_actors AS MATERIALIZED (SELECT * FROM links),
 """ + graph_ctes()


def graph_ctes():
    return """entity_ids AS MATERIALIZED (SELECT entity_id FROM dune.actor_fgl_entities WHERE actor_id IN (SELECT id FROM actor_ids)),
 inventory_ids(id) AS (SELECT id FROM dune.inventories WHERE actor_id IN (SELECT id FROM actor_ids)
   UNION SELECT child.id FROM inventory_ids parent JOIN dune.items item ON item.inventory_id=parent.id JOIN dune.inventories child ON child.item_id=item.id),
 item_ids AS MATERIALIZED (SELECT id FROM dune.items WHERE inventory_id IN (SELECT id FROM inventory_ids)),
 selected_actors AS MATERIALIZED (SELECT a.* FROM dune.actors a WHERE id IN (SELECT id FROM actor_ids)),
 selected_fgl_entities AS MATERIALIZED (SELECT t.* FROM dune.fgl_entities t WHERE entity_id IN (SELECT entity_id FROM entity_ids)),
 selected_actor_fgl_entities AS MATERIALIZED (SELECT t.* FROM dune.actor_fgl_entities t WHERE actor_id IN (SELECT id FROM actor_ids)),
 selected_buildings AS MATERIALIZED (SELECT t.* FROM dune.buildings t WHERE id IN (SELECT id FROM actor_ids)),
 selected_building_instances AS MATERIALIZED (SELECT * FROM selected_pieces),
 selected_placeables AS MATERIALIZED (SELECT t.* FROM dune.placeables t WHERE id IN (SELECT id FROM actor_ids)),
 selected_totems AS MATERIALIZED (SELECT t.* FROM dune.totems t WHERE id IN (SELECT id FROM actor_ids)),
 selected_inventories AS MATERIALIZED (SELECT t.* FROM dune.inventories t WHERE id IN (SELECT id FROM inventory_ids)),
 selected_actor_inventories AS MATERIALIZED (SELECT t.* FROM dune.actor_inventories t WHERE inventory_id IN (SELECT id FROM inventory_ids)),
 selected_items AS MATERIALIZED (SELECT t.* FROM dune.items t WHERE id IN (SELECT id FROM item_ids)),
 selected_permission_actor AS MATERIALIZED (SELECT t.* FROM dune.permission_actor t WHERE actor_id IN (SELECT id FROM actor_ids)),
 selected_permission_actor_rank AS MATERIALIZED (SELECT t.* FROM dune.permission_actor_rank t WHERE permission_actor_id IN (SELECT id FROM actor_ids)),
 selected_landclaim_segments AS MATERIALIZED (SELECT t.* FROM dune.landclaim_segments t WHERE totem_id IN (SELECT id FROM actor_ids)),
 selected_tax_invoice AS MATERIALIZED (SELECT t.* FROM dune.tax_invoice t WHERE totem_id IN (SELECT id FROM actor_ids)),
 selected_travel_actor_parent AS MATERIALIZED (SELECT t.* FROM dune.travel_actor_parent t WHERE id IN (SELECT id FROM actor_ids)),
 selected_player_respawn_locations AS MATERIALIZED (SELECT t.* FROM dune.player_respawn_locations t WHERE locator_actor_id IN (SELECT id FROM actor_ids)),
 selected_sinkcharts AS MATERIALIZED (SELECT t.* FROM dune.sinkcharts t WHERE item_id IN (SELECT id FROM item_ids)),
 selected_building_blueprints AS MATERIALIZED (SELECT t.* FROM dune.building_blueprints t WHERE item_id IN (SELECT id FROM item_ids)),
 selected_building_blueprint_instances AS MATERIALIZED (SELECT t.* FROM dune.building_blueprint_instances t WHERE building_blueprint_id IN (SELECT id FROM selected_building_blueprints)),
 selected_building_blueprint_pentashields AS MATERIALIZED (SELECT t.* FROM dune.building_blueprint_pentashields t WHERE building_blueprint_id IN (SELECT id FROM selected_building_blueprints)),
 selected_building_blueprint_placeables AS MATERIALIZED (SELECT t.* FROM dune.building_blueprint_placeables t WHERE building_blueprint_id IN (SELECT id FROM selected_building_blueprints))"""


def query_parts(schema, tables):
    """(incoming FK coverage, row union, schema literal, table-name VALUES) for a table set."""
    coverage = []
    for fk in schema['constraints']:
        if fk['kind'] != 'f' or fk['table'] in tables:
            continue
        target = (fk.get('target') or '').removeprefix('dune.')
        if target not in tables:
            continue
        if len(fk['columns']) != 1 or len(fk['target_columns']) != 1:
            raise ValueError('unsupported incoming composite foreign key')
        table, column, key = fk['table'], fk['columns'][0], fk['target_columns'][0]
        if any(not re.fullmatch('[a-z_][a-z0-9_]*', name) for name in (table,column,key)):
            raise ValueError('unsafe schema identifier')
        constraint_name = fk['name'].replace("'", "''")
        coverage.append(f"SELECT '{constraint_name}' AS name,count(*) AS count FROM dune.\"{table}\" WHERE \"{column}\" IN (SELECT \"{key}\" FROM selected_{target})")
    rows = '\n UNION ALL '.join(f"SELECT '{table}' AS table_name,to_jsonb(t) AS data FROM selected_{table} t" for table in tables)
    coverage_sql = '\n UNION ALL '.join(coverage) or "SELECT 'none' AS name,0::bigint AS count"
    schema_literal = json.dumps(schema,ensure_ascii=True,separators=(',',':')).replace("'", "''")
    table_names = ','.join("('" + table + "')" for table in tables)
    return coverage_sql, rows, schema_literal, table_names


def export_query(totem, owner, schema):
    valid_id(owner, 'expected owner')
    coverage_sql, rows, schema_literal, table_names = query_parts(schema, TABLES)
    return f"""WITH RECURSIVE {selection_ctes(totem)},
 actual_schema AS ({metadata_query()}),
 incoming_coverage AS MATERIALIZED ({coverage_sql}),
 rows AS MATERIALIZED ({rows}),
 table_names(table_name) AS (VALUES {table_names}),
 summary AS (SELECT count(*) AS count,coalesce(sum(octet_length(data::text)),0) AS bytes FROM rows),
 checks AS (SELECT jsonb_build_object(
   'root_exists',(SELECT count(*)=1 FROM root),
   'expected_owner',(SELECT count(*)=1 AND bool_and(player_id={owner}) FROM selected_permission_actor_rank WHERE permission_actor_id={totem} AND rank=1),
   'single_root_entity',(SELECT count(*)=1 FROM root_entities),
   'single_totem',(SELECT count(*)=1 FROM selected_totems),
   'not_packed',NOT EXISTS(SELECT 1 FROM selected_actors WHERE state='BaseBackup'),
   'one_location',(SELECT count(DISTINCT (map,partition_id,dimension_index))=1 FROM selected_actors),
   'no_shared_entities',NOT EXISTS(SELECT 1 FROM dune.actor_fgl_entities WHERE entity_id IN (SELECT entity_id FROM entity_ids) AND actor_id NOT IN (SELECT id FROM actor_ids)),
   'no_shared_building_actor',NOT EXISTS(SELECT 1 FROM dune.building_instances WHERE building_id IN (SELECT building_id FROM selected_pieces) AND owner_entity_id IS DISTINCT FROM (SELECT entity_id FROM root_entities LIMIT 1)),
   'no_unselected_owned_placeables',NOT EXISTS(SELECT 1 FROM dune.placeables WHERE owner_entity_id IN (SELECT entity_id FROM entity_ids) AND id NOT IN (SELECT id FROM actor_ids)),
   'no_unselected_owned_pieces',NOT EXISTS(SELECT 1 FROM dune.building_instances WHERE owner_entity_id IN (SELECT entity_id FROM entity_ids) AND owner_entity_id NOT IN (SELECT entity_id FROM root_entities)),
   'no_unselected_travel_children',NOT EXISTS(SELECT 1 FROM dune.travel_actor_parent WHERE parent_id IN (SELECT id FROM actor_ids) AND id NOT IN (SELECT id FROM actor_ids)),
   'no_exchange_or_vehicle_inventory',NOT EXISTS(SELECT 1 FROM selected_inventories WHERE exchange_id IS NOT NULL OR vehicle_module_id IS NOT NULL),
   'no_pending_item_operations',NOT EXISTS(SELECT 1 FROM dune.item_operations_staging_table WHERE inventory_id IN (SELECT id FROM inventory_ids) OR item_id IN (SELECT id FROM item_ids)),
   'no_unsupported_incoming_rows',NOT EXISTS(SELECT 1 FROM incoming_coverage WHERE count>0),
   'no_external_blueprint_players',NOT EXISTS(SELECT 1 FROM selected_building_blueprints WHERE player_id IS NOT NULL AND player_id NOT IN (SELECT id FROM actor_ids)),
   'no_unselected_blueprint_dependents',NOT EXISTS(SELECT 1 FROM dune.building_blueprints WHERE player_id IN (SELECT id FROM actor_ids) AND id NOT IN (SELECT id FROM selected_building_blueprints)),
   'schema_unchanged',(SELECT * FROM actual_schema)='{schema_literal}'::jsonb,
   'bounded',(SELECT count<={MAX_ROWS} AND bytes<=67108864 FROM summary) AND (SELECT count(*)<=10000 FROM actor_ids) AND (SELECT count(*)<=50000 FROM selected_pieces)
 ) AS data),
 valid AS (SELECT NOT EXISTS(SELECT 1 FROM checks,jsonb_each(data) entry WHERE entry.value<>'true'::jsonb) AS ok)
SELECT CASE WHEN (SELECT ok FROM valid) THEN jsonb_build_object(
 'format','{FORMAT}',
 'captured_at',clock_timestamp(),'snapshot_id',pg_current_snapshot()::text,
 'postgres_version',current_setting('server_version'),'transaction_isolation',current_setting('transaction_isolation'),
 'transaction_read_only',current_setting('transaction_read_only'),'consistency','persisted_database_state_not_unsaved_RAM',
 'root_totem_id',{totem},'owner_controller_id',{owner},
 'source_location',(SELECT jsonb_build_object('map',map,'partition_id',partition_id,'dimension_index',dimension_index,'transform',to_jsonb(transform)) FROM root),
 'array_bounds',{array_bounds_sql(LAYOUT_ARRAY_BOUNDS[FORMAT])},
 'schema',(SELECT * FROM actual_schema),'checks',(SELECT data FROM checks),
 'counts',(SELECT jsonb_object_agg(table_name,count) FROM (SELECT names.table_name,count(rows.data) AS count FROM table_names names LEFT JOIN rows USING(table_name) GROUP BY names.table_name) counted),
 'tables',(SELECT jsonb_object_agg(table_name,data) FROM (SELECT names.table_name,coalesce(jsonb_agg(rows.data ORDER BY rows.data::text) FILTER (WHERE rows.data IS NOT NULL),'[]'::jsonb) AS data FROM table_names names LEFT JOIN rows USING(table_name) GROUP BY names.table_name) grouped),
 'external_actor_identities',(SELECT coalesce(jsonb_agg(jsonb_build_object('controller_id',ids.player_id,'account_id',ps.account_id,'character_id',eps.id,'character_name',ps.character_name,'funcom_id',ac.funcom_id) ORDER BY ids.player_id),'[]'::jsonb)
   FROM (SELECT DISTINCT player_id FROM selected_permission_actor_rank) ids LEFT JOIN dune.player_state ps ON ps.player_controller_id=ids.player_id LEFT JOIN dune.encrypted_player_state eps ON eps.player_controller_id=ids.player_id LEFT JOIN dune.accounts ac ON ac.id=ps.account_id)
 ) ELSE jsonb_build_object('format','lastsietch-base-snapshot-refused','checks',(SELECT data FROM checks),
 'incoming_coverage',(SELECT jsonb_agg(to_jsonb(t)) FROM incoming_coverage t),'rows',(SELECT count FROM summary),'bytes',(SELECT bytes FROM summary)) END;"""


NATIVE_CHECKS = ('header_exists', 'expected_owner', 'links_nonempty_distinct', 'all_links_exist', 'all_packed',
                 'linked_once', 'single_totem', 'single_root_entity', 'no_totem_permissions', 'one_parked_location',
                 'no_unlinked_owned_placeables', 'no_unlinked_owned_pieces', 'no_shared_building_actor',
                 'no_shared_entities', 'no_unselected_travel_children', 'no_exchange_or_vehicle_inventory',
                 'no_pending_item_operations', 'no_unsupported_incoming_rows', 'schema_unchanged', 'bounded')
NATIVE_FUNCTIONS = ("SELECT md5(string_agg(pg_get_functiondef(p.oid),E'\\n' ORDER BY p.proname,p.oid)) FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='dune' "
                    "AND (p.proname LIKE 'base\\_backup\\_%' OR p.proname='load_actors')")


def array_bounds_sql(bounds=ARRAY_BOUNDS):
    parts = []
    for table, keys, column in bounds:
        key = "||':'||".join(k + '::text' for k in keys)
        parts.append(f"'{table}',(SELECT coalesce(jsonb_object_agg({key},array_lower({column},1)),'{{}}'::jsonb) "
                     f"FROM selected_{table} WHERE array_lower({column},1) IS NOT NULL)")
    return 'jsonb_build_object(' + ','.join(parts) + ')'


def native_export_query(backup, owner, schema):
    """A packed native backup, as it is. The linked actors must all be BaseBackup, the totem's
    permissions must already be gone (a leftover rank means a half-packed or placed state), and
    nothing outside the link set may own or reference the graph."""
    valid_id(owner, 'expected owner')
    coverage_sql, rows, schema_literal, table_names = query_parts(schema, NATIVE_TABLES)
    return f"""WITH RECURSIVE {native_selection_ctes(backup)},
 actual_schema AS ({metadata_query(NATIVE_TABLES)}),
 incoming_coverage AS MATERIALIZED ({coverage_sql}),
 rows AS MATERIALIZED ({rows}),
 table_names(table_name) AS (VALUES {table_names}),
 summary AS (SELECT count(*) AS count,coalesce(sum(octet_length(data::text)),0) AS bytes FROM rows),
 checks AS (SELECT jsonb_build_object(
   'header_exists',(SELECT count(*)=1 FROM header),
   'expected_owner',(SELECT count(*)=1 AND bool_and(player_id={owner}) FROM header) AND EXISTS(SELECT 1 FROM dune.encrypted_player_state WHERE player_controller_id={owner}),
   'links_nonempty_distinct',(SELECT count(*)>0 AND count(*)<=10000 AND count(*)=count(DISTINCT actor_id) AND bool_and(actor_id IS NOT NULL) FROM links),
   'all_links_exist',(SELECT count(*) FROM actor_ids)=(SELECT count(*) FROM selected_actors),
   'all_packed',NOT EXISTS(SELECT 1 FROM selected_actors WHERE state IS DISTINCT FROM 'BaseBackup'),
   'linked_once',NOT EXISTS(SELECT 1 FROM dune.base_backup_linked_actors WHERE actor_id IN (SELECT id FROM actor_ids) AND id<>{backup}),
   'single_totem',(SELECT count(*)=1 FROM root),
   'single_root_entity',(SELECT count(*)=1 FROM root_entities),
   'no_totem_permissions',NOT EXISTS(SELECT 1 FROM dune.permission_actor_rank WHERE permission_actor_id IN (SELECT id FROM root)) AND NOT EXISTS(SELECT 1 FROM dune.permission_actor WHERE actor_id IN (SELECT id FROM root)),
   'one_parked_location',(SELECT count(DISTINCT (map,partition_id,dimension_index))=1 FROM selected_actors),
   'no_unlinked_owned_placeables',NOT EXISTS(SELECT 1 FROM dune.placeables WHERE owner_entity_id IN (SELECT entity_id FROM entity_ids) AND id NOT IN (SELECT id FROM actor_ids)),
   'no_unlinked_owned_pieces',NOT EXISTS(SELECT 1 FROM dune.building_instances WHERE owner_entity_id IN (SELECT entity_id FROM root_entities) AND building_id NOT IN (SELECT id FROM actor_ids)),
   'no_shared_building_actor',NOT EXISTS(SELECT 1 FROM dune.building_instances WHERE building_id IN (SELECT building_id FROM selected_pieces) AND owner_entity_id IS DISTINCT FROM (SELECT entity_id FROM root_entities LIMIT 1)),
   'no_shared_entities',NOT EXISTS(SELECT 1 FROM dune.actor_fgl_entities WHERE entity_id IN (SELECT entity_id FROM entity_ids) AND actor_id NOT IN (SELECT id FROM actor_ids)),
   'no_unselected_travel_children',NOT EXISTS(SELECT 1 FROM dune.travel_actor_parent WHERE parent_id IN (SELECT id FROM actor_ids) AND id NOT IN (SELECT id FROM actor_ids)),
   'no_exchange_or_vehicle_inventory',NOT EXISTS(SELECT 1 FROM selected_inventories WHERE exchange_id IS NOT NULL OR vehicle_module_id IS NOT NULL),
   'no_pending_item_operations',NOT EXISTS(SELECT 1 FROM dune.item_operations_staging_table WHERE inventory_id IN (SELECT id FROM inventory_ids) OR item_id IN (SELECT id FROM item_ids)),
   'no_unsupported_incoming_rows',NOT EXISTS(SELECT 1 FROM incoming_coverage WHERE count>0),
   'schema_unchanged',(SELECT * FROM actual_schema)='{schema_literal}'::jsonb,
   'bounded',(SELECT count<={MAX_ROWS} AND bytes<=67108864 FROM summary) AND (SELECT count(*)<=10000 FROM actor_ids) AND (SELECT count(*)<=50000 FROM selected_pieces)
 ) AS data),
 valid AS (SELECT NOT EXISTS(SELECT 1 FROM checks,jsonb_each(data) entry WHERE entry.value<>'true'::jsonb) AS ok)
SELECT CASE WHEN (SELECT ok FROM valid) THEN jsonb_build_object(
 'format','{NATIVE_FORMAT}',
 'captured_at',clock_timestamp(),'snapshot_id',pg_current_snapshot()::text,
 'postgres_version',current_setting('server_version'),'transaction_isolation',current_setting('transaction_isolation'),
 'transaction_read_only',current_setting('transaction_read_only'),'consistency','persisted_database_state_not_unsaved_RAM',
 'root_backup_id',{backup},'root_totem_id',(SELECT id FROM root),'owner_controller_id',{owner},
 'location_kind','parked_backup',
 'source_location',(SELECT jsonb_build_object('map',map,'partition_id',partition_id,'dimension_index',dimension_index,'transform',to_jsonb(transform)) FROM root),
 'array_bounds',{array_bounds_sql()},
 'native_function_fingerprint',({NATIVE_FUNCTIONS}),
 'schema',(SELECT * FROM actual_schema),'checks',(SELECT data FROM checks),
 'counts',(SELECT jsonb_object_agg(table_name,count) FROM (SELECT names.table_name,count(rows.data) AS count FROM table_names names LEFT JOIN rows USING(table_name) GROUP BY names.table_name) counted),
 'tables',(SELECT jsonb_object_agg(table_name,data) FROM (SELECT names.table_name,coalesce(jsonb_agg(rows.data ORDER BY rows.data::text) FILTER (WHERE rows.data IS NOT NULL),'[]'::jsonb) AS data FROM table_names names LEFT JOIN rows USING(table_name) GROUP BY names.table_name) grouped),
 'external_actor_identities',(SELECT coalesce(jsonb_agg(jsonb_build_object('controller_id',ids.player_id,'account_id',ps.account_id,'character_id',eps.id,'character_name',ps.character_name,'funcom_id',ac.funcom_id) ORDER BY ids.player_id),'[]'::jsonb)
   FROM (SELECT DISTINCT player_id FROM header) ids LEFT JOIN dune.player_state ps ON ps.player_controller_id=ids.player_id LEFT JOIN dune.encrypted_player_state eps ON eps.player_controller_id=ids.player_id LEFT JOIN dune.accounts ac ON ac.id=ps.account_id)
 ) ELSE jsonb_build_object('format','lastsietch-native-backup-refused','checks',(SELECT data FROM checks),
 'incoming_coverage',(SELECT jsonb_agg(to_jsonb(t)) FROM incoming_coverage t),'rows',(SELECT count FROM summary),'bytes',(SELECT bytes FROM summary)) END;"""


def main():
    parser = argparse.ArgumentParser(description='Read-only base graph exporter. Never packs or changes a base.')
    root = parser.add_mutually_exclusive_group(required=True)
    root.add_argument('--totem',type=int,help='a live placed base, by totem id')
    root.add_argument('--backup',type=int,help='a packed native BRT backup, by base_backups.id')
    parser.add_argument('--expected-owner',type=int,required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('operator-only exporter')
    if args.backup is not None:
        schema = json.loads(sql_read(metadata_query(NATIVE_TABLES) + ';'))
        raw = sql_read(native_export_query(args.backup,args.expected_owner,schema))
        expected = NATIVE_FORMAT
    else:
        schema = json.loads(sql_read(metadata_query() + ';'))
        raw = sql_read(export_query(args.totem,args.expected_owner,schema))
        expected = FORMAT
    result = json.loads(raw)
    if result.get('format') != expected:
        print(json.dumps(result),file=sys.stderr)
        return 1
    sys.stdout.buffer.write(raw + b'\n')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'status':'export_failed','error_class':type(error).__name__}),file=sys.stderr)
        raise SystemExit(1)
