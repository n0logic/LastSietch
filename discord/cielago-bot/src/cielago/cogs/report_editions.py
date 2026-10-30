import asyncio
from datetime import datetime, timezone, timedelta
import math
import re
from urllib.parse import urlsplit

import discord
from discord.http import handle_message_parameters
import httpx

ID = re.compile(r'^(daily|weekly)-\d{4}-\d{2}-\d{2}$')


def text(value, limit=950):
    value = str(value).replace('\u2014', '-').replace('\u2013', '-')
    escaped = discord.utils.escape_mentions(discord.utils.escape_markdown(value))
    return escaped[:limit].rstrip('\\')


def origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
        raise ValueError('invalid_report_public_origin')
    return value.rstrip('/')


def validate(edition):
    if (not isinstance(edition, dict) or edition.get('schema') != 1
            or not isinstance(edition.get('id'), str) or not ID.fullmatch(edition['id'])
            or edition.get('period') not in ('daily', 'weekly')
            or not edition['id'].startswith(edition['period'] + '-')
            or edition.get('links') != {'report': '/reports/' + edition['id'], 'events': '/events', 'exchange': '/exchange'}):
        raise ValueError('invalid_report_edition')
    for metric in edition.get('metrics', []):
        if type(metric.get('value')) not in (int, float) or not math.isfinite(metric['value']) or metric['value'] < 0:
            raise ValueError('invalid_report_metric')
    return edition


def amount(value):
    for scale, suffix in ((10**12, 'T'), (10**9, 'B'), (10**6, 'M'), (10**3, 'K')):
        if value >= scale:
            return f'{value / scale:.2f}{suffix}'
    return f'{value:,.0f}'


def build_report_embed(edition, public_origin, preview=False):
    validate(edition);public_origin = origin(public_origin)
    description = '**' + text(edition['headline'], 160) + '**\n' + text(edition['summary'], 360)
    embed = discord.Embed(title=edition['title'], description=description,
                          url=public_origin + edition['links']['report'], color=0xD4A574)
    embed.set_author(name='Last Sietch', url=public_origin + '/reports')
    end = datetime.fromtimestamp(edition['window']['end'], timezone.utc)
    start = datetime.fromtimestamp(edition['window']['start'], timezone.utc)
    from zoneinfo import ZoneInfo
    zone = ZoneInfo('America/New_York')
    window_label = start.astimezone(zone).strftime('%b %d, %-I %p') + ' to ' + end.astimezone(zone).strftime('%b %d, %-I %p ET')
    embed.set_footer(text=('Preview • not published | ' if preview else 'Last Sietch | ') + window_label)
    embed.timestamp = end
    metrics = {m['key']: m for m in edition['metrics']}
    pulse = []
    if 'peak' in metrics:
        pulse.append(f"**{amount(metrics['peak']['value'])}** peak online")
    if 'active_accounts' in metrics:
        pulse.append(f"**{amount(metrics['active_accounts']['value'])}** players seen")
    if 'player_hours' in metrics:
        pulse.append(f"**~{metrics['player_hours']['value']:.1f} h** recorded player-time across all maps")
    if pulse:
        embed.add_field(name='The week in numbers' if edition['period'] == 'weekly' else 'Across the sands',
                        value='\n'.join(pulse), inline=False)
    selected = edition['highlights'] if edition['period'] == 'weekly' else edition['highlights'][1:]
    for item in selected:
        embed.add_field(name=text(item['title'], 200), value=text(item['body']), inline=False)
    if edition['period'] == 'weekly':
        changes = []
        for key in ('active_accounts', 'player_hours', 'sales_count', 'sale_value'):
            metric = metrics.get(key)
            if metric is None or metric.get('change') is None:
                continue
            delta = metric['change']
            direction = '+' if delta > 0 else ''
            changes.append(f"{text(metric['label'], 80)}: **{direction}{delta:,.1f}** {text(metric.get('unit', ''), 20)}")
        if changes:
            embed.add_field(name='Compared with the previous edition', value='\n'.join(changes), inline=False)
        if not preview and len(edition['activity']) > 1 and any(p.get('hours') is not None for p in edition['activity']):
            embed.set_image(url=public_origin + '/portal/reports/' + edition['id'] + '/chart.png')
    if len(embed) > 5800:
        raise ValueError('report_discord_payload_too_large')
    return embed


def report_links(edition, public_origin, preview=False):
    validate(edition);public_origin = origin(public_origin)
    view = discord.ui.View(timeout=None)
    for label, key in [('Events', 'events'), ('Exchange', 'exchange'), ('Full Report', 'report')]:
        view.add_item(discord.ui.Button(label=label, style=discord.ButtonStyle.link,
                                       url=public_origin + edition['links'][key],
                                       disabled=preview and key == 'report'))
    return view


def signature(embed):
    return (embed.title, embed.description, embed.url,
            [(field.name, field.value) for field in embed.fields])


class ReportPublisher:
    def __init__(self, bot, base_url, key, public_origin):
        if len(key) < 32:
            raise ValueError('report_publisher_key_missing')
        parsed = urlsplit(base_url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('invalid_report_api_origin')
        with handle_message_parameters(content='nonce capability check', nonce='report-check') as params:
            if params.payload.get('enforce_nonce') is not True:
                raise ValueError('discord_nonce_enforcement_unavailable')
        self.bot = bot
        self.base_url = base_url.rstrip('/')
        self.key = key
        self.public_origin = origin(public_origin)

    async def request(self, path, body):
        async with httpx.AsyncClient(timeout=110, follow_redirects=False) as client:
            response = await client.post(self.base_url + '/_internal/reports/' + path,
                                         json=body, headers={'X-Report-Key': self.key})
        if response.status_code != 200:
            raise ValueError('report_api_request_failed')
        value = response.json()
        if not isinstance(value, dict) or value.get('ok') is not True:
            raise ValueError('report_api_response_unverified')
        return value

    async def preview(self, period):
        value = await self.request('preview', {'period': period})
        return validate(value['edition'])

    async def acknowledge(self, row, message_id):
        edition_id = row['edition']['id']
        return await self.request(edition_id + '/ack', {'attempt_token': row['attempt_token'],
                                 'channel_id': row['channel_id'], 'message_id': str(message_id)})

    async def reconcile(self, row, channel, expected_embed):
        after = datetime.fromtimestamp(row['attempt_at'], timezone.utc) - timedelta(seconds=300)
        matches = []
        async for message in channel.history(limit=100, after=after, oldest_first=True):
            if self.bot.user is None or message.author.id != self.bot.user.id:
                continue
            if any(signature(embed) == signature(expected_embed) for embed in message.embeds):
                matches.append(message)
        if len(matches) != 1:
            return {'state': 'uncertain', 'edition_id': row['edition']['id']}
        await self.acknowledge(row, matches[0].id)
        return {'state': 'reconciled', 'edition_id': row['edition']['id'], 'message_id': matches[0].id}

    async def publish(self, period, channel):
        prepared = await self.request('prepare', {'period': period})
        edition = validate(prepared['edition'])
        embed = build_report_embed(edition, self.public_origin)
        view = report_links(edition, self.public_origin)
        channel_id = str(channel.id)
        row = await self.request(edition['id'] + '/claim', {'channel_id': channel_id})
        if (row.get('channel_id') != channel_id or row.get('edition') != edition
                or not isinstance(row.get('attempt_token'), str) or not row['attempt_token']
                or not isinstance(row.get('nonce'), str) or not 1 <= len(row['nonce']) <= 25):
            raise ValueError('report_claim_response_unverified')
        if row['delivery_state'] == 'sent':
            return {'state': 'already_sent', 'edition_id': edition['id'], 'message_id': row['message_id']}
        if row.get('acquired') is not True:
            return await self.reconcile(row, channel, embed)
        try:
            message = await asyncio.wait_for(channel.send(embed=embed, view=view, nonce=row['nonce'],
                            allowed_mentions=discord.AllowedMentions.none()), timeout=30)
            if str(message.channel.id) != channel_id or message.author.id != self.bot.user.id:
                raise ValueError('report_send_response_unverified')
            await self.acknowledge(row, message.id)
            return {'state': 'sent', 'edition_id': edition['id'], 'message_id': message.id}
        except Exception:
            try:
                await self.request(edition['id'] + '/uncertain', {'attempt_token': row['attempt_token']})
            except Exception:
                pass
            return {'state': 'uncertain', 'edition_id': edition['id']}
