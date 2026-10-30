#!/usr/bin/env python3
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys

spec = importlib.util.spec_from_file_location('base_export', Path(__file__).with_name('export-base.py'))
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


def capture_enabled():
    try:
        descriptor = os.open('/etc/lastsietch/base-vault-admin.enabled', os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as source:
            info = os.fstat(source.fileno())
            return (stat.S_ISREG(info.st_mode) and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o600
                    and source.read(17).strip() == b'1')
    except OSError:
        return False


def describe(totem):
    raw = exporter.sql_read(f"""SELECT jsonb_build_object(
        'totem_id',t.id,'label',coalesce(pa.actor_name,''),'map',a.map,
        'dimension_index',a.dimension_index,'owner_controller_id',p.player_id,
        'owner_name',ps.character_name,'capture_allowed',
        (p.player_id IS NOT NULL AND a.state::text IS DISTINCT FROM 'BaseBackup'),
        'backup_id',(SELECT min(l.id) FROM dune.base_backup_linked_actors l WHERE l.actor_id=t.id))
        FROM dune.totems t JOIN dune.actors a ON a.id=t.id
        LEFT JOIN dune.permission_actor pa ON pa.actor_id=t.id
        LEFT JOIN LATERAL (SELECT min(player_id) AS player_id
          FROM dune.permission_actor_rank WHERE permission_actor_id=t.id AND rank=1
          HAVING count(*)=1) p ON true
        LEFT JOIN dune.player_state ps ON ps.player_controller_id=p.player_id
        WHERE t.id={totem};""")
    return json.loads(raw) if raw else None


def describe_backup(backup):
    raw = exporter.sql_read(f"""SELECT jsonb_build_object(
        'backup_id',h.id,'label',h.base_backup_name,'owner_controller_id',h.player_id,
        'owner_name',ps.character_name,
        'linked_actors',(SELECT count(*) FROM dune.base_backup_linked_actors l WHERE l.id=h.id),
        'totem_id',(SELECT min(t.id) FROM dune.base_backup_linked_actors l JOIN dune.totems t ON t.id=l.actor_id WHERE l.id=h.id),
        'map',(SELECT min(a.map) FROM dune.base_backup_linked_actors l JOIN dune.totems t ON t.id=l.actor_id JOIN dune.actors a ON a.id=t.id WHERE l.id=h.id),
        'capture_allowed',(EXISTS (SELECT 1 FROM dune.base_backup_linked_actors l WHERE l.id=h.id)
          AND NOT EXISTS (SELECT 1 FROM dune.base_backup_linked_actors l JOIN dune.actors a ON a.id=l.actor_id
                          WHERE l.id=h.id AND a.state::text IS DISTINCT FROM 'BaseBackup')))
        FROM dune.base_backups h LEFT JOIN dune.player_state ps ON ps.player_controller_id=h.player_id
        WHERE h.id={exporter.valid_id(backup, 'backup')};""")
    return json.loads(raw) if raw else None


def execute(request):
    if not isinstance(request, dict) or request.get('action') not in ('describe', 'capture', 'describe_backup', 'capture_backup'):
        raise ValueError('invalid request')
    if request['action'] in ('describe_backup', 'capture_backup'):
        if set(request) != {'action', 'backup_id'} or type(request['backup_id']) is not int or not 0 < request['backup_id'] < 2 ** 63:
            raise ValueError('invalid request')
        backup = describe_backup(request['backup_id'])
        if request['action'] == 'describe_backup':
            return json.dumps({'backup': backup}).encode()
        if not capture_enabled() or not backup or not backup['capture_allowed']:
            raise ValueError('backup unavailable')
        schema = json.loads(exporter.sql_read(exporter.metadata_query(exporter.NATIVE_TABLES) + ';'))
        raw = exporter.sql_read(exporter.native_export_query(request['backup_id'], backup['owner_controller_id'], schema))
        if json.loads(raw).get('format') != exporter.NATIVE_FORMAT:
            raise ValueError('capture refused')
        return raw
    if (set(request) != {'action', 'totem_id'}
            or type(request['totem_id']) is not int or not 0 < request['totem_id'] < 2 ** 63):
        raise ValueError('invalid request')
    base = describe(request['totem_id'])
    if request['action'] == 'describe':
        return json.dumps({'base': base}).encode()
    if not capture_enabled() or not base or not base['capture_allowed']:
        raise ValueError('base unavailable')
    schema = json.loads(exporter.sql_read(exporter.metadata_query() + ';'))
    raw = exporter.sql_read(exporter.export_query(request['totem_id'], base['owner_controller_id'], schema))
    if json.loads(raw).get('format') != exporter.FORMAT:
        raise ValueError('capture refused')
    return raw


if __name__ == '__main__':
    try:
        if os.geteuid() != 0 or len(sys.argv) != 1 or os.environ.get('SSH_ORIGINAL_COMMAND'):
            raise ValueError('restricted exporter')
        raw = sys.stdin.buffer.read(2049)
        if len(raw) > 2048:
            raise ValueError('request too large')
        sys.stdout.buffer.write(execute(json.loads(raw)))
    except Exception:
        print('{"error":"capture_unavailable"}', file=sys.stderr)
        raise SystemExit(1)
