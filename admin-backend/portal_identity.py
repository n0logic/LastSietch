import re
import sqlite3
import uuid

MAX_LEGACY_LINKS = 100000
SCHEMA = (
    ('portal_profiles', 'portal_profiles', """CREATE TABLE portal_profiles (
        id TEXT PRIMARY KEY NOT NULL
            CHECK(length(id) = 32 AND id NOT GLOB '*[^a-f0-9]*'),
        legacy_discord_id TEXT UNIQUE,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        disabled_at TEXT,
        CHECK(legacy_discord_id IS NULL OR
              (length(legacy_discord_id) BETWEEN 1 AND 20
               AND legacy_discord_id NOT GLOB '*[^0-9]*'
               AND substr(legacy_discord_id, 1, 1) != '0'))
    )"""),
    ('portal_profile_providers', 'portal_profile_providers', """CREATE TABLE portal_profile_providers (
        provider TEXT NOT NULL
            CHECK(length(provider) BETWEEN 1 AND 32 AND provider NOT GLOB '*[^a-z0-9_-]*'),
        subject TEXT NOT NULL CHECK(length(subject) BETWEEN 1 AND 255),
        profile_id TEXT NOT NULL REFERENCES portal_profiles(id),
        linked_at TEXT NOT NULL DEFAULT (datetime('now')),
        revoked_at TEXT,
        PRIMARY KEY(provider, subject)
    )"""),
    ('uq_portal_profile_active_provider', 'portal_profile_providers',
     """CREATE UNIQUE INDEX uq_portal_profile_active_provider
        ON portal_profile_providers(profile_id, provider) WHERE revoked_at IS NULL"""),
    ('portal_profile_accounts', 'portal_profile_accounts', """CREATE TABLE portal_profile_accounts (
        profile_id TEXT NOT NULL REFERENCES portal_profiles(id),
        account_id INTEGER NOT NULL CHECK(typeof(account_id) = 'integer' AND account_id > 0),
        legacy_link_id INTEGER UNIQUE REFERENCES ls_account_links(id),
        linked_at TEXT NOT NULL,
        revoked_at TEXT,
        PRIMARY KEY(profile_id, account_id)
    )"""),
    ('uq_portal_profile_active_account', 'portal_profile_accounts',
     """CREATE UNIQUE INDEX uq_portal_profile_active_account
        ON portal_profile_accounts(account_id) WHERE revoked_at IS NULL"""),
)
TABLES = ('portal_profiles', 'portal_profile_providers', 'portal_profile_accounts')
SCHEMA_V1 = SCHEMA
SESSION_COLUMN = "session_version INTEGER NOT NULL DEFAULT 1 CHECK(session_version > 0 AND typeof(session_version) = 'integer')"
SCHEMA = tuple((name, table, sql.replace('revoked_at TEXT,', 'revoked_at TEXT, ' + SESSION_COLUMN + ',')
               if name == 'portal_profile_accounts' else sql)
               for name, table, sql in SCHEMA_V1)
SCHEMA_V2 = SCHEMA
ACCOUNT_DETAILS = "character_name TEXT NOT NULL DEFAULT '', verified_via TEXT NOT NULL DEFAULT 'legacy'"
SCHEMA_V3 = tuple((name, table, sql.replace(SESSION_COLUMN + ',', SESSION_COLUMN + ', ' + ACCOUNT_DETAILS + ',')
                  if name == 'portal_profile_accounts' else sql) for name, table, sql in SCHEMA_V2)


class ProfileMigrationError(ValueError):
    pass


def valid_discord_id(value):
    return isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,19}', value) is not None


def _source_links(conn):
    required = {'id', 'discord_id', 'account_id', 'linked_at', 'revoked_at'}
    if not required.issubset({row[1] for row in conn.execute('PRAGMA table_info(ls_account_links)')}):
        raise ProfileMigrationError('legacy_link_schema_unavailable')
    rows = conn.execute('SELECT id, discord_id, account_id, linked_at, revoked_at '
                        'FROM ls_account_links ORDER BY id LIMIT ?', (MAX_LEGACY_LINKS + 1,)).fetchall()
    if len(rows) > MAX_LEGACY_LINKS:
        raise ProfileMigrationError('legacy_link_bound_exceeded')
    active = set()
    identities = set()
    for link_id, discord_id, account_id, linked_at, revoked_at in rows:
        if (type(link_id) is not int or link_id <= 0 or not valid_discord_id(discord_id)
                or type(account_id) is not int or account_id <= 0
                or not isinstance(linked_at, str) or not 0 < len(linked_at) <= 64
                or (revoked_at is not None and (not isinstance(revoked_at, str) or len(revoked_at) > 64))):
            raise ProfileMigrationError('legacy_link_invalid')
        if (discord_id, account_id) in identities:
            raise ProfileMigrationError('legacy_link_ambiguous')
        identities.add((discord_id, account_id))
        if revoked_at is None:
            if account_id in active:
                raise ProfileMigrationError('legacy_active_owner_conflict')
            active.add(account_id)
    return rows


def _schema(conn, create=True, definitions=None):
    existing = conn.execute("SELECT name, tbl_name, sql FROM sqlite_master "
                            "WHERE tbl_name IN (?, ?, ?) AND sql IS NOT NULL", TABLES).fetchall()
    accept_credentials = definitions is None
    definitions = SCHEMA if definitions is None else definitions
    expected = {name: (table, re.sub(r'\s+', ' ', sql).strip()) for name, table, sql in definitions}
    if existing:
        actual = {name: (table, re.sub(r'\s+', ' ', sql).strip()) for name, table, sql in existing}
        if actual != expected:
            credential_schema = {name: (table, re.sub(r'\s+', ' ', sql).strip()) for name, table, sql in SCHEMA_V3}
            if not accept_credentials or actual != credential_schema:
                raise ProfileMigrationError('profile_schema_differs')
    elif create:
        for _name, _table, sql in definitions:
            conn.execute(sql)
    else:
        raise ProfileMigrationError('profile_schema_unavailable')


def upgrade_for_sessions(conn):
    if conn.in_transaction:
        raise ProfileMigrationError('migration_requires_own_transaction')
    try:
        conn.execute('BEGIN IMMEDIATE')
        try:
            _schema(conn, create=False)
            upgraded = False
        except ProfileMigrationError:
            _schema(conn, create=False, definitions=SCHEMA_V1)
            conn.execute('ALTER TABLE portal_profile_accounts ADD COLUMN ' + SESSION_COLUMN)
            _schema(conn, create=False)
            upgraded = True
        conn.commit()
        return {'session_schema_upgraded': upgraded}
    except BaseException:
        conn.rollback()
        raise


def backfill(conn):
    if conn.in_transaction:
        raise ProfileMigrationError('migration_requires_own_transaction')
    conn.execute('PRAGMA foreign_keys=ON')
    created = {'profiles_created': 0, 'providers_created': 0, 'bindings_created': 0}
    try:
        conn.execute('BEGIN IMMEDIATE')
        links = _source_links(conn)
        _schema(conn)
        profiles = {}
        for discord_id in sorted({row[1] for row in links}):
            existing = conn.execute('SELECT id, disabled_at FROM portal_profiles WHERE legacy_discord_id = ?',
                                    (discord_id,)).fetchone()
            provider = conn.execute("SELECT profile_id, revoked_at FROM portal_profile_providers "
                                    "WHERE provider = 'discord' AND subject = ?", (discord_id,)).fetchone()
            if existing is None:
                if provider is not None:
                    raise ProfileMigrationError('provider_ownership_conflict')
                profile_id = uuid.uuid4().hex
                conn.execute('INSERT INTO portal_profiles(id, legacy_discord_id) VALUES (?, ?)',
                             (profile_id, discord_id))
                created['profiles_created'] += 1
            else:
                profile_id = existing[0]
                if existing[1] is not None:
                    raise ProfileMigrationError('profile_disabled')
                if provider is None:
                    raise ProfileMigrationError('existing_profile_provider_missing')
            if provider is None:
                conn.execute("INSERT INTO portal_profile_providers(provider, subject, profile_id) "
                             "VALUES ('discord', ?, ?)", (discord_id, profile_id))
                created['providers_created'] += 1
            elif provider[0] != profile_id or provider[1] is not None:
                raise ProfileMigrationError('provider_ownership_conflict')
            profiles[discord_id] = profile_id

        for link_id, discord_id, account_id, linked_at, revoked_at in links:
            expected = (profiles[discord_id], account_id, link_id, linked_at, revoked_at)
            existing = conn.execute('SELECT profile_id, account_id, legacy_link_id, linked_at, revoked_at '
                                    'FROM portal_profile_accounts WHERE legacy_link_id = ? '
                                    'OR (profile_id = ? AND account_id = ?)',
                                    (link_id, profiles[discord_id], account_id)).fetchall()
            if existing:
                if len(existing) != 1 or tuple(existing[0]) != expected:
                    raise ProfileMigrationError('account_binding_differs')
            else:
                conn.execute('INSERT INTO portal_profile_accounts '
                             '(profile_id, account_id, legacy_link_id, linked_at, revoked_at) '
                             'VALUES (?, ?, ?, ?, ?)', expected)
                created['bindings_created'] += 1

        sources = {row[0] for row in links}
        if any(row[0] not in sources for row in conn.execute(
                'SELECT legacy_link_id FROM portal_profile_accounts WHERE legacy_link_id IS NOT NULL')):
            raise ProfileMigrationError('legacy_binding_source_missing')
        for table in TABLES:
            if conn.execute('PRAGMA foreign_key_check(' + table + ')').fetchone():
                raise ProfileMigrationError('profile_foreign_key_conflict')
        conn.commit()
    except BaseException as error:
        conn.rollback()
        if isinstance(error, sqlite3.DatabaseError):
            raise ProfileMigrationError('profile_migration_sql_refused') from None
        raise
    return {**created, 'legacy_links': len(links), 'legacy_profiles': len(profiles),
            'active_accounts': sum(row[4] is None for row in links),
            'revoked_links': sum(row[4] is not None for row in links)}


def legacy_session_context(conn, discord_id, account_id):
    # Inputs must come from a verified legacy session, never from game proof alone.
    if not valid_discord_id(discord_id) or type(account_id) is not int or account_id <= 0:
        return None
    try:
        row = conn.execute("""SELECT p.id, a.session_version
            FROM portal_profiles p
            JOIN portal_profile_providers v ON v.profile_id = p.id
            JOIN portal_profile_accounts a ON a.profile_id = p.id
            JOIN ls_account_links l ON l.id = a.legacy_link_id
            WHERE v.provider = 'discord' AND v.subject = ? AND v.revoked_at IS NULL
              AND p.legacy_discord_id = v.subject AND p.disabled_at IS NULL
              AND a.account_id = ? AND a.revoked_at IS NULL
              AND l.discord_id = v.subject AND l.account_id = a.account_id AND l.revoked_at IS NULL
              AND (SELECT COUNT(*) FROM ls_account_links other
                   WHERE other.account_id = a.account_id AND other.revoked_at IS NULL) = 1""",
                           (discord_id, account_id)).fetchall()
    except sqlite3.DatabaseError:
        return None
    if len(row) != 1 or type(row[0][1]) is not int or row[0][1] <= 0:
        return None
    return {'pid': row[0][0], 'lv': row[0][1]}


def profile_for_legacy_account(conn, discord_id, account_id):
    context = legacy_session_context(conn, discord_id, account_id)
    return context['pid'] if context else None


def enabled():
    import config
    return getattr(config, 'PORTAL_PROFILES_ENABLED', False) is True


def credentials_installed(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='portal_auth_meta'").fetchone() is not None


def enforcement_required():
    if enabled():
        return True
    try:
        from database import get_db
        conn = get_db()
        try:
            return credentials_installed(conn)
        finally:
            conn.close()
    except Exception:
        return True


def link_table(conn):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='view' AND name='portal_identity_links'").fetchone():
        return 'portal_identity_links'
    return 'ls_account_links'


def actor_key(session):
    session = session or {}
    return str(session.get('identity') or session.get('did') or '')


def quota_keys(conn, key):
    keys = [str(key)]
    if credentials_installed(conn):
        profile = profile_for_key(conn, str(key))
        if profile:
            keys.extend('acct:' + str(row[0]) for row in conn.execute(
                'SELECT account_id FROM portal_account_owners WHERE profile_id=? ORDER BY account_id', (profile,)))
    return keys


def profile_for_key(conn, key):
    if not isinstance(key, str):
        return None
    if key.startswith('profile:') and re.fullmatch(r'[a-f0-9]{32}', key[8:]):
        row = conn.execute('SELECT id FROM portal_profiles WHERE id = ? AND disabled_at IS NULL', (key[8:],)).fetchone()
    else:
        row = conn.execute('SELECT id FROM portal_profiles WHERE legacy_discord_id = ? AND disabled_at IS NULL', (key,)).fetchone()
    return row[0] if row else None


def discord_for_identity(conn, key):
    if not credentials_installed(conn):
        return key if valid_discord_id(key) else None
    profile = profile_for_key(conn, key)
    if profile is None and valid_discord_id(key):
        row = conn.execute("SELECT p.id FROM portal_profile_providers v JOIN portal_profiles p ON p.id=v.profile_id "
                           "WHERE v.provider='discord' AND v.subject=? AND v.revoked_at IS NULL AND p.disabled_at IS NULL", (key,)).fetchone()
        profile = row[0] if row else None
    if profile is None:
        return None
    row = conn.execute("SELECT subject FROM portal_profile_providers WHERE profile_id=? AND provider='discord' AND revoked_at IS NULL", (profile,)).fetchone()
    return row[0] if row else None


def account_context(conn, profile_id, account_id):
    owner = conn.execute('SELECT profile_id FROM portal_account_owners WHERE account_id=?', (account_id,)).fetchone()
    if owner is None or owner[0] != profile_id:
        return None
    row = conn.execute('SELECT discord_id, account_id, character_name, discord_handle, session_version '
                       'FROM portal_identity_links WHERE profile_id=? AND account_id=?', (profile_id, account_id)).fetchone()
    if row is None:
        return None
    return {'pid': profile_id, 'aid': row[1], 'identity': row[0], 'lv': row[4],
            'character_name': row[2], 'display_handle': row[3],
            'did': discord_for_identity(conn, row[0]) or ''}


def links_for_identity(conn, key):
    table = link_table(conn)
    return conn.execute('SELECT account_id, character_name, discord_handle, linked_at, last_session_at '
                        'FROM ' + table + ' WHERE discord_id=? AND revoked_at IS NULL ORDER BY linked_at, account_id', (key,)).fetchall()


def link_for_account(conn, key, account_id):
    table = link_table(conn)
    return conn.execute('SELECT character_name, discord_handle FROM ' + table +
                        ' WHERE discord_id=? AND account_id=? AND revoked_at IS NULL', (key, account_id)).fetchone()


def begin_legacy_links(conn, discord_id, allow_grant=False, require_profiles=None):
    if conn.in_transaction:
        raise ProfileMigrationError('invalid_link_transaction')
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('BEGIN IMMEDIATE')
        required = enabled() if require_profiles is None else require_profiles
        present = conn.execute('SELECT count(*) FROM sqlite_master WHERE name IN (?, ?, ?)', TABLES).fetchone()[0]
        if not present and not required:
            return None
        if not valid_discord_id(discord_id):
            raise ProfileMigrationError('invalid_link_identity')
        _schema(conn, create=False)
        before = {row[0]: tuple(row) for row in _source_links(conn)}
        return {'connection': conn, 'discord_id': discord_id,
                'allow_grant': allow_grant is True, 'before': before}
    except BaseException:
        conn.rollback()
        raise


def finish_legacy_links(conn, change):
    if change is None:
        return
    if change.get('connection') is not conn or not conn.in_transaction:
        raise ProfileMigrationError('link_transaction_lost')
    try:
        before = change['before']
        after = {row[0]: tuple(row) for row in _source_links(conn)}
        if set(before) - set(after):
            raise ProfileMigrationError('legacy_link_deletion_refused')
        changed = [row for key, row in after.items() if row != before.get(key)]
        if not changed:
            return
        discord_id = change['discord_id']
        for link_id, did, aid, _linked, revoked in changed:
            old = before.get(link_id)
            if did != discord_id or (old is not None and old[:3] != (link_id, did, aid)):
                raise ProfileMigrationError('link_change_outside_identity')
            if revoked is None and not change['allow_grant']:
                raise ProfileMigrationError('link_grant_not_authorized')
        profile = conn.execute('SELECT id, disabled_at FROM portal_profiles WHERE legacy_discord_id = ?',
                               (discord_id,)).fetchone()
        granting = any(row[4] is None for row in changed)
        if profile is None:
            if not granting or any(row[1] == discord_id for row in before.values()):
                raise ProfileMigrationError('existing_profile_missing')
            if conn.execute("SELECT 1 FROM portal_profile_providers WHERE provider = 'discord' AND subject = ?",
                            (discord_id,)).fetchone():
                raise ProfileMigrationError('provider_ownership_conflict')
            profile_id = uuid.uuid4().hex
            conn.execute('INSERT INTO portal_profiles(id, legacy_discord_id) VALUES (?, ?)', (profile_id, discord_id))
            conn.execute("INSERT INTO portal_profile_providers(provider, subject, profile_id) VALUES ('discord', ?, ?)",
                         (discord_id, profile_id))
        else:
            profile_id = profile[0]
            if granting:
                provider = conn.execute("SELECT profile_id, revoked_at FROM portal_profile_providers "
                                        "WHERE provider = 'discord' AND subject = ?", (discord_id,)).fetchone()
                if profile[1] is not None or provider is None or provider[0] != profile_id or provider[1] is not None:
                    raise ProfileMigrationError('profile_provider_unavailable')

        # Revoke first so a scoped relink never bypasses the active-owner constraint.
        for link_id, did, aid, linked_at, revoked_at in sorted(changed, key=lambda row: row[4] is None):
            old = before.get(link_id)
            binding = conn.execute('SELECT profile_id, account_id, legacy_link_id, linked_at, revoked_at, session_version '
                                   'FROM portal_profile_accounts WHERE legacy_link_id = ? OR (profile_id = ? AND account_id = ?)',
                                   (link_id, profile_id, aid)).fetchall()
            if revoked_at is None and credentials_installed(conn):
                owner = conn.execute('SELECT profile_id FROM portal_account_owners WHERE account_id=?', (aid,)).fetchone()
                if owner is not None and owner[0] != profile_id:
                    raise ProfileMigrationError('account_ownership_recovery_required')
                if owner is None:
                    conn.execute('INSERT INTO portal_account_owners(account_id, profile_id) VALUES (?, ?)', (aid, profile_id))
            if old is None:
                if binding:
                    raise ProfileMigrationError('new_link_binding_conflict')
                # Version 1 is reserved for cookies from before profile migration.
                conn.execute('INSERT INTO portal_profile_accounts '
                             '(profile_id, account_id, legacy_link_id, linked_at, revoked_at, session_version) '
                             'VALUES (?, ?, ?, ?, ?, 2)', (profile_id, aid, link_id, linked_at, revoked_at))
            else:
                if len(binding) != 1 or tuple(binding[0][:3]) != (profile_id, aid, link_id):
                    raise ProfileMigrationError('existing_link_binding_missing')
                if revoked_at is None and tuple(binding[0][3:5]) != (old[3], old[4]):
                    raise ProfileMigrationError('existing_link_binding_differs')
                conn.execute('UPDATE portal_profile_accounts SET linked_at = ?, revoked_at = ?, '
                             'session_version = session_version + 1 WHERE legacy_link_id = ?',
                             (linked_at, revoked_at, link_id))
    except BaseException as error:
        conn.rollback()
        if isinstance(error, sqlite3.DatabaseError):
            raise ProfileMigrationError('profile_link_write_refused') from None
        raise


def quota_identity(conn, profile_id):
    # A historical quota key is not a login provider or an authorization decision.
    if not isinstance(profile_id, str) or re.fullmatch(r'[a-f0-9]{32}', profile_id) is None:
        return None
    try:
        row = conn.execute('SELECT legacy_discord_id FROM portal_profiles '
                           'WHERE id = ? AND disabled_at IS NULL', (profile_id,)).fetchone()
    except sqlite3.DatabaseError:
        return None
    if row is None:
        return None
    return row[0] if row[0] is not None else 'profile:' + profile_id
