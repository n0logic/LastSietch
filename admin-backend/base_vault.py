import asyncio
import json
import os
import uuid

from fastapi import HTTPException

COMMAND = ('/usr/bin/sudo', '-n', '--', '/usr/bin/python3', '-I',
           '/opt/lastsietch-base-vault/current/admin-helper.py')


def validate_totem(totem):
    if type(totem) is not int or not 0 < totem < 2 ** 63:
        raise HTTPException(400, 'Invalid base ID')


def validate_capture(body):
    try:
        if (not isinstance(body, dict) or set(body) != {'request_id'}
                or not isinstance(body['request_id'], str)
                or str(uuid.UUID(body['request_id'])) != body['request_id']):
            raise ValueError()
        return body['request_id']
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(400, 'A canonical capture request ID is required')


async def call(request):
    if os.environ.get('LASTSIETCH_BASE_VAULT_ENABLED') != '1':
        raise HTTPException(503, 'Base Vault is unavailable.')
    process = None
    try:
        process = await asyncio.create_subprocess_exec(*COMMAND, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        raw, _ = await asyncio.wait_for(process.communicate(json.dumps(request).encode()),
                                       timeout=90 if request.get('action') == 'restore_plan' else 30)
        if process.returncode or len(raw) > 131072:
            raise ValueError()
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError()
        error = result.get('error')
        if error == 'plan_not_found':
            raise HTTPException(404, 'Restore plan not found')
        if error in ('capture_busy', 'request_conflict', 'capture_disabled'):
            raise HTTPException(409, error)
        if error:
            raise ValueError()
        return result
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, 'Base Vault is unavailable. Refresh history before trying again.')
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
