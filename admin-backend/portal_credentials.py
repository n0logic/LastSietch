import base64
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import time
import unicodedata
import uuid

import portal_identity as identity

SESSION_PREFIX = 'ls3_'
SESSION_IDLE = 7 * 86400
SESSION_ABSOLUTE = 30 * 86400
RECENT_AUTH = 600
CHALLENGE_TTL = 300
HASH_SLOTS = threading.BoundedSemaphore(2)
HASH_INIT = threading.Lock()
USERNAME = re.compile(r'[a-z0-9][a-z0-9_.-]{2,31}')
_hasher = None
_dummy_hash = None
_blocked = None

SCHEMA = (
    """CREATE TABLE portal_auth_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE portal_auth_users (
        profile_id TEXT PRIMARY KEY REFERENCES portal_profiles(id),
        username TEXT UNIQUE COLLATE NOCASE,
        user_handle BLOB NOT NULL UNIQUE,
        created_at REAL NOT NULL
    )""",
    """CREATE TABLE portal_account_owners (
        account_id INTEGER PRIMARY KEY CHECK(account_id > 0),
        profile_id TEXT REFERENCES portal_profiles(id)
    )""",
    """CREATE TABLE portal_password_credentials (
        profile_id TEXT PRIMARY KEY REFERENCES portal_profiles(id),
        password_hash TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
        updated_at REAL NOT NULL, revoked_at REAL
    )""",
    """CREATE TABLE portal_passkeys (
        id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES portal_profiles(id),
        public_key BLOB NOT NULL, sign_count INTEGER NOT NULL,
        label TEXT NOT NULL, transports TEXT NOT NULL DEFAULT '[]',
        device_type TEXT NOT NULL, backed_up INTEGER NOT NULL,
        created_at REAL NOT NULL, last_used REAL, revoked_at REAL
    )""",
    "CREATE INDEX idx_portal_passkey_profile ON portal_passkeys(profile_id)",
    """CREATE TABLE portal_auth_sessions (
        id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
        profile_id TEXT NOT NULL REFERENCES portal_profiles(id),
        account_id INTEGER NOT NULL DEFAULT 0, link_version INTEGER NOT NULL DEFAULT 0,
        method TEXT NOT NULL, credential TEXT NOT NULL,
        auth_at REAL NOT NULL, created_at REAL NOT NULL, last_seen REAL NOT NULL,
        expires_at REAL NOT NULL, user_agent TEXT NOT NULL DEFAULT '', revoked_at REAL
    )""",
    "CREATE INDEX idx_portal_session_profile ON portal_auth_sessions(profile_id)",
    """CREATE TABLE portal_auth_challenges (
        id TEXT PRIMARY KEY, purpose TEXT NOT NULL, binding_hash TEXT NOT NULL,
        profile_id TEXT, challenge BLOB, context TEXT NOT NULL DEFAULT '{}',
        code_mac TEXT, created_at REAL NOT NULL, expires_at REAL NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, verified_at REAL, consumed_at REAL
    )""",
    "CREATE INDEX idx_portal_challenge_expiry ON portal_auth_challenges(expires_at)",
    """CREATE TABLE portal_auth_limits (
        bucket TEXT NOT NULL, key_hash TEXT NOT NULL, window INTEGER NOT NULL,
        uses INTEGER NOT NULL, PRIMARY KEY(bucket, key_hash, window)
    )""",
    """CREATE TABLE portal_auth_audit (
        id INTEGER PRIMARY KEY, profile_id TEXT,
        event TEXT NOT NULL, method TEXT NOT NULL, result TEXT NOT NULL, occurred_at REAL NOT NULL
    )""",
    """CREATE VIEW portal_identity_links AS
        SELECT a.rowid AS id, p.id AS profile_id,
            COALESCE(p.legacy_discord_id, 'profile:' || p.id) AS discord_id,
            a.account_id, COALESCE(NULLIF(a.character_name,''),l.character_name,'') AS character_name,
            COALESCE(u.username,l.discord_handle,NULLIF(a.character_name,''),'Player') AS discord_handle,
            a.linked_at, l.last_session_at, a.revoked_at, a.session_version,
            l.fls_id, l.revoked_by, l.revoke_reason
        FROM portal_profile_accounts a JOIN portal_profiles p ON p.id=a.profile_id
        LEFT JOIN portal_auth_users u ON u.profile_id=p.id
        LEFT JOIN ls_account_links l ON l.id=a.legacy_link_id
        WHERE p.disabled_at IS NULL AND a.revoked_at IS NULL
          AND (a.legacy_link_id IS NULL OR (l.revoked_at IS NULL AND l.account_id=a.account_id
               AND l.discord_id=p.legacy_discord_id AND
               (SELECT count(*) FROM ls_account_links other WHERE other.account_id=a.account_id AND other.revoked_at IS NULL)=1))""",
)


class AuthError(ValueError):
    pass


@contextmanager
def transaction(conn):
    if conn.in_transaction:
        raise AuthError('transaction_already_active')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('BEGIN IMMEDIATE')
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def install(conn):
    require_private_storage(conn)
    with transaction(conn):
        identity._schema(conn, create=False)
        if identity.credentials_installed(conn):
            row = conn.execute("SELECT value FROM portal_auth_meta WHERE key='schema'").fetchone()
            if row is None or row[0] != '3':
                raise AuthError('unknown_auth_schema')
            check_schema(conn)
            return
        columns = {r[1] for r in conn.execute('PRAGMA table_info(portal_profile_accounts)')}
        if 'character_name' not in columns:
            conn.execute("ALTER TABLE portal_profile_accounts ADD COLUMN character_name TEXT NOT NULL DEFAULT ''")
            conn.execute("ALTER TABLE portal_profile_accounts ADD COLUMN verified_via TEXT NOT NULL DEFAULT 'legacy'")
        identity._schema(conn, create=False, definitions=identity.SCHEMA_V3)
        for sql in SCHEMA:
            conn.execute(sql)
        for profile_id, in conn.execute('SELECT id FROM portal_profiles').fetchall():
            conn.execute('INSERT INTO portal_auth_users(profile_id,user_handle,created_at) VALUES (?,?,?)',
                         (profile_id, secrets.token_bytes(64), time.time()))
        for account_id, in conn.execute('SELECT DISTINCT account_id FROM portal_profile_accounts').fetchall():
            rows = conn.execute('SELECT profile_id,revoked_at FROM portal_profile_accounts WHERE account_id=?', (account_id,)).fetchall()
            active = [row[0] for row in rows if row[1] is None]
            owners = {row[0] for row in rows}
            owner = active[0] if len(active) == 1 else next(iter(owners)) if len(owners) == 1 else None
            conn.execute('INSERT INTO portal_account_owners(account_id,profile_id) VALUES (?,?)', (account_id, owner))
        conn.execute("INSERT INTO portal_auth_meta(key,value) VALUES ('schema','3')")
        check_schema(conn)


def check_schema(conn):
    for sql in SCHEMA:
        name = re.match(r'CREATE (?:TABLE|INDEX|VIEW) (\w+)', sql)[1]
        row = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
        if row is None or re.sub(r'\s+', ' ', row[0]).strip() != re.sub(r'\s+', ' ', sql).strip():
            raise AuthError('unknown_auth_schema')


def require_private_storage(conn):
    filename = next(row[2] for row in conn.execute('PRAGMA database_list') if row[1] == 'main')
    if not filename:
        return
    path = Path(filename)
    if path != path.resolve():
        raise AuthError('private_storage_required')
    for item in (path, *path.parents):
        info = item.lstat()
        if info.st_uid not in (0, os.geteuid()):
            raise AuthError('private_storage_required')
        if item == path:
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise AuthError('private_storage_required')
        elif not stat.S_ISDIR(info.st_mode) or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX):
            raise AuthError('private_storage_required')


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def audit(conn, profile_id, event, method, result):
    conn.execute('INSERT INTO portal_auth_audit(profile_id,event,method,result,occurred_at) VALUES (?,?,?,?,?)',
                 (profile_id, event, method, result, time.time()))


def username(value):
    value = value.strip().lower() if isinstance(value, str) else ''
    if USERNAME.fullmatch(value) is None:
        raise AuthError('username_format')
    return value


def _password_hasher():
    global _hasher, _dummy_hash
    if _hasher is None:
        with HASH_INIT:
            if _hasher is None:
                from argon2 import PasswordHasher
                hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1)
                _dummy_hash = hasher.hash(secrets.token_urlsafe(32))
                _hasher = hasher
    return _hasher


def validate_password(value, account_name=''):
    global _blocked
    if not isinstance(value, str):
        raise AuthError('password_length')
    value = unicodedata.normalize('NFC', value)
    try:
        encoded_length = len(value.encode('utf-8'))
    except UnicodeError:
        raise AuthError('password_length') from None
    if not 8 <= len(value) <= 128 or encoded_length > 512:
        raise AuthError('password_length')
    if _blocked is None:
        from zxcvbn.frequency_lists import FREQUENCY_LISTS
        _blocked = {unicodedata.normalize('NFC', word).casefold() for word in FREQUENCY_LISTS['passwords']}
        _blocked.update(('123456789012345', '1234567890' * 2, 'passwordpassword', 'lastsietchpassword'))
    if value.casefold() in _blocked or value.casefold() == account_name.casefold():
        raise AuthError('password_common')
    return value


def hash_password(value):
    if not HASH_SLOTS.acquire(blocking=False):
        raise AuthError('busy')
    try:
        return _password_hasher().hash(value)
    finally:
        HASH_SLOTS.release()


def password_matches(stored, value):
    if not isinstance(value, str) or len(value) > 128:
        return False
    try:
        if len(value.encode('utf-8')) > 512:
            return False
    except UnicodeError:
        return False
    if not HASH_SLOTS.acquire(blocking=False):
        raise AuthError('busy')
    try:
        from argon2 import extract_parameters
        from argon2.exceptions import VerificationError, InvalidHashError
        hasher = _password_hasher()
        candidate = stored or _dummy_hash
        params = extract_parameters(candidate)
        if params.memory_cost > 65536 or params.time_cost > 4 or params.parallelism > 4:
            raise AuthError('password_hash_parameters')
        try:
            good = hasher.verify(candidate, unicodedata.normalize('NFC', value))
        except (VerificationError, InvalidHashError):
            good = False
        return bool(stored and good)
    finally:
        HASH_SLOTS.release()


def rate_limit(conn, bucket, key, limit, seconds, now=None):
    now = time.time() if now is None else now
    window = int(now // seconds)
    import config
    key_hash = hmac.new(config.PORTAL_SESSION_SECRET.encode(), (bucket + ':' + key).encode(), hashlib.sha256).hexdigest()
    with transaction(conn):
        conn.execute('DELETE FROM portal_auth_limits WHERE window < ? AND bucket=?', (window - 1, bucket))
        row = conn.execute('SELECT uses FROM portal_auth_limits WHERE bucket=? AND key_hash=? AND window=?',
                           (bucket, key_hash, window)).fetchone()
        if row and row[0] >= limit:
            return False
        conn.execute('INSERT INTO portal_auth_limits(bucket,key_hash,window,uses) VALUES (?,?,?,1) '
                     'ON CONFLICT(bucket,key_hash,window) DO UPDATE SET uses=uses+1', (bucket, key_hash, window))
    return True


def new_challenge(conn, purpose, binding, profile_id=None, context=None, challenge=None, code=None):
    identifier = uuid.uuid4().hex
    now = time.time()
    code_mac = None
    if code is not None:
        code_mac = code_digest(identifier, binding, code)
    with transaction(conn):
        conn.execute('DELETE FROM portal_auth_challenges WHERE expires_at < ?', (now - 86400,))
        conn.execute('INSERT INTO portal_auth_challenges(id,purpose,binding_hash,profile_id,challenge,context,code_mac,created_at,expires_at) '
                     'VALUES (?,?,?,?,?,?,?,?,?)', (identifier, purpose, digest(binding), profile_id, challenge,
                                                   json.dumps(context or {}, separators=(',', ':')), code_mac, now, now + CHALLENGE_TTL))
    return identifier


def code_digest(identifier, binding, code):
    import config
    return hmac.new(config.PORTAL_SESSION_SECRET.encode(), ('game-code:' + identifier + ':' + digest(binding) + ':' + code).encode(), hashlib.sha256).hexdigest()


def challenge_row(conn, identifier, binding, purpose, profile_id=None):
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT * FROM portal_auth_challenges WHERE id=?', (identifier,)).fetchone()
    if (row is None or row['purpose'] != purpose or row['binding_hash'] != digest(binding)
            or row['profile_id'] != profile_id or row['consumed_at'] is not None or row['expires_at'] < time.time()):
        raise AuthError('challenge_expired')
    return dict(row)


def consume_challenge(conn, identifier, binding, purpose, profile_id=None):
    with transaction(conn):
        row = challenge_row(conn, identifier, binding, purpose, profile_id)
        changed = conn.execute('UPDATE portal_auth_challenges SET consumed_at=? WHERE id=? AND consumed_at IS NULL',
                               (time.time(), identifier)).rowcount
        if changed != 1:
            raise AuthError('challenge_expired')
    return row


def verify_game_code(conn, identifier, binding, code, purpose, profile_id=None):
    if not isinstance(code, str) or re.fullmatch('[0-9]{6}', code) is None:
        code = ''
    with transaction(conn):
        row = challenge_row(conn, identifier, binding, purpose, profile_id)
        if row['verified_at'] is not None or row['attempts'] >= 5:
            raise AuthError('challenge_expired')
        good = hmac.compare_digest(row['code_mac'] or '', code_digest(identifier, binding, code))
        conn.execute('UPDATE portal_auth_challenges SET attempts=attempts+1,verified_at=?,expires_at=?,consumed_at=? WHERE id=?',
                     (time.time() if good else None, time.time() + 600 if good else row['expires_at'],
                      time.time() if not good and row['attempts'] >= 4 else None, identifier))
    if not good:
        raise AuthError('verification_failed')
    return json.loads(row['context'])


def _profile_active(conn, profile_id):
    row = conn.execute('SELECT id FROM portal_profiles WHERE id=? AND disabled_at IS NULL', (profile_id,)).fetchone()
    if row is None:
        raise AuthError('sign_in_failed')


def _credential_active(conn, profile_id, method, credential):
    if method == 'discord':
        return conn.execute("SELECT 1 FROM portal_profile_providers WHERE profile_id=? AND provider='discord' AND subject=? AND revoked_at IS NULL",
                            (profile_id, credential)).fetchone() is not None
    if method == 'password':
        return conn.execute('SELECT 1 FROM portal_password_credentials WHERE profile_id=? AND version=? AND revoked_at IS NULL',
                            (profile_id, credential)).fetchone() is not None
    if method == 'passkey':
        return conn.execute('SELECT 1 FROM portal_passkeys WHERE profile_id=? AND id=? AND revoked_at IS NULL',
                            (profile_id, credential)).fetchone() is not None
    return False


def _create_session(conn, profile_id, method, credential, user_agent='', auth_at=None, account_id=None):
    _profile_active(conn, profile_id)
    if not _credential_active(conn, profile_id, method, credential):
        raise AuthError('sign_in_failed')
    now = time.time()
    if conn.execute('SELECT 1 FROM portal_auth_users WHERE profile_id=?', (profile_id,)).fetchone() is None:
        conn.execute('INSERT INTO portal_auth_users(profile_id,user_handle,created_at) VALUES (?,?,?)',
                     (profile_id, secrets.token_bytes(64), now))
    if account_id is None:
        row = conn.execute('SELECT account_id FROM portal_identity_links WHERE profile_id=? ORDER BY linked_at,account_id LIMIT 1', (profile_id,)).fetchone()
        account_id = row[0] if row else 0
    context = identity.account_context(conn, profile_id, account_id) if account_id else None
    if account_id and context is None:
        raise AuthError('account_unavailable')
    token = SESSION_PREFIX + secrets.token_urlsafe(32)
    identifier = uuid.uuid4().hex
    conn.execute('INSERT INTO portal_auth_sessions(id,token_hash,profile_id,account_id,link_version,method,credential,auth_at,created_at,last_seen,expires_at,user_agent) '
                 'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                 (identifier, digest(token), profile_id, account_id, context['lv'] if context else 0,
                  method, str(credential), auth_at or now, now, now, now + SESSION_ABSOLUTE,
                  ''.join(c for c in user_agent[:160] if c.isprintable())))
    audit(conn, profile_id, 'login', method, 'accepted')
    return token


def create_session(conn, profile_id, method, credential, user_agent='', auth_at=None, account_id=None):
    with transaction(conn):
        return _create_session(conn, profile_id, method, credential, user_agent, auth_at, account_id)


def session(conn, token):
    if not isinstance(token, str) or re.fullmatch(r'ls3_[A-Za-z0-9_-]{43}', token) is None:
        return None
    now = time.time()
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT * FROM portal_auth_sessions WHERE token_hash=?', (digest(token),)).fetchone()
    if row is None or row['revoked_at'] is not None or not 0 <= now - row['last_seen'] <= SESSION_IDLE or now > row['expires_at']:
        return None
    profile_id = row['profile_id']
    try:
        _profile_active(conn, profile_id)
    except AuthError:
        return None
    if not _credential_active(conn, profile_id, row['method'], row['credential']):
        return None
    context = identity.account_context(conn, profile_id, row['account_id']) if row['account_id'] else None
    if row['account_id'] and (context is None or context['lv'] != row['link_version']):
        return None
    key = identity.quota_identity(conn, profile_id)
    if key is None:
        return None
    if now - row['last_seen'] > 60:
        conn.execute('UPDATE portal_auth_sessions SET last_seen=? WHERE id=? AND revoked_at IS NULL', (now, row['id']))
        conn.commit()
    return {**(context or {'pid': profile_id, 'aid': 0, 'identity': key, 'lv': 0,
                          'did': identity.discord_for_identity(conn, key) or ''}),
            'sid': row['id'], 'sh': row['token_hash'], 'iat': int(row['created_at']), 'auth_at': row['auth_at'], 'method': row['method']}


def require_recent(conn, profile_id, current):
    if not isinstance(current, dict) or current.get('pid') != profile_id:
        raise AuthError('reauthenticate')
    row = conn.execute('SELECT auth_at,method,credential FROM portal_auth_sessions WHERE id=? AND token_hash=? AND profile_id=? AND revoked_at IS NULL AND expires_at>=?',
                       (current.get('sid'), current.get('sh'), profile_id, time.time())).fetchone()
    if row is None or not 0 <= time.time() - row[0] <= RECENT_AUTH or not _credential_active(conn, profile_id, row[1], row[2]):
        raise AuthError('reauthenticate')
    _profile_active(conn, profile_id)


def password_login(conn, name, password, user_agent='', profile_id=None):
    try:
        name = username(name)
    except AuthError:
        name = ''
    row = conn.execute('SELECT u.profile_id,p.password_hash,p.version FROM portal_auth_users u '
                       'JOIN portal_password_credentials p ON p.profile_id=u.profile_id '
                       'WHERE u.username=? COLLATE NOCASE AND p.revoked_at IS NULL', (name,)).fetchone()
    if not password_matches(row[1] if row else None, password) or (profile_id is not None and row[0] != profile_id):
        with transaction(conn):
            audit(conn, row[0] if row else None, 'login', 'password', 'refused')
        raise AuthError('sign_in_failed')
    with transaction(conn):
        current = conn.execute('SELECT password_hash,version FROM portal_password_credentials WHERE profile_id=? AND revoked_at IS NULL', (row[0],)).fetchone()
        if current is None or current[0] != row[1] or current[1] != row[2]:
            raise AuthError('sign_in_failed')
        return _create_session(conn, row[0], 'password', str(row[2]), user_agent)


def set_password(conn, profile_id, current, name, password):
    name = username(name)
    value = validate_password(password, name)
    require_recent(conn, profile_id, current)
    encoded = hash_password(value)
    try:
        with transaction(conn):
            require_recent(conn, profile_id, current)
            conn.execute('UPDATE portal_auth_users SET username=? WHERE profile_id=?', (name, profile_id))
            previous = conn.execute('SELECT version FROM portal_password_credentials WHERE profile_id=?', (profile_id,)).fetchone()
            version = previous[0] + 1 if previous else 1
            conn.execute('INSERT INTO portal_password_credentials(profile_id,password_hash,version,updated_at) VALUES (?,?,?,?) '
                         'ON CONFLICT(profile_id) DO UPDATE SET password_hash=excluded.password_hash,version=excluded.version,updated_at=excluded.updated_at,revoked_at=NULL',
                         (profile_id, encoded, version, time.time()))
            conn.execute('UPDATE portal_auth_sessions SET revoked_at=? WHERE profile_id=? AND id!=? AND revoked_at IS NULL', (time.time(), profile_id, current['sid']))
            token = SESSION_PREFIX + secrets.token_urlsafe(32)
            conn.execute("UPDATE portal_auth_sessions SET token_hash=?,method='password',credential=?,auth_at=? WHERE id=? AND profile_id=?", (digest(token), str(version), time.time(), current['sid'], profile_id))
            audit(conn, profile_id, 'password_changed', 'password', 'accepted')
        return token
    except sqlite3.IntegrityError:
        raise AuthError('username_unavailable') from None


def usable_methods(conn, profile_id):
    password = conn.execute('SELECT 1 FROM portal_password_credentials WHERE profile_id=? AND revoked_at IS NULL', (profile_id,)).fetchone()
    keys = conn.execute('SELECT count(*) FROM portal_passkeys WHERE profile_id=? AND revoked_at IS NULL', (profile_id,)).fetchone()[0]
    discord = conn.execute("SELECT 1 FROM portal_profile_providers WHERE profile_id=? AND provider='discord' AND revoked_at IS NULL", (profile_id,)).fetchone()
    return bool(password), keys, bool(discord)


def revoke_method(conn, profile_id, current, method, identifier=None):
    with transaction(conn):
        require_recent(conn, profile_id, current)
        password, keys, discord = usable_methods(conn, profile_id)
        count = int(password) + keys + int(discord)
        if count <= 1:
            raise AuthError('last_sign_in_method')
        if method == 'password':
            changed = conn.execute('UPDATE portal_password_credentials SET revoked_at=? WHERE profile_id=? AND revoked_at IS NULL', (time.time(), profile_id)).rowcount
        elif method == 'passkey':
            changed = conn.execute('UPDATE portal_passkeys SET revoked_at=? WHERE profile_id=? AND id=? AND revoked_at IS NULL', (time.time(), profile_id, identifier)).rowcount
        elif method == 'discord':
            changed = conn.execute("UPDATE portal_profile_providers SET revoked_at=? WHERE profile_id=? AND provider='discord' AND revoked_at IS NULL", (str(time.time()), profile_id)).rowcount
            if changed:
                conn.execute('UPDATE portal_auth_sessions SET link_version=link_version+1 WHERE profile_id=? '
                             'AND revoked_at IS NULL AND link_version=(SELECT a.session_version FROM portal_profile_accounts a '
                             'WHERE a.profile_id=portal_auth_sessions.profile_id AND a.account_id=portal_auth_sessions.account_id AND a.revoked_at IS NULL)', (profile_id,))
                conn.execute('UPDATE portal_profile_accounts SET session_version=session_version+1 WHERE profile_id=? AND revoked_at IS NULL', (profile_id,))
        else:
            raise AuthError('unknown_method')
        if changed != 1:
            raise AuthError('unknown_method')
        query = 'UPDATE portal_auth_sessions SET revoked_at=? WHERE profile_id=? AND method=? AND revoked_at IS NULL'
        args = [time.time(), profile_id, method]
        if method == 'passkey':
            query += ' AND credential=?'
            args.append(identifier)
        conn.execute(query, args)
        audit(conn, profile_id, 'method_removed', method, 'accepted')


def switch_session(conn, current, account_id):
    with transaction(conn):
        profile_id, session_id = current['pid'], current['sid']
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT * FROM portal_auth_sessions WHERE id=? AND token_hash=? AND profile_id=? AND revoked_at IS NULL AND expires_at>=?', (session_id, current['sh'], profile_id, time.time())).fetchone()
        context = identity.account_context(conn, profile_id, account_id)
        if row is None or context is None or not _credential_active(conn, profile_id, row['method'], row['credential']):
            raise AuthError('account_unavailable')
        token = SESSION_PREFIX + secrets.token_urlsafe(32)
        conn.execute('UPDATE portal_auth_sessions SET token_hash=?,account_id=?,link_version=? WHERE id=?',
                     (digest(token), account_id, context['lv'], session_id))
        return token


def revoke_session(conn, profile_id, current, target_id):
    with transaction(conn):
        require_recent(conn, profile_id, current)
        if conn.execute('UPDATE portal_auth_sessions SET revoked_at=? WHERE profile_id=? AND id=? AND revoked_at IS NULL', (time.time(), profile_id, target_id)).rowcount != 1:
            raise AuthError('session_unavailable')
        audit(conn, profile_id, 'session_revoked', 'session', 'accepted')


def security_summary(conn, profile_id, current_session):
    row = conn.execute('SELECT username FROM portal_auth_users WHERE profile_id=?', (profile_id,)).fetchone()
    password, _keys, discord = usable_methods(conn, profile_id)
    keys = [{'id': r[0], 'label': r[1], 'created_at': r[2], 'last_used': r[3]} for r in conn.execute(
        'SELECT id,label,created_at,last_used FROM portal_passkeys WHERE profile_id=? AND revoked_at IS NULL ORDER BY created_at', (profile_id,))]
    sessions = [{'id': r[0], 'method': r[1], 'created_at': r[2], 'last_seen': r[3], 'device': r[4], 'current': r[0] == current_session}
                for r in conn.execute('SELECT id,method,created_at,last_seen,user_agent FROM portal_auth_sessions '
                                      'WHERE profile_id=? AND revoked_at IS NULL AND expires_at>=? ORDER BY last_seen DESC LIMIT 30', (profile_id, time.time()))]
    return {'username': row[0] if row else None, 'password': password, 'discord_linked': discord, 'passkeys': keys, 'sessions': sessions}


def webauthn_settings():
    import config
    from urllib.parse import urlsplit
    origin = config.PORTAL_AUTH_ORIGIN.rstrip('/')
    parsed = urlsplit(origin)
    if parsed.scheme != 'https' or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise AuthError('auth_configuration')
    if parsed.hostname != config.PORTAL_AUTH_RP_ID:
        raise AuthError('auth_configuration')
    return config.PORTAL_AUTH_RP_ID, origin


def _client_origin(credential, expected):
    try:
        raw = credential['response']['clientDataJSON']
        if not isinstance(raw, str) or len(raw) > 8192:
            raise ValueError()
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        data = json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)), object_pairs_hook=unique)
        if data.get('origin') != expected or data.get('crossOrigin', False) is not False or data.get('topOrigin', expected) != expected:
            raise ValueError()
    except Exception:
        raise AuthError('passkey_failed') from None


def registration_options(conn, profile_id, current, binding, label, bootstrap=None):
    from webauthn import generate_registration_options
    from webauthn.helpers import options_to_json_dict
    from webauthn.helpers.structs import AuthenticatorSelectionCriteria, ResidentKeyRequirement, UserVerificationRequirement, PublicKeyCredentialDescriptor
    if bootstrap is None:
        require_recent(conn, profile_id, current)
        row = conn.execute('SELECT username,user_handle FROM portal_auth_users WHERE profile_id=?', (profile_id,)).fetchone()
        if row is None or not row[0]:
            raise AuthError('username_required')
        name, user_handle = row
        purpose = 'passkey-register'
        context = {'session_id': current['sid'], 'label': clean_label(label)}
    else:
        name, user_handle = bootstrap['username'], bytes.fromhex(bootstrap['user_handle'])
        purpose = 'passkey-bootstrap'
        context = {**bootstrap, 'label': clean_label(label)}
    existing = [r[0] for r in conn.execute('SELECT id FROM portal_passkeys WHERE profile_id=? AND revoked_at IS NULL', (profile_id,))]
    if len(existing) >= 8:
        raise AuthError('passkey_limit')
    rp_id, origin = webauthn_settings()
    challenge = secrets.token_bytes(32)
    context.update(rp_id=rp_id, origin=origin)
    identifier = new_challenge(conn, purpose, binding, profile_id, context, challenge)
    options = generate_registration_options(rp_id=rp_id, rp_name='Last Sietch', user_id=user_handle,
        user_name=name, user_display_name=name, challenge=challenge, timeout=300000,
        authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.REQUIRED,
                                                              user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))) for value in existing])
    return {'challenge_id': identifier, 'options': options_to_json_dict(options)}


def clean_label(value):
    value = value if isinstance(value, str) else ''
    return ''.join(c for c in value.strip()[:64] if c.isprintable()) or 'Passkey'


def _verify_registration(row, credential):
    from webauthn import verify_registration_response
    rp_id, origin = webauthn_settings()
    context = json.loads(row['context'])
    if context['rp_id'] != rp_id or context['origin'] != origin:
        raise AuthError('challenge_expired')
    _client_origin(credential, origin)
    try:
        return verify_registration_response(credential=credential, expected_challenge=row['challenge'],
            expected_rp_id=rp_id, expected_origin=origin, require_user_verification=True, require_user_presence=True)
    except Exception:
        raise AuthError('passkey_failed') from None


def _store_passkey(conn, profile_id, verified, label, credential):
    key_id = base64.urlsafe_b64encode(verified.credential_id).decode().rstrip('=')
    if conn.execute('SELECT count(*) FROM portal_passkeys WHERE profile_id=? AND revoked_at IS NULL', (profile_id,)).fetchone()[0] >= 8:
        raise AuthError('passkey_limit')
    transports = credential.get('response', {}).get('transports', [])
    transports = [t for t in transports if t in ('usb','nfc','ble','internal','hybrid','smart-card')][:6] if isinstance(transports, list) else []
    conn.execute('INSERT INTO portal_passkeys(id,profile_id,public_key,sign_count,label,transports,device_type,backed_up,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                 (key_id, profile_id, verified.credential_public_key, verified.sign_count, label,
                  json.dumps(transports), str(verified.credential_device_type.value), int(verified.credential_backed_up), time.time()))
    audit(conn, profile_id, 'passkey_added', 'passkey', 'accepted')
    return key_id


def finish_registration(conn, current, binding, identifier, credential):
    require_recent(conn, current['pid'], current)
    row = consume_challenge(conn, identifier, binding, 'passkey-register', current['pid'])
    context = json.loads(row['context'])
    if context['session_id'] != current['sid']:
        raise AuthError('challenge_expired')
    verified = _verify_registration(row, credential)
    try:
        with transaction(conn):
            require_recent(conn, current['pid'], current)
            _store_passkey(conn, current['pid'], verified, context['label'], credential)
            conn.execute('UPDATE portal_auth_sessions SET revoked_at=? WHERE profile_id=? AND id!=? AND revoked_at IS NULL', (time.time(), current['pid'], current['sid']))
            token = SESSION_PREFIX + secrets.token_urlsafe(32)
            conn.execute('UPDATE portal_auth_sessions SET token_hash=? WHERE id=?', (digest(token), current['sid']))
            return token
    except sqlite3.IntegrityError:
        raise AuthError('passkey_already_registered') from None


def authentication_options(conn, binding, profile_id=None):
    from webauthn import generate_authentication_options
    from webauthn.helpers import options_to_json_dict
    from webauthn.helpers.structs import UserVerificationRequirement
    rp_id, origin = webauthn_settings()
    challenge = secrets.token_bytes(32)
    identifier = new_challenge(conn, 'passkey-login', binding, profile_id, {'rp_id':rp_id,'origin':origin}, challenge)
    options = generate_authentication_options(rp_id=rp_id, challenge=challenge, timeout=300000,
                                              user_verification=UserVerificationRequirement.REQUIRED)
    return {'challenge_id': identifier, 'options': options_to_json_dict(options)}


def finish_authentication(conn, binding, identifier, credential, user_agent='', profile_id=None):
    from webauthn import verify_authentication_response
    row = consume_challenge(conn, identifier, binding, 'passkey-login', profile_id)
    rp_id, origin = webauthn_settings()
    context = json.loads(row['context'])
    if context != {'rp_id': rp_id, 'origin': origin}:
        raise AuthError('challenge_expired')
    _client_origin(credential, origin)
    key_id = credential.get('id')
    if not isinstance(key_id, str) or len(key_id) > 2048:
        raise AuthError('passkey_failed')
    key = conn.execute('SELECT profile_id,public_key,sign_count FROM portal_passkeys WHERE id=? AND revoked_at IS NULL', (key_id,)).fetchone()
    if key is None or (profile_id is not None and profile_id != key[0]):
        raise AuthError('passkey_failed')
    user_handle = credential.get('response', {}).get('userHandle')
    stored_handle = conn.execute('SELECT user_handle FROM portal_auth_users WHERE profile_id=?', (key[0],)).fetchone()
    if user_handle is not None:
        try:
            decoded = base64.urlsafe_b64decode(user_handle + '=' * (-len(user_handle) % 4))
        except Exception:
            raise AuthError('passkey_failed') from None
        if stored_handle is None or not hmac.compare_digest(decoded, stored_handle[0]):
            raise AuthError('passkey_failed')
    try:
        verified = verify_authentication_response(credential=credential, expected_challenge=row['challenge'], expected_rp_id=rp_id,
            expected_origin=origin, credential_public_key=key[1], credential_current_sign_count=key[2], require_user_verification=True)
    except Exception:
        raise AuthError('passkey_failed') from None
    with transaction(conn):
        updated = conn.execute('UPDATE portal_passkeys SET sign_count=?,last_used=?,device_type=?,backed_up=? '
                               'WHERE id=? AND profile_id=? AND sign_count=? AND revoked_at IS NULL',
                               (verified.new_sign_count, time.time(), verified.credential_device_type.value, int(verified.credential_backed_up), key_id, key[0], key[2])).rowcount
        if updated != 1:
            raise AuthError('passkey_failed')
        return _create_session(conn, key[0], 'passkey', key_id, user_agent)


def set_username(conn, current, value):
    value = username(value)
    try:
        with transaction(conn):
            require_recent(conn, current['pid'], current)
            conn.execute('UPDATE portal_auth_users SET username=? WHERE profile_id=?', (value, current['pid']))
    except sqlite3.IntegrityError:
        raise AuthError('username_unavailable') from None


def verified_game_ticket(conn, identifier, binding, purpose, profile_id=None):
    row = challenge_row(conn, identifier, binding, purpose, profile_id)
    if row['verified_at'] is None or not 0 <= time.time() - row['verified_at'] <= 600:
        raise AuthError('verification_required')
    return row, json.loads(row['context'])


def assert_new_account(conn, account_id):
    if conn.execute('SELECT 1 FROM portal_account_owners WHERE account_id=?', (account_id,)).fetchone() or conn.execute('SELECT 1 FROM ls_account_links WHERE account_id=?', (account_id,)).fetchone():
        raise AuthError('existing_account_sign_in')


def _bootstrap_profile(conn, ticket, context, name):
    assert_new_account(conn, context['account_id'])
    profile_id = context['profile_id']
    conn.execute('INSERT INTO portal_profiles(id) VALUES (?)', (profile_id,))
    conn.execute('INSERT INTO portal_auth_users(profile_id,username,user_handle,created_at) VALUES (?,?,?,?)',
                 (profile_id, name, bytes.fromhex(context['user_handle']), time.time()))
    conn.execute('INSERT INTO portal_account_owners(account_id,profile_id) VALUES (?,?)', (context['account_id'], profile_id))
    conn.execute("INSERT INTO portal_profile_accounts(profile_id,account_id,linked_at,session_version,character_name,verified_via) VALUES (?,?,datetime('now'),2,?,'game-whisper')",
                 (profile_id, context['account_id'], context['character_name']))
    if conn.execute('UPDATE portal_auth_challenges SET consumed_at=? WHERE id=? AND consumed_at IS NULL', (time.time(), ticket['id'])).rowcount != 1:
        raise AuthError('challenge_expired')
    return profile_id


def bootstrap_password(conn, identifier, binding, name, password, user_agent=''):
    name = username(name)
    value = validate_password(password, name)
    verified_game_ticket(conn, identifier, binding, 'game-onboard')
    encoded = hash_password(value)
    try:
        with transaction(conn):
            ticket, context = verified_game_ticket(conn, identifier, binding, 'game-onboard')
            profile_id = _bootstrap_profile(conn, ticket, context, name)
            conn.execute('INSERT INTO portal_password_credentials(profile_id,password_hash,updated_at) VALUES (?,?,?)', (profile_id, encoded, time.time()))
            return _create_session(conn, profile_id, 'password', '1', user_agent)
    except sqlite3.IntegrityError:
        raise AuthError('username_unavailable') from None


def bootstrap_passkey_options(conn, identifier, binding, name, label):
    name = username(name)
    _ticket, context = verified_game_ticket(conn, identifier, binding, 'game-onboard')
    assert_new_account(conn, context['account_id'])
    if conn.execute('SELECT 1 FROM portal_auth_users WHERE username=? COLLATE NOCASE', (name,)).fetchone():
        raise AuthError('username_unavailable')
    return registration_options(conn, context['profile_id'], None, binding, label,
                                bootstrap={**context, 'username': name, 'game_ticket': identifier})


def bootstrap_passkey(conn, identifier, binding, credential, user_agent=''):
    row = conn.execute('SELECT profile_id FROM portal_auth_challenges WHERE id=? AND purpose=?', (identifier, 'passkey-bootstrap')).fetchone()
    if row is None:
        raise AuthError('challenge_expired')
    challenge = consume_challenge(conn, identifier, binding, 'passkey-bootstrap', row[0])
    context = json.loads(challenge['context'])
    verified = _verify_registration(challenge, credential)
    try:
        with transaction(conn):
            ticket, original = verified_game_ticket(conn, context['game_ticket'], binding, 'game-onboard')
            if any(context[field] != original[field] for field in ('profile_id','account_id','user_handle','character_name')):
                raise AuthError('challenge_expired')
            profile_id = _bootstrap_profile(conn, ticket, original, context['username'])
            key_id = _store_passkey(conn, profile_id, verified, context['label'], credential)
            return _create_session(conn, profile_id, 'passkey', key_id, user_agent)
    except sqlite3.IntegrityError:
        raise AuthError('username_unavailable') from None


def link_verified_game(conn, current, identifier, binding):
    with transaction(conn):
        require_recent(conn, current['pid'], current)
        ticket, context = verified_game_ticket(conn, identifier, binding, 'game-link', current['pid'])
        aid = context['account_id']
        owner = conn.execute('SELECT profile_id FROM portal_account_owners WHERE account_id=?', (aid,)).fetchone()
        if owner and owner[0] != current['pid']:
            raise AuthError('existing_account_sign_in')
        existing = conn.execute('SELECT legacy_link_id,revoked_at FROM portal_profile_accounts WHERE profile_id=? AND account_id=?', (current['pid'], aid)).fetchone()
        if existing:
            if existing[1] is None:
                raise AuthError('account_already_linked')
            if existing[0] is not None:
                conn.execute("UPDATE ls_account_links SET revoked_at=NULL,revoked_by=NULL,revoke_reason=NULL,linked_at=datetime('now') WHERE id=?", (existing[0],))
            conn.execute("UPDATE portal_profile_accounts SET revoked_at=NULL,linked_at=datetime('now'),session_version=session_version+1,character_name=? WHERE profile_id=? AND account_id=?", (context['character_name'], current['pid'], aid))
        else:
            if conn.execute('SELECT 1 FROM ls_account_links WHERE account_id=?', (aid,)).fetchone():
                raise AuthError('existing_account_sign_in')
            conn.execute("INSERT INTO portal_profile_accounts(profile_id,account_id,linked_at,session_version,character_name,verified_via) VALUES (?,?,datetime('now'),2,?,'game-whisper')", (current['pid'], aid, context['character_name']))
        if owner is None:
            conn.execute('INSERT INTO portal_account_owners(account_id,profile_id) VALUES (?,?)', (aid, current['pid']))
        if current['aid'] == 0:
            version = conn.execute('SELECT session_version FROM portal_profile_accounts WHERE profile_id=? AND account_id=?', (current['pid'], aid)).fetchone()[0]
            conn.execute('UPDATE portal_auth_sessions SET account_id=?,link_version=? WHERE id=? AND token_hash=?', (aid, version, current['sid'], current['sh']))
        conn.execute('UPDATE portal_auth_challenges SET consumed_at=? WHERE id=? AND consumed_at IS NULL', (time.time(), ticket['id']))
        audit(conn, current['pid'], 'account_linked', 'game', 'accepted')


def unlink_account(conn, current, account_id):
    with transaction(conn):
        require_recent(conn, current['pid'], current)
        row = conn.execute('SELECT legacy_link_id FROM portal_profile_accounts WHERE profile_id=? AND account_id=? AND revoked_at IS NULL', (current['pid'], account_id)).fetchone()
        if row is None:
            raise AuthError('account_unavailable')
        if row[0] is not None:
            conn.execute("UPDATE ls_account_links SET revoked_at=datetime('now'),revoked_by='self',revoke_reason='player-unlinked' WHERE id=?", (row[0],))
        conn.execute("UPDATE portal_profile_accounts SET revoked_at=datetime('now'),session_version=session_version+1 WHERE profile_id=? AND account_id=?", (current['pid'], account_id))
        conn.execute('UPDATE portal_auth_sessions SET revoked_at=? WHERE profile_id=? AND account_id=? AND id!=?', (time.time(), current['pid'], account_id, current['sid']))
        if current['aid'] == account_id:
            survivor = conn.execute('SELECT account_id,session_version FROM portal_identity_links WHERE profile_id=? ORDER BY linked_at,account_id LIMIT 1', (current['pid'],)).fetchone()
            conn.execute('UPDATE portal_auth_sessions SET account_id=?,link_version=? WHERE id=?',
                         (survivor[0] if survivor else 0, survivor[1] if survivor else 0, current['sid']))
        audit(conn, current['pid'], 'account_unlinked', 'game', 'accepted')


def link_discord(conn, current, discord_id):
    if not identity.valid_discord_id(discord_id):
        raise AuthError('discord_failed')
    with transaction(conn):
        require_recent(conn, current['pid'], current)
        row = conn.execute("SELECT profile_id FROM portal_profile_providers WHERE provider='discord' AND subject=?", (discord_id,)).fetchone()
        if row and row[0] != current['pid']:
            raise AuthError('provider_in_use')
        existing = conn.execute("SELECT subject FROM portal_profile_providers WHERE profile_id=? AND provider='discord' AND revoked_at IS NULL", (current['pid'],)).fetchone()
        if existing and existing[0] != discord_id:
            raise AuthError('discord_already_linked')
        conn.execute("INSERT INTO portal_profile_providers(provider,subject,profile_id) VALUES ('discord',?,?) "
                     "ON CONFLICT(provider,subject) DO UPDATE SET revoked_at=NULL", (discord_id, current['pid']))
        audit(conn, current['pid'], 'provider_linked', 'discord', 'accepted')
