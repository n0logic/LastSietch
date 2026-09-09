from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('schedule', ROOT / 'scripts/server-schedule.py')
schedule = importlib.util.module_from_spec(spec)
spec.loader.exec_module(schedule)
NOW = datetime(2030, 1, 15, 0, 0, tzinfo=timezone.utc)


class ScheduleTests(unittest.TestCase):
    def test_no_schedule_is_invented(self):
        data = schedule.reading(NOW)
        self.assertEqual(set(data), {'available', 'server_now_utc', 'time_zone', 'coriolis'})
        self.assertEqual(data['coriolis'], {'available': False})
        self.assertEqual(data['time_zone'], 'UTC')

    def test_boundary_advances_to_next_cycle(self):
        data = schedule.reading(NOW, cycle_anchor='2030-01-01T00:00:00Z')
        self.assertEqual(data['coriolis']['cycle_start_utc'], NOW.isoformat())
        self.assertEqual(data['coriolis']['next_cycle_utc'], '2030-01-29T00:00:00+00:00')

    def test_future_anchor_and_offsets_use_absolute_time(self):
        data = schedule.reading(NOW, cycle_anchor='2030-01-16T09:00:00+09:00')
        self.assertEqual(data['coriolis']['next_cycle_utc'], '2030-01-16T00:00:00+00:00')

    def test_naive_bad_zone_and_bad_period_refuse(self):
        for kwargs in ({'now': datetime(2030, 1, 1)}, {'time_zone': 'Not/AZone'},
                       {'cycle_days': True}, {'cycle_days': 0}, {'cycle_days': 367},
                       {'cycle_anchor': '2030-01-01T00:00:00'}):
            with self.subTest(kwargs=kwargs), self.assertRaises((ValueError, KeyError)):
                schedule.reading(**kwargs)

    def test_cli_is_read_only_and_handles_bad_input(self):
        command = ['python3', str(ROOT / 'scripts/server-schedule.py')]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)['coriolis']['available'])
        result = subprocess.run(command + ['--time-zone', 'Not/AZone'], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(json.loads(result.stdout)['available'])

    def node(self, code):
        script = 'import * as u from ' + json.dumps((ROOT / 'web/server-time.mjs').as_uri()) + ';\n' + code
        result = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_display_handles_unavailable_schedule(self):
        data = schedule.reading(NOW)
        parsed = self.node('console.log(JSON.stringify(u.parseTimeReading(' + json.dumps(data) + ')));')
        self.assertIsNone(parsed['nextResetMs'])

    def test_display_rejects_ambiguous_clock(self):
        data = schedule.reading(NOW)
        data['server_now_utc'] = '2030-01-15T00:00:00'
        self.assertIsNone(self.node('console.log(JSON.stringify(u.parseTimeReading(' + json.dumps(data) + ')));'))

    def test_clock_uses_dst_rules_and_countdown_is_not_completion(self):
        result = self.node("console.log(JSON.stringify([u.serverClockText(Date.parse('2030-07-01T05:00:00Z'),'America/New_York'), u.serverClockText(Date.parse('2030-12-01T05:00:00Z'),'America/New_York'),u.resetCountdown(1000,1000),u.resetCountdown(61000,1000)]));")
        self.assertIn('EDT', result[0])
        self.assertIn('EST', result[1])
        self.assertEqual(result[2:], ['Scheduled time reached', '1m remaining'])


if __name__ == '__main__':
    unittest.main()
