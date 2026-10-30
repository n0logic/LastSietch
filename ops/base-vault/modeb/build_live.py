# Mode B production SQL (scratch): the lab-verified restore transaction, with the lab-only guard
# swapped for a live-DB guard. Owner GO: Medic 2026-10-28T00:52Z (Discord mod-ops).
import importlib.util, sys
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
sql = r.build_restore_sql(raw).decode()
lab_guard = "DO $$ BEGIN IF current_database()<>'vaultcheck' THEN RAISE EXCEPTION 'vault_restore_lab_only'; END IF; END $$;"
assert sql.count(lab_guard) == 1
sql = sql.replace(lab_guard, "DO $$ BEGIN IF current_database()<>'dune' THEN RAISE EXCEPTION 'vault_restore_live_only'; END IF; END $$;")
assert sql.count("'lab_restore_verified'") == 1
sql = sql.replace("'lab_restore_verified'", "'modeb_live_restore_verified'")
# The only user trigger on the restored tables (live 2026-10-28) is Funcom's AFTER DELETE cleanup on
# actor_fgl_entities. It cannot fire on an insert-only restore, so allow exactly it, pinned by its definition.
trig_old = "WHERE n.nspname='dune' AND NOT t.tgisinternal AND c.relname IN ("
trig_new = ("WHERE n.nspname='dune' AND NOT t.tgisinternal AND NOT (c.relname='actor_fgl_entities' AND t.tgname='actor_fgl_entities_cleanup_orphaned_entities' "
            "AND pg_get_triggerdef(t.oid)='CREATE TRIGGER actor_fgl_entities_cleanup_orphaned_entities AFTER DELETE ON dune.actor_fgl_entities FOR EACH ROW EXECUTE FUNCTION dune.cleanup_orphaned_entities()') AND c.relname IN (")
assert sql.count(trig_old) == 1
sql = sql.replace(trig_old, trig_new)
assert sql.rstrip().endswith('COMMIT;')
open(sys.argv[2], 'w').write(sql)
open(sys.argv[3], 'w').write(sql.rstrip()[:-len('COMMIT;')] + 'ROLLBACK;\n')
print('ok', len(sql))
