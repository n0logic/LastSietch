# Mode B read-only preflight generator (scratch). Never touches the game DB itself.
import importlib.util, sys, json
spec = importlib.util.spec_from_file_location('restore', '/opt/lastsietch-base-vault/current/restore.py')
r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
vault = r.vault
r.PARENT_SCOPES['base_backup_linked_actors'] = ('id', 'base_backups', 'id')
def validate(raw):
    data, external = vault.validate(raw)
    r.identities(data, external)
    for t in vault.tables_for(data):
        r.row_key(data, t)
    return data, external
r.validate = validate
store = vault.Vault(vault.ROOT)
manifest, raw = store.verify(sys.argv[1])
data, external = validate(raw)
info = {k: data.get(k) for k in ('format', 'captured_at', 'root_totem_id', 'owner_controller_id', 'source_location')}
info['tables'] = {t: len(data['tables'][t]) for t in vault.tables_for(data)}
info['header'] = data['tables'].get('base_backups')
print(json.dumps(info, default=str), file=sys.stderr)
sql = 'BEGIN TRANSACTION READ ONLY;\n' + r.preflight_select(raw) + ';\nROLLBACK;\n'
open(sys.argv[2], 'w').write(sql)
