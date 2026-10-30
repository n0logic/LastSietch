# Mode B lab rehearsal (scratch): fresh isolated lab, insert-only restore of a native archive, exact readback.
import importlib.util, sys, json, subprocess
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
container = sys.argv[2]
print(json.dumps(r.setup_lab(raw, container)))
ident = r.lab.inspect_lab(container)
res = subprocess.run(['docker', 'exec', '-i', ident, 'psql', '-X', '-q', '-t', '-A', '-U', 'postgres', '-d', 'vaultcheck',
                      '-v', 'ON_ERROR_STOP=1'], input=r.build_restore_sql(raw), capture_output=True, timeout=180)
print('rc', res.returncode); print(res.stdout.decode()[-2000:]); print(res.stderr.decode()[-2000:])
# second run must refuse (collisions) and leave the lab unchanged
res2 = subprocess.run(['docker', 'exec', '-i', ident, 'psql', '-X', '-q', '-t', '-A', '-U', 'postgres', '-d', 'vaultcheck',
                       '-v', 'ON_ERROR_STOP=1'], input=r.build_restore_sql(raw), capture_output=True, timeout=180)
print('rerun rc', res2.returncode, res2.stderr.decode()[-300:])
q = "SELECT (SELECT count(*) FROM dune.base_backups),(SELECT count(*) FROM dune.base_backup_linked_actors),(SELECT count(*) FROM dune.actors WHERE state='BaseBackup'),(SELECT count(*) FROM dune.building_instances),(SELECT landclaim_original_global_location::text FROM dune.totems WHERE id=137099)"
res3 = subprocess.run(['docker', 'exec', '-i', ident, 'psql', '-X', '-q', '-t', '-A', '-U', 'postgres', '-d', 'vaultcheck', '-c', q], capture_output=True, timeout=30)
print('after', res3.stdout.decode().strip(), res3.stderr.decode()[-300:])
