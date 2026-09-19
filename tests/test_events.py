import json
import os
from pathlib import Path
import tempfile
import unittest

from ops import events


@unittest.skipUnless(os.name != 'posix' or os.geteuid() == 0, 'event log is root-private')
class EventLogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.path = self.root / 'events.jsonl'

    def test_records_are_validated_bounded_and_returned_newest_first(self):
        ticks = iter(range(100, 200))
        events.record('device-started', 'num01', 'attested start', path=self.path, clock=lambda: next(ticks))
        events.record('device-crashed', 'num01', 'exit\ncode 137 ' + 'x' * 400, path=self.path, clock=lambda: next(ticks))
        events.record('artifact-imported', None, 'com.whatsapp 2.24', path=self.path, clock=lambda: next(ticks))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        recent = events.recent(10, path=self.path)
        self.assertEqual([item['kind'] for item in recent], ['artifact-imported', 'device-crashed', 'device-started'])
        self.assertEqual(recent[1]['device'], 'num01')
        self.assertNotIn('\n', recent[1]['detail'])
        self.assertLessEqual(len(recent[1]['detail']), events.MAX_DETAIL)
        self.assertIsNone(recent[0]['device'])
        self.assertEqual([item['kind'] for item in events.recent(10, path=self.path, device='num01')],
                         ['device-crashed', 'device-started'])
        self.assertEqual(len(events.recent(1, path=self.path)), 1)
        for kind, device in (('device-exploded', 'num01'), ('device-started', 'dev01'), ('device-started', 'num01; rm')):
            with self.subTest(kind=kind, device=device), self.assertRaises(ValueError):
                events.record(kind, device, path=self.path)
        with self.assertRaises(ValueError):
            events.recent(0, path=self.path)

    def test_note_never_raises_and_malformed_lines_are_skipped(self):
        self.assertIsNone(events.note('device-started', 'not-a-device', path=self.path))
        self.assertIsNotNone(events.note('device-stopped', 'num02', path=self.path))
        with self.path.open('a', encoding='utf-8') as stream:
            stream.write('{"kind": "device-started", "device": "../x", "at": 1}\nnot json\n'
                         '{"kind": "surprise", "at": 2}\n{"kind": "device-ready", "device": null, "at": "3"}\n')
        self.assertEqual([item['kind'] for item in events.recent(path=self.path)], ['device-stopped'])
        self.path.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, 'private'):
            events.recent(path=self.path)

    def test_log_rotates_and_keeps_the_newest_entries(self):
        with self.path.open('w', encoding='utf-8') as stream:
            for index in range(events.MAX_LINES):
                stream.write(json.dumps({'at': index, 'kind': 'device-ready', 'device': 'num01', 'detail': None}) + '\n')
        self.path.chmod(0o600)
        events.record('device-started', 'num01', path=self.path, clock=lambda: 10 ** 6)
        lines = self.path.read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(lines), events.KEEP_LINES)
        self.assertEqual(json.loads(lines[-1])['kind'], 'device-started')
        self.assertEqual(json.loads(lines[0])['at'], events.MAX_LINES + 1 - events.KEEP_LINES)

    def test_environment_override_redirects_the_default_log(self):
        os.environ[events.PATH_ENVIRONMENT] = str(self.root / 'override.jsonl')
        try:
            events.note('device-ready', 'num03')
            self.assertEqual(events.recent()[0]['device'], 'num03')
            self.assertTrue((self.root / 'override.jsonl').exists())
        finally:
            del os.environ[events.PATH_ENVIRONMENT]
        self.assertEqual(events.default_path(), events.DEFAULT_PATH)


if __name__ == '__main__':
    unittest.main()
