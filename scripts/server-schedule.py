#!/usr/bin/env python3
import argparse
from datetime import datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo


def instant(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('timestamps must include a UTC offset')
    return parsed.astimezone(timezone.utc)


def reading(now=None, time_zone='UTC', cycle_anchor=None, cycle_days=14):
    now = now if now is not None else datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('clock must be timezone-aware')
    ZoneInfo(time_zone)
    if type(cycle_days) is not int or not 1 <= cycle_days <= 366:
        raise ValueError('cycle length must be an integer from 1 to 366 days')
    now = now.astimezone(timezone.utc)
    cycle = {'available': False}
    if cycle_anchor is not None:
        anchor = instant(cycle_anchor)
        duration = timedelta(days=cycle_days)
        start = anchor + ((now - anchor) // duration) * duration
        cycle = {'available': True, 'cycle_start_utc': start.isoformat(),
                 'next_cycle_utc': (start + duration).isoformat()}
    return {'available': True, 'server_now_utc': now.isoformat(),
            'time_zone': time_zone, 'coriolis': cycle}


def main():
    parser = argparse.ArgumentParser(description='Read-only clock and configured recurrence display. Never schedules a restart.')
    parser.add_argument('--time-zone', default='UTC', help='IANA display timezone')
    parser.add_argument('--cycle-anchor', help='operator-verified UTC-offset-qualified recurrence boundary')
    parser.add_argument('--cycle-days', type=int, default=14)
    args = parser.parse_args()
    print(json.dumps(reading(time_zone=args.time_zone, cycle_anchor=args.cycle_anchor, cycle_days=args.cycle_days)))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OverflowError):
        print(json.dumps({'available': False, 'error': 'invalid_clock_or_schedule_configuration'}))
        raise SystemExit(2)
