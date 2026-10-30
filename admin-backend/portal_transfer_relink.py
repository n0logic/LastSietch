"""Re-point portal links after a Funcom character transfer.

An incoming character transfer gives the player a NEW game account id; the FLS
id stays the same. Portal links are keyed on account id, so the old link points
at an account that no longer exists and the player lands on an empty portal.

The evidence comes from the game host, never from a guess here: the
grant/players payload carries `transfers`, pairs of (old_account_id,
new_account_id) built from dune.account_removal_log rows with reason
'incoming char transfer', joined to the live dune.accounts row with the same
FLS id, and only where the old account row is gone. The FLS id itself never
leaves the game host.

For every ACTIVE Discord link on an old account the plan is one of:

  * move    - link the same Discord to the new account through the identity
              layer (begin/finish_legacy_links, the quiz-pass path), revoke the
              old link as 'transfer-relink', re-point live sessions, carry
              active chat mutes, DM blocks and a directory opt-out forward.
  * retire  - the same Discord already links the new account (a manual fix,
              or a whisper link): only revoke the stale old link.
  * refuse  - ambiguous target, target linked or owned by someone else.

A tick that plans more changes than the cap applies none of them: a restore or
a bad read must never re-point links in bulk. Profile-native bindings (game
whisper links with no Discord link row) are counted, not changed.
"""
import asyncio
import json
import logging
import time

import portal_identity

log = logging.getLogger(__name__)

REASON = 'transfer-relink'


def transfer_pairs(payload):
    """{old_account_id: {new_account_id, ...}} from a grant/players payload, or
    None when the payload is unusable (relay down, old game-host script)."""
    if not isinstance(payload, dict) or payload.get('available') is not True:
        return None
    rows = payload.get('transfers')
    if not isinstance(rows, list):
        return None
    pairs = {}
    for row in rows:
        try:
            old, new = int(row['old_account_id']), int(row['new_account_id'])
        except (KeyError, TypeError, ValueError):
            continue
        if old > 0 and new > 0 and old != new:
            pairs.setdefault(old, set()).add(new)
    return pairs


def _profile_for_discord(conn, discord_id):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='portal_profiles'").fetchone():
        return None
    row = conn.execute('SELECT id FROM portal_profiles WHERE legacy_discord_id=?', (discord_id,)).fetchone()
    return row[0] if row else None


def plan(conn, pairs):
    links = conn.execute('SELECT id, discord_id, account_id, character_name, discord_handle, linked_at '
                         'FROM ls_account_links WHERE revoked_at IS NULL ORDER BY id').fetchall()
    active = {}
    for row in links:
        active.setdefault(row[2], set()).add(row[1])
    owners_installed = portal_identity.credentials_installed(conn)
    items = []
    for link_id, discord_id, old, name, handle, linked_at in links:
        if old not in pairs:
            continue
        item = {'link_id': link_id, 'discord_id': discord_id, 'old': old, 'new': None,
                'character_name': name, 'discord_handle': handle, 'linked_at': linked_at}
        targets = pairs[old]
        if len(targets) != 1:
            items.append({**item, 'action': 'refuse', 'why': 'ambiguous_target'})
            continue
        new = next(iter(targets))
        item['new'] = new
        # setdefault: a planned move claims the target, so a second old account
        # of the same player (A->C and B->C) retires instead of moving twice.
        holders = active.setdefault(new, set())
        if holders - {discord_id}:
            items.append({**item, 'action': 'refuse', 'why': 'target_linked_elsewhere'})
            continue
        if owners_installed:
            owner = conn.execute('SELECT profile_id FROM portal_account_owners WHERE account_id=?', (new,)).fetchone()
            if owner is not None and owner[0] != _profile_for_discord(conn, discord_id):
                items.append({**item, 'action': 'refuse', 'why': 'target_owned_elsewhere'})
                continue
        items.append({**item, 'action': 'retire' if discord_id in holders else 'move'})
        holders.add(discord_id)
    return items


def profile_native_orphans(conn, pairs):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='portal_profile_accounts'").fetchone():
        return 0
    rows = conn.execute('SELECT account_id FROM portal_profile_accounts '
                        'WHERE legacy_link_id IS NULL AND revoked_at IS NULL').fetchall()
    return sum(1 for (aid,) in rows if aid in pairs)


def _carry_forward(conn, old, new):
    conn.execute('INSERT INTO portal_chat_mutes(account_id, channel, by_kind, by_discord, reason, until_utc, created_utc) '
                 'SELECT ?, channel, by_kind, by_discord, reason, until_utc, created_utc FROM portal_chat_mutes '
                 'WHERE account_id=? AND lifted_utc IS NULL', (new, old))
    conn.execute('INSERT OR IGNORE INTO portal_message_block(account_id, blocked_account_id, created_at) '
                 'SELECT ?, blocked_account_id, created_at FROM portal_message_block WHERE account_id=?', (new, old))
    conn.execute('INSERT OR IGNORE INTO portal_message_block(account_id, blocked_account_id, created_at) '
                 'SELECT account_id, ?, created_at FROM portal_message_block WHERE blocked_account_id=?', (new, old))
    conn.execute('INSERT OR IGNORE INTO portal_player_profile(account_id, char_name, listed, blurb) '
                 'SELECT ?, char_name, 0, blurb FROM portal_player_profile WHERE account_id=? AND listed=0', (new, old))


def apply_item(conn, item, operator='system'):
    """One link, one transaction. Raises on refusal from the identity layer; the
    transaction is rolled back and nothing is written."""
    if item['action'] not in ('move', 'retire'):
        raise ValueError('not an applicable plan item')
    discord_id, old, new = item['discord_id'], item['old'], item['new']
    change = portal_identity.begin_legacy_links(conn, discord_id, allow_grant=item['action'] == 'move')
    if change is None and not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    try:
        if item['action'] == 'move':
            # Keep the old linked_at so the moved link keeps its place as the default character.
            conn.execute(
                """INSERT INTO ls_account_links
                     (discord_id, account_id, character_name, discord_handle, linked_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(discord_id, account_id) DO UPDATE SET
                     character_name = excluded.character_name,
                     discord_handle = excluded.discord_handle,
                     linked_at = excluded.linked_at,
                     revoked_at = NULL, revoked_by = NULL, revoke_reason = NULL""",
                (discord_id, new, item['character_name'], item['discord_handle'], item['linked_at']))
        revoked = conn.execute("UPDATE ls_account_links SET revoked_at=datetime('now'), revoked_by=?, revoke_reason=? "
                               'WHERE id=? AND account_id=? AND revoked_at IS NULL',
                               (operator, REASON, item['link_id'], old)).rowcount
        if revoked != 1:
            raise RuntimeError('link_changed_since_plan')
        portal_identity.finish_legacy_links(conn, change)
        profile_id = _profile_for_discord(conn, discord_id)
        if profile_id and portal_identity.credentials_installed(conn):
            binding = conn.execute('SELECT session_version FROM portal_profile_accounts '
                                   'WHERE profile_id=? AND account_id=? AND revoked_at IS NULL', (profile_id, new)).fetchone()
            if binding is not None:
                conn.execute('UPDATE portal_auth_sessions SET account_id=?, link_version=? '
                             'WHERE profile_id=? AND account_id=? AND revoked_at IS NULL',
                             (new, binding[0], profile_id, old))
            conn.execute('INSERT INTO portal_auth_audit(profile_id, event, method, result, occurred_at) '
                         "VALUES (?, 'account_relinked', 'transfer', 'accepted', ?)", (profile_id, time.time()))
        _carry_forward(conn, old, new)
        conn.execute("INSERT INTO audit_log(username, action, target, ip_address, details, success) "
                     "VALUES (?, 'portal_transfer_relink', ?, 'local', ?, 1)",
                     (operator, discord_id, json.dumps({'link_id': item['link_id'], 'old_account_id': old,
                                                        'new_account_id': new, 'action': item['action']})))
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def run(conn, pairs, apply=False, max_changes=3, operator='system'):
    """Plan, and when apply is set and the plan is under the cap, apply each item.
    Returns a summary with counts and per-item outcomes (ids only)."""
    items = plan(conn, pairs)
    todo = [i for i in items if i['action'] in ('move', 'retire')]
    summary = {'planned': len(todo), 'refused': len(items) - len(todo), 'applied': 0, 'failed': 0,
               'profile_native': profile_native_orphans(conn, pairs), 'capped': False, 'items': []}
    if apply and len(todo) > max_changes:
        summary['capped'] = True
        apply = False
    for item in items:
        outcome = {k: item[k] for k in ('link_id', 'old', 'new', 'action')}
        if item['action'] == 'refuse':
            outcome['result'] = item['why']
        elif not apply:
            outcome['result'] = 'planned'
        else:
            try:
                apply_item(conn, item, operator)
                outcome['result'] = 'applied'
                summary['applied'] += 1
            except Exception as exc:  # noqa: BLE001
                outcome['result'] = 'failed: ' + (str(exc) or type(exc).__name__)
                summary['failed'] += 1
        summary['items'].append(outcome)
    return summary


async def sync_loop(app):
    """Every TRANSFER_RELINK_INTERVAL seconds, fetch the transfer pairs from the
    relay and run the plan. Off unless TRANSFER_RELINK_ENABLED; log-only unless
    TRANSFER_RELINK_APPLY. A failed relay read is a skipped tick, never an
    empty plan."""
    import config
    from database import get_db
    from relay import call_relay

    if not config.TRANSFER_RELINK_ENABLED:
        log.info('transfer_relink: disabled (LASTSIETCH_TRANSFER_RELINK_ENABLED=0); loop not started')
        return
    log.info('transfer_relink: loop started (interval=%ss, apply=%s, max=%s)', config.TRANSFER_RELINK_INTERVAL,
             config.TRANSFER_RELINK_APPLY, config.TRANSFER_RELINK_MAX)

    def tick(pairs):
        conn = get_db()
        try:
            return run(conn, pairs, apply=config.TRANSFER_RELINK_APPLY, max_changes=config.TRANSFER_RELINK_MAX)
        finally:
            conn.close()

    last = None
    while True:
        await asyncio.sleep(config.TRANSFER_RELINK_INTERVAL)
        try:
            pairs = transfer_pairs(await call_relay('/dune/grant/players', timeout=45))
            if pairs is None:
                log.warning('transfer_relink: no usable transfers in the players payload; tick skipped')
                continue
            summary = await asyncio.to_thread(tick, pairs)
            line = json.dumps(summary, sort_keys=True)
            if (summary['items'] or summary['profile_native']) and line != last:
                log.warning('transfer_relink: %s', line)
            last = line
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning('transfer_relink: tick failed: %s', exc)
