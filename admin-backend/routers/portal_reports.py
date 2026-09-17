import asyncio
import hashlib
import io
import os
from pathlib import Path
import re
import secrets
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

import feature_flags
from config import DB_PATH
from portal_reports import ReportStore, build_edition, report_window, iso
from relay import call_relay
from routers.portal_events import _require_admin, list_events
from routers.portal import _v2_body_and_csrf

router = APIRouter()
_prepare_lock = asyncio.Lock()


def enabled():
    return bool(feature_flags.enabled('LASTSIETCH_REPORTS_ENABLED', '0'))


def store():
    return ReportStore(os.environ.get('LASTSIETCH_REPORTS_DB_PATH', str(Path(DB_PATH).parent / 'reports.sqlite3')))


def error(code, status=503):
    return JSONResponse({'ok': False, 'error': code}, status_code=status,
                        headers={'Cache-Control': 'private, no-store'})


def internal(request, require_enabled=True):
    expected = os.environ.get('LASTSIETCH_REPORT_PUBLISH_KEY', '')
    supplied = request.headers.get('X-Report-Key', '')
    return (not require_enabled or enabled()) and len(expected) >= 32 and len(supplied) <= 256 and secrets.compare_digest(expected.encode(), supplied.encode())


def public_edition(row):
    return {**row['edition'], 'published_at': iso(row['published_at']) if row['published_at'] else None,
            'digest': row['digest']}


async def collect_edition(period):
    window = report_window(period)
    previous = store().archive(period, before=window['end'], limit=1)
    previous = previous[0]['edition'] if previous else None
    query = f"/dune/stats/reports?period={period}&window_start={window['start']}&window_end={window['end']}"
    raw = await call_relay(query, timeout=95)
    try:
        events = list_events('upcoming', 3)
    except Exception:
        events = []
    return build_edition(raw, window, previous, events)


async def body(request, keys):
    raw = await request.body()
    if len(raw) > 2048:
        raise ValueError('report_request_too_large')
    value = await request.json()
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError('invalid_report_request')
    return value


async def preview_edition(period):
    existing = store().get(report_window(period)['id'], public=False)
    return existing['edition'] if existing else await collect_edition(period)


@router.get('/portal/reports')
async def reports_list(period: str = 'all', before: int | None = None, limit: int = 12):
    if not enabled():
        return error('reports_unavailable', 404)
    try:
        rows = store().archive(None if period == 'all' else period, before, limit)
        items = []
        for row in rows:
            e = public_edition(row)
            items.append({k: e[k] for k in ('id', 'period', 'title', 'date', 'window', 'headline',
                                            'summary', 'metrics', 'published_at')})
        return JSONResponse({'ok': True, 'editions': items,
                             'next_cursor': items[-1]['window']['end'] if len(items) == limit else None},
                            headers={'Cache-Control': 'public, max-age=60'})
    except ValueError:
        return error('invalid_archive_query', 400)
    except Exception:
        return error('reports_temporarily_unavailable')


@router.post('/portal/reports/admin/preview')
async def reports_preview(request: Request):
    gate, early = _require_admin(request)
    if early is not None:
        return early
    if not enabled():
        return error('reports_unavailable', 404)
    value, csrf_ok = await _v2_body_and_csrf(request)
    if not csrf_ok:
        return error('csrf', 403)
    if not isinstance(value, dict) or set(value) != {'period'} or value['period'] not in ('daily', 'weekly'):
        return error('invalid_report_period', 400)
    if _prepare_lock.locked():
        return error('report_preparation_busy', 409)
    try:
        async with _prepare_lock:
            edition = await preview_edition(value['period'])
        return JSONResponse({'ok': True, 'preview': True, 'edition': edition},
                            headers={'Cache-Control': 'private, no-store'})
    except Exception:
        return error('report_sources_unavailable')


@router.get('/portal/reports/admin/delivery')
async def reports_delivery(request: Request):
    gate, early = _require_admin(request)
    if early is not None:
        return early
    if not enabled():
        return error('reports_unavailable', 404)
    rows = store().archive(limit=12)
    return JSONResponse({'ok': True, 'deliveries': [{'id': r['edition']['id'], 'state': r['delivery_state'],
                                                    'message_id': r['message_id']} for r in rows]},
                        headers={'Cache-Control': 'private, no-store'})


def chart_png(edition):
    from PIL import Image, ImageDraw, ImageFont
    points = edition['activity']
    if len(points) < 2 or not any(p['hours'] is not None for p in points):
        return None
    image = Image.new('RGB', (1200, 440), '#15120e')
    draw = ImageDraw.Draw(image)
    title = ImageFont.load_default(size=30)
    body_font = ImageFont.load_default(size=22)
    small = ImageFont.load_default(size=20)
    draw.text((48, 28), 'LAST SIETCH  /  RECORDED PLAYER-HOURS', fill='#eadfc9', font=title)
    draw.text((48, 76), edition['date'] + '  |  ' + edition['title'], fill='#b6a589', font=body_font)
    maximum = max((p['hours'] or 0 for p in points), default=1) or 1
    left, top, bottom, width = 60, 138, 354, 1080
    step = width / len(points)
    for index, point in enumerate(points):
        x = left + index * step
        if point['hours'] is None:
            draw.line((x + 10, bottom - 2, x + step - 20, bottom - 2), fill='#776b59', width=2)
            label = 'gap'
        else:
            height = max(2, (bottom - top) * point['hours'] / maximum)
            draw.rounded_rectangle((x + 8, bottom - height, x + step - 20, bottom), radius=3, fill='#d4a574')
            label = f"{point['hours']:.1f} h"
        draw.text((x + 8, bottom + 14), point['label'], fill='#d6c7af', font=small)
        draw.text((x + 8, bottom + 40), label, fill='#a4947c', font=small)
    draw.text((48, 410), 'Five-minute sample estimates. Gaps are not recorded as zero activity.', fill='#a4947c', font=ImageFont.load_default(size=18))
    output = io.BytesIO();image.save(output, format='PNG')
    return output.getvalue()


@router.get('/portal/reports/{edition_id}/chart.png')
async def reports_chart(edition_id: str):
    if not enabled():
        return error('reports_unavailable', 404)
    try:
        row = store().get(edition_id)
        if row is None:
            return error('not_found', 404)
        data = chart_png(row['edition'])
        if data is None:
            return error('chart_unavailable', 404)
        return Response(data, media_type='image/png', headers={'Cache-Control': 'public, max-age=3600',
                        'ETag': '"' + hashlib.sha256(data).hexdigest() + '"'})
    except Exception:
        return error('chart_temporarily_unavailable')


@router.get('/portal/reports/{edition_id}')
async def reports_detail(edition_id: str):
    if not enabled():
        return error('reports_unavailable', 404)
    try:
        row = store().get(edition_id)
        if row is None:
            return error('not_found', 404)
        return JSONResponse({'ok': True, 'edition': public_edition(row)},
                            headers={'Cache-Control': 'public, max-age=3600', 'ETag': '"' + row['digest'] + '"'})
    except Exception:
        return error('reports_temporarily_unavailable')


@router.post('/_internal/reports/preview')
async def internal_preview(request: Request):
    if not internal(request):
        return error('not_found', 404)
    try:
        value = await body(request, ('period',))
        if value['period'] not in ('daily', 'weekly'):
            return error('invalid_report_period', 400)
        if _prepare_lock.locked():
            return error('report_preparation_busy', 409)
        async with _prepare_lock:
            edition = await preview_edition(value['period'])
        return JSONResponse({'ok': True, 'preview': True, 'edition': edition},
                            headers={'Cache-Control': 'private, no-store'})
    except Exception:
        return error('report_preview_unavailable')


@router.post('/_internal/reports/prepare')
async def internal_prepare(request: Request):
    if not internal(request):
        return error('not_found', 404)
    try:
        value = await body(request, ('period',))
        window = report_window(value['period'])
        pending = store().pending(value['period'])
        if pending:
            return JSONResponse({'ok': True, **pending}, headers={'Cache-Control': 'private, no-store'})
        existing = store().get(window['id'], public=False)
        if existing:
            return JSONResponse({'ok': True, **existing}, headers={'Cache-Control': 'private, no-store'})
        if _prepare_lock.locked():
            return error('report_preparation_busy', 409)
        async with _prepare_lock:
            edition = await collect_edition(value['period'])
            result = store().prepare(edition)
        return JSONResponse({'ok': True, **result}, headers={'Cache-Control': 'private, no-store'})
    except Exception:
        return error('report_preparation_unavailable')


@router.post('/_internal/reports/{edition_id}/claim')
async def internal_claim(request: Request, edition_id: str):
    if not internal(request):
        return error('not_found', 404)
    try:
        value = await body(request, ('channel_id',))
        expected = os.environ.get('LASTSIETCH_REPORT_DISCORD_CHANNEL_ID', '')
        if not re.fullmatch(r'[0-9]{5,24}', expected) or value['channel_id'] != expected:
            return error('report_channel_unavailable', 409)
        result = store().claim(edition_id, expected)
        return JSONResponse({'ok': True, **result}, headers={'Cache-Control': 'private, no-store'})
    except Exception:
        return error('report_claim_unavailable', 409)


@router.post('/_internal/reports/{edition_id}/ack')
async def internal_ack(request: Request, edition_id: str):
    if not internal(request, require_enabled=False):
        return error('not_found', 404)
    try:
        value = await body(request, ('attempt_token', 'channel_id', 'message_id'))
        store().acknowledge(edition_id, value['attempt_token'], value['channel_id'], value['message_id'])
        return JSONResponse({'ok': True, 'state': 'sent'}, headers={'Cache-Control': 'private, no-store'})
    except Exception:
        return error('report_acknowledgement_unavailable', 409)


@router.post('/_internal/reports/{edition_id}/uncertain')
async def internal_uncertain(request: Request, edition_id: str):
    if not internal(request, require_enabled=False):
        return error('not_found', 404)
    try:
        value = await body(request, ('attempt_token',))
        state = store().uncertain(edition_id, value['attempt_token'])
        return JSONResponse({'ok': True, 'state': state}, headers={'Cache-Control': 'private, no-store'})
    except Exception:
        return error('report_reconciliation_required', 409)
