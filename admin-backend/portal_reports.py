import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
PERIODS = ('daily', 'weekly')
EDITION_ID = re.compile(r'^(daily|weekly)-\d{4}-\d{2}-\d{2}$')
PUBLIC_KEYS = ('schema', 'id', 'period', 'title', 'date', 'window', 'captured_at',
               'headline', 'summary', 'metrics', 'highlights', 'sections', 'stations',
               'activity', 'quality', 'links', 'spotlight')


def clean_text(value, limit=300):
    if not isinstance(value, str):
        return ''
    value = value.replace('\u2014', '-').replace('\u2013', '-')
    return ' '.join(''.join(c for c in value if c.isprintable() or c.isspace()).split())[:limit]


def number(value, integer=False):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('invalid_report_number')
    if integer and int(value) != value:
        raise ValueError('invalid_report_count')
    return int(value) if integer else round(value, 1)


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace('+00:00', 'Z')


def report_window(period, now=None):
    if period not in PERIODS:
        raise ValueError('invalid_report_period')
    now = time.time() if now is None else now
    local = datetime.fromtimestamp(now, ET)
    if period == 'daily':
        end = local.replace(hour=9, minute=0, second=0, microsecond=0)
        if end > local:
            end -= timedelta(days=1)
        start = end - timedelta(days=1)
    else:
        end = (local - timedelta(days=(local.weekday() + 1) % 7)).replace(
            hour=18, minute=0, second=0, microsecond=0)
        if end > local:
            end -= timedelta(days=7)
        start = end - timedelta(days=7)
    return {'id': period + '-' + end.date().isoformat(), 'period': period,
            'date': end.date().isoformat(), 'start': int(start.timestamp()),
            'end': int(end.timestamp())}


def _section(raw, key):
    item = raw.get(key)
    if not isinstance(item, dict) or item.get('status') != 'ok':
        return None
    return item.get('data')


def _name(value):
    value = clean_text(value, 80)
    return value if value and not re.fullmatch(r'acct \d+', value) else 'A fellow Sleeper'


def _amount(value):
    for scale, label in ((10**12, 'T'), (10**9, 'B'), (10**6, 'M'), (10**3, 'K')):
        if value >= scale:
            return f'{value / scale:.2f}{label}'
    return f'{value:,.0f}'


def build_edition(raw, window, previous=None, events=None):
    if (not isinstance(raw, dict) or raw.get('schema') != 2
            or raw.get('period') != window['period']
            or raw.get('window_start') != window['start'] or raw.get('window_end') != window['end']):
        raise ValueError('report_source_contract_mismatch')
    captured = number(raw.get('captured_at'), integer=True)
    if captured < window['end'] or captured > time.time() + 60:
        raise ValueError('report_capture_time_invalid')
    period = window['period']
    previous = previous if (previous and previous.get('period') == period
                            and previous.get('window', {}).get('end') == window['start']) else None
    old_metrics = {m['key']: m for m in previous.get('metrics', [])} if previous else {}
    metrics, highlights, sections, quality = [], [], [], []
    activity = _section(raw, 'activity')
    market = _section(raw, 'economy')
    arrivals = _section(raw, 'arrivals')
    losses = _section(raw, 'losses')
    spice = _section(raw, 'spice')
    changes = _section(raw, 'changes')
    stations_raw = _section(raw, 'stations')
    usable = sum(_section(raw, key) is not None for key in
                 ('activity', 'economy', 'arrivals', 'losses', 'spice', 'stations'))
    if not usable:
        raise ValueError('report_sources_unavailable')
    for key, label in [('arrivals', 'Welcome'), ('stations', 'Testing-station'),
                       ('losses', 'Death'), ('spice', 'Spice')]:
        if _section(raw, key) is None:
            quality.append(label + ' data was unavailable when this edition was prepared.')

    def metric(key, label, value, unit='', estimated=False):
        value = number(value)
        old = old_metrics.get(key)
        prior = old.get('value') if old else None
        delta = round(value - prior, 1) if type(prior) in (int, float) else None
        metrics.append({'key': key, 'label': label, 'value': value, 'unit': unit,
                        'estimated': estimated, 'previous': prior, 'change': delta})

    activity_points = []
    spotlight = None
    if activity is not None:
        metric('active_accounts', 'Players seen', activity['active_accounts'])
        metric('peak', 'Peak online', activity['peak'])
        metric('player_hours', 'Recorded player-hours', activity['player_hours'], 'h', True)
        points = activity.get('series', [])
        if not isinstance(points, list) or len(points) > 14:
            raise ValueError('invalid_report_series')
        seen_starts = set()
        for point in points:
            stamp = number(point['start'], True)
            if not window['start'] <= stamp < window['end'] or stamp in seen_starts:
                raise ValueError('invalid_report_series_window')
            seen_starts.add(stamp)
            activity_points.append({'label': clean_text(point['label'], 32),
                                    'start': stamp,
                                    'hours': number(point['hours']) if point.get('hours') is not None else None})
        candidates = activity.get('leaders', [])[:5]
        old_spotlight = previous.get('spotlight') if previous else None
        chosen = next((p for p in candidates if _name(p.get('name')) != old_spotlight), None)
        if chosen:
            spotlight = _name(chosen.get('name'))
            highlights.append({'key': 'wanderer', 'title': 'On the sands',
                               'body': f"{spotlight} logged approximately {number(chosen['hours']):.1f} player-hours this period.",
                               'kind': 'community'})
        if candidates:
            sections.append({'key': 'wanderers', 'title': 'Across the Sietch',
                             'description': 'Recorded playtime estimates, not a measure of contribution.',
                             'rows': [{'label': _name(p.get('name')), 'value': f"{number(p['hours']):.1f} h"}
                                      for p in candidates]})
        quality.append('Player-hours are estimated from five-minute presence samples across all maps.')
        quality.append('Players seen counts distinct recorded game accounts.')
        if activity.get('coverage') is not None:
            coverage = number(activity['coverage'])
            if coverage < 0.9:
                quality.append(f'Activity sampling covers approximately {min(100, round(coverage * 100))}% of the reporting window.')
        else:
            quality.append('Sampling coverage could not be verified; a gap is not proof of inactivity.')
    else:
        quality.append('Activity data was unavailable when this edition was prepared.')

    if arrivals is not None and number(arrivals.get('count', 0), True):
        count = number(arrivals['count'], True)
        names = [_name(n) for n in arrivals.get('names', [])[:5]]
        body = 'Welcome, ' + ', '.join(names) + '.' if names else f'{count} welcome packs were recorded this period.'
        highlights.insert(0, {'key': 'welcome', 'title': 'Welcome to the Sietch', 'body': body, 'kind': 'community'})
        quality.append('Welcomes are based on recorded welcome-pack grants, not a lifetime member census.')

    stations = []
    old_stations = {s['id']: s for s in previous.get('stations', [])} if previous else {}
    for row in (stations_raw or [])[:12]:
        sid = clean_text(row.get('id'), 80)
        tier = number(row.get('tier'), True)
        if not sid or not tier:
            continue
        old = old_stations.get(sid)
        prior = old.get('tier') if old else None
        changed = prior is not None and tier > prior
        station = {'id': sid, 'name': clean_text(row.get('name'), 100), 'tier': tier,
                   'runner': _name(row.get('runner')) if row.get('runner') else None,
                   'party_size': number(row.get('party_size', 0), True),
                   'previous_tier': prior, 'improved': changed}
        stations.append(station)
        if changed:
            highlights.insert(0, {'key': 'record-' + sid, 'title': 'A higher mark on the board',
                                 'body': f"{station['name']} rose from tier {prior} to {tier} since the previous published {period} edition.",
                                 'kind': 'record'})
    stations.sort(key=lambda s: (-s['tier'], s['name']))
    if stations:
        quality.append('Station tiers are standing records observed at collection time. A matching tier is not a new record.')
        quality.append('A recorded runner may be only one member of a group; the archive does not imply solo credit.')

    if market is not None:
        count = number(market.get('sales_count', 0), True)
        value = number(market.get('sale_value', 0))
        metric('sales_count', 'Sales recorded', count)
        metric('sale_value', 'Recorded sales value', value, 'Solari')
        if count:
            highlights.append({'key': 'market', 'title': 'CHOAM ledger', 'kind': 'market',
                               'body': f'{count:,} sale records carried {_amount(value)} Solari in logged value.'})
        sections.append({'key': 'economy', 'title': 'The Exchange ledger',
                         'description': 'Recorded sale and purchase activity are separate. These are not a count of unique two-party trades.',
                         'rows': [{'label': 'Sale records', 'value': f'{count:,}'},
                                  {'label': 'Recorded sales value', 'value': _amount(value) + ' Solari'},
                                  {'label': 'Purchase records', 'value': f"{number(market.get('purchase_count', 0), True):,}"}]})
        quality.append('Unscoped total wealth is omitted. Logged activity may include NPC transactions.')
    else:
        quality.append('Exchange data was unavailable when this edition was prepared.')

    if losses is not None:
        total = number(losses.get('total', 0), True)
        worm = losses.get('worm')
        rows = [{'label': 'Recorded deaths, all causes', 'value': str(total)}]
        if worm is not None:
            rows.append({'label': 'Confirmed Shai-Hulud deaths', 'value': str(number(worm, True))})
        if total or period == 'weekly':
            sections.append({'key': 'desert', 'title': 'The desert takes its due',
                             'description': 'Only confirmed killer classifications are attributed to Shai-Hulud.', 'rows': rows})
    if spice is not None:
        sections.append({'key': 'spice', 'title': 'Spice at publication',
                         'description': 'A collection-time snapshot, not a historical spawn-rate measurement.',
                         'rows': [{'label': 'Active fields', 'value': str(number(spice['active'], True))},
                                  {'label': 'Spawning enabled', 'value': f"{number(spice['enabled'], True)} of {number(spice['types'], True)} field types"}]})

    if changes:
        rows = [{'label': clean_text(c.get('text'), 260), 'value': ''} for c in changes[:6]
                if clean_text(c.get('text'), 260)]
        if rows:
            sections.append({'key': 'changes', 'title': 'From the keep',
                             'description': 'Published community improvement notes.', 'rows': rows})
            highlights.append({'key': 'improvements', 'title': 'From the keep', 'kind': 'news', 'body': rows[-1]['label']})
    event_rows = []
    for event in (events or [])[:3]:
        if event.get('status') != 'published' or type(event.get('id')) is not int:
            continue
        try:
            event_time = datetime.fromisoformat(str(event.get('starts_utc')).replace('Z', '+00:00'))
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=timezone.utc)
            event_label = event_time.astimezone(ET).strftime('%a, %b %-d • %-I:%M %p %Z')
        except ValueError:
            continue
        event_rows.append({'label': clean_text(event.get('title'), 160),
                           'value': event_label,
                           'href': '/events/' + str(event['id'])})
    if event_rows:
        sections.append({'key': 'events', 'title': 'On the horizon',
                         'description': 'Upcoming events as they appeared when this edition was published.', 'rows': event_rows})
        highlights.append({'key': 'event', 'title': 'On the horizon', 'kind': 'event',
                           'body': event_rows[0]['label'] + ' • ' + event_rows[0]['value']})
    if not any(h['kind'] in ('record', 'community', 'news', 'event') for h in highlights) and stations:
        pick = stations[int(hashlib.sha256(window['id'].encode()).hexdigest()[:8], 16) % len(stations)]
        highlights.insert(0, {'key': 'standing-target', 'title': 'A record to chase', 'kind': 'standing',
                             'body': f"{pick['name']} stands at tier {pick['tier']}. This is a standing mark, not a new result."})

    priorities = {'record': 0, 'news': 1, 'community': 2, 'event': 3, 'market': 4, 'standing': 5}
    highlights.sort(key=lambda h: priorities[h['kind']])
    limit = 3 if period == 'daily' else 6
    highlights = highlights[:limit]
    headline = highlights[0]['title'] if highlights else 'From across the Sietch'
    summary = highlights[0]['body'] if highlights else 'A quieter edition, with the available observations preserved below.'
    if not activity and not highlights:
        summary = 'This edition preserves the available observations; some sources could not be verified.'
    if period == 'weekly':
        headline = 'The week across the Sietch'
        parts = []
        if activity is not None:
            parts.append(f"{number(activity['active_accounts'], True)} recorded game accounts spent approximately {number(activity['player_hours']):.1f} player-hours across the worlds.")
        if market is not None and number(market.get('sales_count', 0), True):
            parts.append(f"The Exchange logged {number(market['sales_count'], True):,} sales records.")
        improved = sum(station['improved'] for station in stations)
        if improved:
            parts.append(f"{improved} standing station record{'s' if improved != 1 else ''} improved since the previous weekly edition.")
        if parts:
            summary = ' '.join(parts)
        elif not highlights:
            summary = 'This week’s edition preserves the available observations. Some sources could not be verified.'
    window_public = {'start': window['start'], 'end': window['end'],
                     'start_utc': iso(window['start']), 'end_utc': iso(window['end']),
                     'timezone': 'America/New_York'}
    report = {'schema': 1, 'id': window['id'], 'period': period,
              'title': 'Sietch Dispatch' if period == 'daily' else 'The Sietch Chronicle',
              'date': window['date'], 'window': window_public, 'captured_at': iso(captured),
              'headline': headline, 'summary': summary, 'metrics': metrics,
              'highlights': highlights, 'sections': sections, 'stations': stations,
              'activity': activity_points, 'quality': quality,
              'links': {'report': '/reports/' + window['id'], 'events': '/events', 'exchange': '/exchange'},
              'spotlight': spotlight}
    return {key: report[key] for key in PUBLIC_KEYS}


class ReportStore:
    def __init__(self, path):
        self.path = Path(path)

    def _connect(self, write=False):
        parent = self.path.parent
        info = parent.stat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError('report_storage_not_private')
        if not self.path.exists():
            if not write:
                return None
            try:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                os.close(descriptor)
            except FileExistsError:
                pass
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError('unsafe_report_database')
        con = sqlite3.connect(str(self.path) if write else self.path.as_uri() + '?mode=ro',
                              uri=not write, timeout=5)
        con.row_factory = sqlite3.Row
        if write:
            con.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS report_editions (
                    id TEXT PRIMARY KEY, period TEXT NOT NULL, window_start INTEGER NOT NULL,
                    window_end INTEGER NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
                    created_at INTEGER NOT NULL, published_at INTEGER, delivery_state TEXT NOT NULL DEFAULT 'draft',
                    attempt_token TEXT, nonce TEXT, channel_id TEXT, message_id TEXT, attempt_at INTEGER,
                    UNIQUE(period, window_end),
                    CHECK(delivery_state IN ('draft','pending','sent','uncertain'))
                );
                CREATE TRIGGER IF NOT EXISTS report_payload_immutable
                BEFORE UPDATE OF payload,digest,id,period,window_start,window_end ON report_editions
                WHEN OLD.published_at IS NOT NULL
                BEGIN SELECT RAISE(ABORT, 'published_report_is_immutable'); END;
            ''')
        return con

    def prepare(self, report, now=None):
        if (set(report) != set(PUBLIC_KEYS) or not EDITION_ID.fullmatch(report['id'])
                or report['id'] != report['period'] + '-' + report['date']):
            raise ValueError('invalid_report_payload')
        expected = report_window(report['period'], report['window']['end'])
        if (report['schema'] != 1 or expected['id'] != report['id']
                or expected['start'] != report['window']['start'] or expected['end'] != report['window']['end']):
            raise ValueError('invalid_report_window')
        payload = json.dumps(report, allow_nan=False, sort_keys=True, separators=(',', ':'))
        if len(payload.encode()) > 65536:
            raise ValueError('report_payload_too_large')
        digest = hashlib.sha256(payload.encode()).hexdigest()
        con = self._connect(True)
        try:
            with con:
                con.execute('INSERT OR IGNORE INTO report_editions '
                            '(id,period,window_start,window_end,payload,digest,created_at) VALUES (?,?,?,?,?,?,?)',
                            (report['id'], report['period'], report['window']['start'], report['window']['end'],
                             payload, digest, int(time.time() if now is None else now)))
            return self._row(con.execute('SELECT * FROM report_editions WHERE id=?', (report['id'],)).fetchone())
        finally:
            con.close()

    def _row(self, row):
        if row is None:
            return None
        if hashlib.sha256(row['payload'].encode()).hexdigest() != row['digest']:
            raise ValueError('report_integrity_error')
        edition = json.loads(row['payload'])
        if set(edition) != set(PUBLIC_KEYS) or edition.get('schema') != 1:
            raise ValueError('report_schema_unrecognized')
        return {'edition': edition, 'digest': row['digest'],
                'published_at': row['published_at'], 'delivery_state': row['delivery_state'],
                'attempt_token': row['attempt_token'], 'nonce': row['nonce'],
                'channel_id': row['channel_id'], 'message_id': row['message_id'], 'attempt_at': row['attempt_at']}

    def get(self, edition_id, public=True):
        if not isinstance(edition_id, str) or not EDITION_ID.fullmatch(edition_id):
            return None
        con = self._connect()
        if con is None:
            return None
        try:
            query = 'SELECT * FROM report_editions WHERE id=?'
            if public:
                query += ' AND published_at IS NOT NULL'
            return self._row(con.execute(query, (edition_id,)).fetchone())
        finally:
            con.close()

    def archive(self, period=None, before=None, limit=12):
        if period not in (None, *PERIODS) or type(limit) is not int or not 1 <= limit <= 24:
            raise ValueError('invalid_archive_query')
        if before is not None and (type(before) is not int or before < 1):
            raise ValueError('invalid_archive_cursor')
        con = self._connect()
        if con is None:
            return []
        try:
            clauses = ['published_at IS NOT NULL'];args = []
            if period:
                clauses.append('period=?');args.append(period)
            if before is not None:
                clauses.append('window_end<?');args.append(before)
            args.append(limit)
            return [self._row(row) for row in con.execute('SELECT * FROM report_editions WHERE '
                    + ' AND '.join(clauses) + ' ORDER BY window_end DESC,id DESC LIMIT ?', args)]
        finally:
            con.close()

    def pending(self, period):
        con = self._connect()
        if con is None:
            return None
        try:
            return self._row(con.execute("SELECT * FROM report_editions WHERE period=? AND delivery_state IN ('pending','uncertain') ORDER BY window_end LIMIT 1", (period,)).fetchone())
        finally:
            con.close()

    def claim(self, edition_id, channel_id, now=None):
        if not isinstance(channel_id, str) or not re.fullmatch(r'[0-9]{5,24}', channel_id):
            raise ValueError('invalid_report_channel')
        now = int(time.time() if now is None else now)
        con = self._connect(True)
        try:
            with con:
                con.execute('BEGIN IMMEDIATE')
                row = con.execute('SELECT * FROM report_editions WHERE id=?', (edition_id,)).fetchone()
                if row is None:
                    raise ValueError('report_not_prepared')
                acquired = row['delivery_state'] == 'draft'
                if acquired:
                    unresolved = con.execute("SELECT id FROM report_editions WHERE period=? AND delivery_state IN ('pending','uncertain') AND id<>? LIMIT 1",
                                             (row['period'], edition_id)).fetchone()
                    if unresolved:
                        raise ValueError('previous_report_delivery_unresolved')
                    con.execute("UPDATE report_editions SET published_at=?,delivery_state='pending',attempt_token=?,nonce=?,channel_id=?,attempt_at=? WHERE id=?",
                                (now, secrets.token_hex(24), secrets.token_hex(10), channel_id, now, edition_id))
                elif row['channel_id'] != channel_id:
                    raise ValueError('report_channel_changed')
                result = self._row(con.execute('SELECT * FROM report_editions WHERE id=?', (edition_id,)).fetchone())
                result['acquired'] = acquired
            return result
        finally:
            con.close()

    def acknowledge(self, edition_id, token, channel_id, message_id):
        if not isinstance(message_id, str) or not re.fullmatch(r'[0-9]{5,24}', message_id):
            raise ValueError('invalid_report_message')
        con = self._connect(True)
        try:
            with con:
                con.execute('BEGIN IMMEDIATE')
                row = con.execute('SELECT * FROM report_editions WHERE id=?', (edition_id,)).fetchone()
                if (row is None or not isinstance(token, str) or not secrets.compare_digest(row['attempt_token'] or '', token)
                        or row['channel_id'] != channel_id or row['delivery_state'] == 'draft'):
                    raise ValueError('report_delivery_identity_mismatch')
                if row['message_id'] is not None and row['message_id'] != message_id:
                    raise ValueError('report_delivery_message_changed')
                con.execute("UPDATE report_editions SET delivery_state='sent',message_id=? WHERE id=?", (message_id, edition_id))
        finally:
            con.close()

    def uncertain(self, edition_id, token):
        con = self._connect(True)
        try:
            with con:
                con.execute('BEGIN IMMEDIATE')
                row = con.execute('SELECT attempt_token,delivery_state FROM report_editions WHERE id=?', (edition_id,)).fetchone()
                if row is None or not isinstance(token, str) or not secrets.compare_digest(row['attempt_token'] or '', token) or row['delivery_state'] == 'draft':
                    raise ValueError('report_delivery_identity_mismatch')
                con.execute("UPDATE report_editions SET delivery_state='uncertain' WHERE id=? AND attempt_token=? AND delivery_state='pending'", (edition_id, token))
                return 'sent' if row['delivery_state'] == 'sent' else 'uncertain'
        finally:
            con.close()
