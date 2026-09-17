import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

import config
from database import get_db
import portal_credentials as credentials
import portal_identity as identity
from portal_auth import (SESSION_COOKIE, CSRF_COOKIE, get_portal_session, set_cookie,
                         csrf_for_session, clear_cookie, client_ip)

router = APIRouter()
FLOW_COOKIE = '__Host-lastsietch-auth'
AUTH_HEADER = 'X-Portal-Auth-CSRF'
NO_STORE = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'}


def enabled(conn):
    if not (config.PORTAL_AUTH_ENABLED and config.PORTAL_SESSION_SECRET and identity.credentials_installed(conn)):
        return False
    try:
        credentials.require_private_storage(conn)
        return True
    except (credentials.AuthError, OSError):
        return False


def flow_cookie(request):
    value = request.cookies.get(FLOW_COOKIE, '')
    return value if re.fullmatch(r'[A-Za-z0-9_-]{43}', value) else ''


def binding_for(request):
    return request.cookies.get(SESSION_COOKIE) or flow_cookie(request)


def csrf(binding):
    return hmac.new(config.PORTAL_SESSION_SECRET.encode(),
                    ('auth-csrf:' + binding).encode(), hashlib.sha256).hexdigest()


def error_response(error, status=400):
    if error in ('reauthenticate', 'sign_in_required'):
        status = 401
    elif error in ('rate_limited', 'busy'):
        status = 429
    return JSONResponse({'ok': False, 'error': error}, status_code=status, headers=NO_STORE)


def attach_session(response, token):
    set_cookie(response, SESSION_COOKIE, token, max_age=credentials.SESSION_IDLE, httponly=True)
    set_cookie(response, CSRF_COOKIE, csrf_for_session(token), max_age=credentials.SESSION_IDLE, httponly=False)
    response.delete_cookie(FLOW_COOKIE, path='/', secure=True, httponly=True, samesite='lax')
    return response


@router.get('/portal/auth/status')
def status(request: Request):
    conn = get_db()
    try:
        if not enabled(conn):
            if config.PORTAL_AUTH_ENABLED:
                return error_response('auth_unavailable', 503)
            return JSONResponse({'ok': True, 'enabled': False, 'game_enabled': False}, headers=NO_STORE)
        credentials.webauthn_settings()
        current = get_portal_session(request)
        flow = flow_cookie(request) or secrets.token_urlsafe(32)
        binding = request.cookies.get(SESSION_COOKIE) or flow
        summary = None
        recent = False
        if current and current.get('pid'):
            summary = credentials.security_summary(conn, current['pid'], current.get('sid'))
            try:
                credentials.require_recent(conn, current['pid'], current)
                recent = True
            except credentials.AuthError:
                pass
        response = JSONResponse({'ok': True, 'enabled': True,
            'game_enabled': bool(config.PORTAL_GAME_AUTH_ENABLED), 'authenticated': bool(current),
            'recent': recent, 'security': summary, 'csrf': csrf(binding)}, headers=NO_STORE)
        if not request.cookies.get(SESSION_COOKIE):
            response.set_cookie(FLOW_COOKIE, flow, path='/', secure=True, httponly=True,
                                samesite='lax', max_age=1800)
        return response
    except Exception:
        return error_response('auth_unavailable', 503)
    finally:
        conn.close()


async def read_body(request):
    if request.headers.get('content-type', '').split(';')[0].strip() != 'application/json':
        raise credentials.AuthError('bad_request')
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 32768:
            raise credentials.AuthError('bad_request')
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError):
        raise credentials.AuthError('bad_request') from None
    if not isinstance(body, dict):
        raise credentials.AuthError('bad_request')
    return body


def current_profile(request):
    current = get_portal_session(request)
    if not current or not current.get('pid'):
        raise credentials.AuthError('sign_in_required')
    return current


def limit(conn, bucket, key, uses, seconds):
    if not credentials.rate_limit(conn, bucket, key, uses, seconds):
        raise credentials.AuthError('rate_limited')


def game_relay(job):
    try:
        response = httpx.post(config.RELAY_URL.rstrip('/') + '/dune/auth/whisper',
            headers={'X-API-Key': config.RELAY_API_KEY}, json=job, timeout=75)
        data = response.json() if response.status_code == 200 else None
        if not isinstance(data, dict) or data.get('ok') is not True:
            raise ValueError()
        return data
    except Exception:
        raise credentials.AuthError('game_verification_unavailable') from None


def game_action(conn, request, action, body, binding, current):
    if not config.PORTAL_GAME_AUTH_ENABLED:
        raise credentials.AuthError('game_verification_unavailable')
    linked = bool(current)
    profile_id = current.get('pid') if linked else None
    purpose = 'game-link' if linked else 'game-onboard'
    if linked:
        credentials.require_recent(conn, profile_id, current)
        if not config.MULTIACCOUNT_ENABLED and current.get('aid'):
            raise credentials.AuthError('additional_accounts_unavailable')
    if action == 'game-start':
        limit(conn, 'game-ip', client_ip(request), 5, 600)
        character = body.get('character')
        if not isinstance(character, str) or not 1 <= len(character.strip()) <= 80:
            raise credentials.AuthError('character_unavailable')
        player = game_relay({'op': 'lookup', 'character': character.strip()})
        aid, name = player.get('account_id'), player.get('character_name')
        if type(aid) is not int or aid <= 0 or not isinstance(name, str) or not 1 <= len(name) <= 80:
            raise credentials.AuthError('game_verification_unavailable')
        limit(conn, 'game-account', str(aid), 3, 1800)
        if not linked:
            credentials.assert_new_account(conn, aid)
        else:
            owner = conn.execute('SELECT profile_id FROM portal_account_owners WHERE account_id=?', (aid,)).fetchone()
            if owner and owner[0] != profile_id:
                raise credentials.AuthError('existing_account_sign_in')
            if identity.account_context(conn, profile_id, aid):
                raise credentials.AuthError('account_already_linked')
        code = str(secrets.randbelow(1000000)).zfill(6)
        context = {'account_id': aid, 'character_name': name, 'profile_id': profile_id or uuid.uuid4().hex,
                   'user_handle': secrets.token_bytes(64).hex()}
        identifier = credentials.new_challenge(conn, purpose, binding, profile_id, context, code=code)
        try:
            sent = game_relay({'op': 'send', 'account_id': aid, 'code': code})
            if sent.get('sent') is not True:
                raise credentials.AuthError('game_verification_unavailable')
        except Exception:
            credentials.consume_challenge(conn, identifier, binding, purpose, profile_id)
            raise
        return {'ok': True, 'challenge_id': identifier, 'character_name': name, 'expires_in': 300}, None
    identifier = body.get('challenge_id')
    if action == 'game-verify':
        credentials.verify_game_code(conn, identifier, binding, body.get('code'), purpose, profile_id)
        return {'ok': True, 'verified': True}, None
    if action == 'game-link' and linked:
        credentials.link_verified_game(conn, current, identifier, binding)
        return {'ok': True}, None
    if linked:
        raise credentials.AuthError('bad_request')
    if action == 'bootstrap-password':
        token = credentials.bootstrap_password(conn, identifier, binding, body.get('username'),
                                                body.get('password'), request.headers.get('user-agent',''))
        return {'ok': True}, token
    if action == 'bootstrap-passkey-options':
        return {'ok': True, **credentials.bootstrap_passkey_options(conn, identifier, binding,
                                                body.get('username'), body.get('label'))}, None
    if action == 'bootstrap-passkey':
        token = credentials.bootstrap_passkey(conn, identifier, binding, body.get('credential'),
                                               request.headers.get('user-agent',''))
        return {'ok': True}, token
    raise credentials.AuthError('bad_request')


def perform(request, action, body, binding):
    conn = get_db()
    try:
        if not enabled(conn):
            return error_response('auth_unavailable', 503)
        limit(conn, 'auth-ip', client_ip(request), 60, 600)
        agent = request.headers.get('user-agent', '')
        token = None
        result = {'ok': True}
        current = get_portal_session(request)
        profile_id = current.get('pid') if current else None
        if action == 'password-login':
            name = body.get('username', '')
            name = name.strip().casefold()[:64] if isinstance(name, str) else ''
            limit(conn, 'password-name', name, 10, 600)
            token = credentials.password_login(conn, name, body.get('password'), agent, profile_id=profile_id)
        elif action == 'passkey-options':
            result.update(credentials.authentication_options(conn, binding, profile_id))
        elif action == 'passkey-login':
            token = credentials.finish_authentication(conn, binding, body.get('challenge_id'),
                                                       body.get('credential'), agent, profile_id)
        elif action == 'discord-start':
            linking = body.get('link') is True
            if linking:
                current = current_profile(request)
                credentials.require_recent(conn, current['pid'], current)
            if not config.DISCORD_CLIENT_ID or not config.DISCORD_CLIENT_SECRET:
                raise credentials.AuthError('discord_unavailable')
            identifier = credentials.new_challenge(conn, 'discord', binding, profile_id,
                {'link': linking, 'session_id': current.get('sid') if current else None})
            result['url'] = 'https://discord.com/oauth2/authorize?' + urlencode({
                'response_type': 'code', 'client_id': config.DISCORD_CLIENT_ID, 'scope': 'identify',
                'state': 'pa3.' + identifier, 'prompt': 'consent',
                'redirect_uri': config.PORTAL_AUTH_ORIGIN.rstrip('/') + '/portal/oauth/callback'})
        elif action in ('game-start', 'game-verify', 'game-link', 'bootstrap-password',
                        'bootstrap-passkey-options', 'bootstrap-passkey'):
            result, token = game_action(conn, request, action, body, binding, current)
        else:
            current = current_profile(request)
            profile_id = current['pid']
            if action == 'password-save':
                token = credentials.set_password(conn, profile_id, current, body.get('username'), body.get('password'))
            elif action == 'username-save':
                credentials.set_username(conn, current, body.get('username'))
            elif action == 'passkey-register-options':
                result.update(credentials.registration_options(conn, profile_id, current, binding, body.get('label')))
            elif action == 'passkey-register':
                token = credentials.finish_registration(conn, current, binding, body.get('challenge_id'), body.get('credential'))
            elif action == 'method-remove':
                credentials.revoke_method(conn, profile_id, current, body.get('method'), body.get('id'))
                result['signed_out'] = credentials.session(conn, binding) is None
            elif action == 'session-revoke':
                credentials.revoke_session(conn, profile_id, current, body.get('id'))
                result['signed_out'] = body.get('id') == current.get('sid')
            elif action == 'account-unlink':
                account_id = body.get('account_id')
                if type(account_id) is not int or account_id <= 0:
                    raise credentials.AuthError('bad_request')
                credentials.unlink_account(conn, current, account_id)
            else:
                raise credentials.AuthError('bad_request')
        response = JSONResponse(result, headers=NO_STORE)
        if token:
            if current and current.get('aid') and action in ('password-login', 'passkey-login'):
                created = credentials.session(conn, token)
                if created['pid'] != current.get('pid'):
                    raise credentials.AuthError('sign_in_failed')
                token = credentials.switch_session(conn, created, current['aid'])
            attach_session(response, token)
        elif result.get('signed_out'):
            clear_cookie(response, SESSION_COOKIE)
            clear_cookie(response, CSRF_COOKIE)
        return response
    except credentials.AuthError as exc:
        return error_response(str(exc))
    except Exception:
        return error_response('auth_unavailable', 503)
    finally:
        conn.close()


@router.post('/portal/auth/{action}')
async def auth_action(request: Request, action: str):
    binding = binding_for(request)
    if (not config.PORTAL_SESSION_SECRET or not binding
            or request.headers.get('origin') != config.PORTAL_AUTH_ORIGIN.rstrip('/')
            or request.headers.get('sec-fetch-site', 'same-origin') != 'same-origin'
            or re.fullmatch('[a-f0-9]{64}', request.headers.get(AUTH_HEADER, '')) is None
            or not hmac.compare_digest(request.headers.get(AUTH_HEADER, ''), csrf(binding))):
        return error_response('csrf', 403)
    try:
        body = await read_body(request)
    except credentials.AuthError as exc:
        return error_response(str(exc))
    return await run_in_threadpool(perform, request, action, body, binding)


async def discord_callback(request, code, state, error):
    conn = get_db()
    try:
        if not enabled(conn) or error or not isinstance(code, str) or not 1 <= len(code) <= 2048:
            raise credentials.AuthError('discord_failed')
        binding = binding_for(request)
        if not binding or re.fullmatch(r'pa3\.[a-f0-9]{32}', state or '') is None:
            raise credentials.AuthError('challenge_expired')
        current = get_portal_session(request)
        profile_id = current.get('pid') if current else None
        row = credentials.consume_challenge(conn, state[4:], binding, 'discord', profile_id)
        context = json.loads(row['context'])
        if context['link']:
            credentials.require_recent(conn, profile_id, current)
            if context['session_id'] != current.get('sid'):
                raise credentials.AuthError('challenge_expired')
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post('https://discord.com/api/oauth2/token', data={
                'grant_type': 'authorization_code', 'code': code,
                'redirect_uri': config.PORTAL_AUTH_ORIGIN.rstrip('/') + '/portal/oauth/callback',
                'client_id': config.DISCORD_CLIENT_ID, 'client_secret': config.DISCORD_CLIENT_SECRET})
            if response.status_code != 200:
                raise credentials.AuthError('discord_failed')
            access = response.json().get('access_token')
            if not isinstance(access, str) or not access:
                raise credentials.AuthError('discord_failed')
            response = await client.get('https://discord.com/api/users/@me', headers={'Authorization': 'Bearer ' + access})
        if response.status_code != 200:
            raise credentials.AuthError('discord_failed')
        person = response.json()
        discord_id = person.get('id')
        if not identity.valid_discord_id(discord_id):
            raise credentials.AuthError('discord_failed')
        if context['link']:
            credentials.link_discord(conn, current, discord_id)
            return RedirectResponse('/settings?notice=discord-linked', 303, headers=NO_STORE)
        provider = conn.execute("SELECT profile_id,revoked_at FROM portal_profile_providers WHERE provider='discord' AND subject=?", (discord_id,)).fetchone()
        if provider is not None:
            if provider[1] is not None or (profile_id is not None and provider[0] != profile_id):
                raise credentials.AuthError('sign_in_failed')
            token = credentials.create_session(conn, provider[0], 'discord', discord_id, request.headers.get('user-agent',''),
                                               account_id=current.get('aid') if current and current.get('aid') else None)
            return attach_session(RedirectResponse('/settings', 303, headers=NO_STORE), token)
        if current:
            raise credentials.AuthError('sign_in_failed')
        from portal_auth import issue_link_flow, LINK_FLOW_COOKIE, csrf_for_link_flow
        flow = issue_link_flow(discord_id, person.get('global_name') or person.get('username') or '', auth_time=int(time.time()))
        response = RedirectResponse('/portal/link', 303, headers=NO_STORE)
        set_cookie(response, LINK_FLOW_COOKIE, flow, max_age=config.PORTAL_LINK_FLOW_MAX_AGE, httponly=True)
        set_cookie(response, CSRF_COOKIE, csrf_for_link_flow(flow), max_age=config.PORTAL_LINK_FLOW_MAX_AGE, httponly=False)
        return response
    except credentials.AuthError as exc:
        return RedirectResponse('/login?error=' + str(exc), 303, headers=NO_STORE)
    except Exception:
        return RedirectResponse('/login?error=discord_failed', 303, headers=NO_STORE)
    finally:
        conn.close()
