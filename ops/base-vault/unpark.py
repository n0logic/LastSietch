#!/usr/bin/env python3
"""Deep Desert unpark (VAULT-04 mode A): return a parked native backup to its original
Deep Desert partition, the proven capture-based replant of ops/dd-base-replant/README.md.

Inputs are two VERIFIED vault archives:
  * the native capture of the packed backup (what is parked now), and
  * a live-base snapshot of the same totem taken while it stood in the Deep Desert: the only
    record of its original partition/dimension and of the totem permissions the pack
    destroyed (all five permission_actor columns and every rank row).

The transaction moves every linked actor back (map, partition, dimension; transforms are
untouched world coordinates), sets them Default, restores the totem permissions and deletes
the backup header (links cascade). It re-checks inside the transaction that the live backup
still IS the captured one, and reads back before committing. It must run while the target
Deep Desert pod is DOWN: a running partition never re-reads the DB, so a write against a
live pod is inert until its next load. `plan` never touches the game database.

Usage (root, <web-host>):
  unpark.py plan --native UUID --origin UUID      -> writes <out>/unpark-<backup>.{json,sql,dryrun.sql}
The .sql ends in COMMIT, the .dryrun.sql in ROLLBACK; both are run by the window lane.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re

spec = importlib.util.spec_from_file_location('base_vault', Path(__file__).with_name('vault.py'))
vault = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vault)

# Deep Desert survey grid, mirrored from admin-backend/map_model.py (pinned equal by test).
DD_CAL = {'originX': -1270000.0, 'spanX': 2438400.0, 'originY': -1270000.0, 'spanY': 2438400.0}
# Coriolis regenerates terrain past the shield wall; only the A row keeps its ground, and the
# loader does not re-settle a base to the ground (README "Why the A row specifically").
STABLE_ROWS = ('A',)
DD_PARTITION_MAPS = {'DeepDesert': 'DeepDesert_1'}
MIN_OVERLAP = 0.9
PERMISSION_COLUMNS = ('actor_id', 'actor_name', 'actor_type', 'access_level', 'is_child')
RANK_COLUMNS = ('permission_actor_id', 'player_id', 'rank')


def sector(x, y):
    cell = DD_CAL['spanX'] / 9
    col = max(1, min(9, int((x - DD_CAL['originX']) / cell) + 1))
    row = max(0, min(8, int((DD_CAL['originY'] + DD_CAL['spanY'] - y) / cell)))
    return chr(ord('A') + row) + str(col)


def literal(value):
    if value is None:
        return 'NULL'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if type(value) is int:
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    raise ValueError('unsupported permission value')


def make_plan(native, origin):
    """Pure: both archives already validated. Returns the unpark plan or raises."""
    if native.get('format') != vault.NATIVE_FORMAT:
        raise ValueError('first archive is not a native backup capture')
    if origin.get('format') not in ('lastsietch-base-snapshot-v1', 'lastsietch-base-snapshot-v2', 'lastsietch-base-snapshot-v3'):
        raise ValueError('origin archive is not a live-base snapshot')
    totem, owner = native['root_totem_id'], native['owner_controller_id']
    if origin['root_totem_id'] != totem or origin['owner_controller_id'] != owner:
        raise ValueError('origin snapshot is for a different totem or owner')
    linked = sorted(row['actor_id'] for row in native['tables']['base_backup_linked_actors'])
    origin_ids = {row['id'] for row in origin['tables']['actors']}
    if len(set(linked) & origin_ids) < MIN_OVERLAP * len(linked):
        raise ValueError('origin snapshot does not describe this backup (actor overlap too low)')
    location = origin['source_location']
    game_map, partition, dimension = location['map'], location['partition_id'], location['dimension_index']
    if game_map not in DD_PARTITION_MAPS:
        raise ValueError('origin is not a Deep Desert location')
    root = next(row for row in origin['tables']['actors'] if row['id'] == totem)
    point = root['transform']['location']
    cell = sector(float(point['x']), float(point['y']))
    if cell[0] not in STABLE_ROWS:
        raise ValueError('origin sector ' + cell + ' is outside the terrain-stable A row')
    permission = [row for row in origin['tables']['permission_actor'] if row['actor_id'] == totem]
    ranks = sorted((row for row in origin['tables']['permission_actor_rank'] if row['permission_actor_id'] == totem),
                   key=lambda row: (row['rank'], row['player_id']))
    if len(permission) != 1 or set(permission[0]) < set(PERMISSION_COLUMNS):
        raise ValueError('origin snapshot lacks the totem permission row')
    if not any(row['rank'] == 1 and row['player_id'] == owner for row in ranks):
        raise ValueError('origin snapshot lacks the owner rank')
    return {'format': 'lastsietch-unpark-plan-v1', 'backup_id': native['root_backup_id'], 'totem_id': totem,
            'owner_controller_id': owner, 'linked_actor_ids': linked,
            'linked_digest': hashlib.sha256(','.join(map(str, linked)).encode()).hexdigest(),
            'counts': {table: native['counts'][table] for table in ('actors', 'building_instances', 'placeables', 'inventories', 'items')},
            'target': {'map': game_map, 'partition_map': DD_PARTITION_MAPS[game_map], 'partition_id': partition,
                       'dimension_index': dimension, 'sector': cell},
            'permission_actor': {key: permission[0][key] for key in PERMISSION_COLUMNS},
            'ranks': [{key: row[key] for key in RANK_COLUMNS} for row in ranks]}


def unpark_sql(plan, commit=True):
    backup, totem, owner = plan['backup_id'], plan['totem_id'], plan['owner_controller_id']
    target = plan['target']
    for value in (backup, totem, owner, target['partition_id'], target['dimension_index']):
        if type(value) is not int or value < 0:
            raise ValueError('invalid plan identity')
    if not re.fullmatch('[A-Za-z0-9_]{1,80}', target['map']) or not re.fullmatch('[A-Za-z0-9_]{1,80}', target['partition_map']):
        raise ValueError('invalid plan map')
    ids = plan['linked_actor_ids']
    if not ids or any(type(i) is not int or i <= 0 for i in ids) or totem not in ids:
        raise ValueError('invalid linked actor set')
    id_list = ','.join(str(i) for i in ids)
    n = len(ids)
    players = sorted({rank['player_id'] for rank in plan['ranks']})
    perm = plan['permission_actor']
    permission_values = ','.join(literal(perm[key]) for key in PERMISSION_COLUMNS)
    rank_values = ','.join('(' + ','.join(literal(rank[key]) for key in RANK_COLUMNS) + ')' for rank in plan['ranks'])
    guard = f"""DO $$ BEGIN
  IF (SELECT count(*) FROM dune.base_backups WHERE id={backup} AND player_id={owner}) <> 1 THEN RAISE EXCEPTION 'unpark_header_changed'; END IF;
  IF (SELECT md5(string_agg(actor_id::text, ',' ORDER BY actor_id)) FROM dune.base_backup_linked_actors WHERE id={backup})
     IS DISTINCT FROM md5('{','.join(str(i) for i in ids)}') THEN RAISE EXCEPTION 'unpark_links_changed'; END IF;
  IF EXISTS (SELECT 1 FROM dune.actors WHERE id IN ({id_list}) AND state IS DISTINCT FROM 'BaseBackup')
     OR (SELECT count(*) FROM dune.actors WHERE id IN ({id_list})) <> {n} THEN RAISE EXCEPTION 'unpark_actors_changed'; END IF;
  IF EXISTS (SELECT 1 FROM dune.permission_actor WHERE actor_id={totem})
     OR EXISTS (SELECT 1 FROM dune.permission_actor_rank WHERE permission_actor_id={totem}) THEN RAISE EXCEPTION 'unpark_totem_has_permissions'; END IF;
  IF (SELECT count(*) FROM dune.actors WHERE id IN ({','.join(map(str, players))})) <> {len(players)} THEN RAISE EXCEPTION 'unpark_rank_player_missing'; END IF;
  IF NOT EXISTS (SELECT 1 FROM dune.world_partition WHERE partition_id={target['partition_id']}
                 AND map='{target['partition_map']}' AND dimension_index={target['dimension_index']}) THEN RAISE EXCEPTION 'unpark_target_partition'; END IF;
  IF EXISTS (SELECT 1 FROM dune.world_partition w JOIN dune.farm_state f ON f.server_id=w.server_id
             WHERE w.partition_id={target['partition_id']} AND f.ready AND f.alive AND f.connected_players>0) THEN RAISE EXCEPTION 'unpark_partition_occupied'; END IF;
END $$;"""
    readback = f"""DO $$ BEGIN
  IF (SELECT count(*) FROM dune.actors WHERE id IN ({id_list}) AND state='Default' AND map='{target['map']}'
      AND partition_id={target['partition_id']} AND dimension_index={target['dimension_index']}) <> {n} THEN RAISE EXCEPTION 'unpark_readback_actors'; END IF;
  IF (SELECT count(*) FROM dune.permission_actor_rank WHERE permission_actor_id={totem}) <> {len(plan['ranks'])}
     OR NOT EXISTS (SELECT 1 FROM dune.permission_actor_rank WHERE permission_actor_id={totem} AND player_id={owner} AND rank=1)
     OR (SELECT count(*) FROM dune.permission_actor WHERE actor_id={totem}) <> 1 THEN RAISE EXCEPTION 'unpark_readback_permissions'; END IF;
  IF EXISTS (SELECT 1 FROM dune.base_backups WHERE id={backup}) OR EXISTS (SELECT 1 FROM dune.base_backup_linked_actors WHERE id={backup})
     THEN RAISE EXCEPTION 'unpark_readback_header'; END IF;
  IF (SELECT count(*) FROM dune.building_instances WHERE building_id IN ({id_list})) <> {plan['counts']['building_instances']}
     THEN RAISE EXCEPTION 'unpark_readback_pieces'; END IF;
END $$;"""
    return '\n'.join([
        'BEGIN;',
        "SET LOCAL statement_timeout='60s'; SET LOCAL lock_timeout='5s';",
        'LOCK TABLE dune.base_backups, dune.base_backup_linked_actors, dune.permission_actor, dune.permission_actor_rank IN SHARE ROW EXCLUSIVE MODE;',
        guard,
        f"UPDATE dune.actors SET map='{target['map']}', partition_id={target['partition_id']}, "
        f"dimension_index={target['dimension_index']}, state='Default' WHERE id IN ({id_list});",
        f"INSERT INTO dune.permission_actor ({','.join(PERMISSION_COLUMNS)}) VALUES ({permission_values});",
        f"INSERT INTO dune.permission_actor_rank ({','.join(RANK_COLUMNS)}) VALUES {rank_values};",
        f"DELETE FROM dune.base_backups WHERE id={backup};",
        readback,
        "SELECT 'unpark_verified backup=" + str(backup) + " actors=" + str(n) + " ranks=" + str(len(plan['ranks'])) + "';",
        'COMMIT;' if commit else 'ROLLBACK;',
    ]) + '\n'


def main():
    parser = argparse.ArgumentParser(description='Plan a Deep Desert unpark from two verified vault archives. Never touches the game DB.')
    parser.add_argument('action', choices=('plan',))
    parser.add_argument('--native', required=True)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--out', type=Path, default=Path('/root/base-vault-staging'))
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('operator-only')
    os.umask(0o077)
    store = vault.Vault(vault.ROOT)
    _, native_raw = store.verify(args.native)
    _, origin_raw = store.verify(args.origin)
    native, _ = vault.validate(native_raw)
    origin, _ = vault.validate(origin_raw)
    plan = make_plan(native, origin)
    plan.update(native_snapshot=args.native, origin_snapshot=args.origin)
    base = args.out / ('unpark-' + str(plan['backup_id']))
    for suffix, body in (('.json', json.dumps(plan, indent=2, sort_keys=True)), ('.sql', unpark_sql(plan)),
                         ('.dryrun.sql', unpark_sql(plan, commit=False))):
        vault.write_new(Path(str(base) + suffix), body.encode())
    summary = {key: plan[key] for key in ('backup_id', 'totem_id', 'owner_controller_id', 'target', 'counts')}
    summary.update(linked_actors=len(plan['linked_actor_ids']), ranks=len(plan['ranks']), files=str(base) + '.{json,sql,dryrun.sql}')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'status': 'unpark_plan_failed', 'error_class': type(error).__name__, 'reason': str(error)}))
        raise SystemExit(1)
