"""Read-only selection of the originating host log, not just later rejected retries."""
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import provisioner


class DiagnosticLogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.logs = Path(temporary.name)

    def log(self, stamp, action='up', body='request rejected\n'):
        path = self.logs / f'20260919T{stamp:06d}Z-{action}.log'
        path.write_text(body, encoding='utf-8')
        return path

    def test_keeps_older_actual_failure_and_last_three_rejections(self):
        origin = self.log(205500, body='Docker failed\nnum04: preparation stopped during guarded start; resume the identical request\n')
        self.log(205600, body='num05: preparation stopped during guarded start; resume the identical request\n')
        self.log(205700, 'up-num05', 'a different device failed\n')
        first = self.log(210000, 'up-num04')
        second = self.log(210030)
        third = self.log(210100, 'up-num04')
        before = origin.read_bytes()
        result = provisioner.diagnostic_job_logs(self.logs, 'num04')
        self.assertEqual([entry['name'] for entry in result], [origin.name, first.name, second.name, third.name])
        self.assertEqual([entry['association'] for entry in result], ['device', 'device', 'unscoped', 'device'])
        self.assertIn('Docker failed', result[0]['tail'])
        self.assertEqual(origin.read_bytes(), before)

    def test_only_latest_proven_failure_is_preserved_without_duplicate(self):
        self.log(200000, body='num04: preparation stopped during guarded start\n')
        latest = self.log(205500, body='num04: preparation stopped during application installation\n')
        self.log(210000, 'up-num04')
        self.log(210100, 'up-num04')
        result = provisioner.diagnostic_job_logs(self.logs, 'num04')
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]['name'], latest.name)

    def test_missing_origin_does_not_make_unscoped_log_a_device_failure(self):
        for index in range(5):
            self.log(210000 + index, body='guarded start failed\n')
        self.log(210006, body='prefix num04: preparation stopped during guarded start\n')
        result = provisioner.diagnostic_job_logs(self.logs, 'num04')
        self.assertEqual(len(result), 3)
        self.assertTrue(all(entry['association'] == 'unscoped' for entry in result))
        self.assertNotIn('20260919T210000Z-up.log', [entry['name'] for entry in result])

    def test_other_device_marker_is_not_a_match_or_an_unscoped_origin(self):
        self.log(210000, body='num40: preparation stopped during guarded start\n')
        self.log(210100, body='num05: preparation stopped during guarded start\n')
        self.log(210200, 'up-num05', 'num04: preparation stopped during guarded start\n')
        self.assertEqual(provisioner.diagnostic_job_logs(self.logs, 'num04'), [])

    def test_considers_only_newest_forty_regular_logs(self):
        origin = self.log(200000, body='num04: preparation stopped during guarded start\n')
        for index in range(41):
            self.log(210000 + index, 'up-num04')
        with patch.object(provisioner, '_diagnostic_log_tail', wraps=provisioner._diagnostic_log_tail) as reader:
            result = provisioner.diagnostic_job_logs(self.logs, 'num04')
        self.assertEqual(reader.call_count, 40)
        self.assertEqual(len(result), 3)
        self.assertNotIn(origin.name, [entry['name'] for entry in result])

    def test_read_limit_keeps_tail_of_large_logs_and_drops_partial_first_line(self):
        self.log(200000, body=('x' * (3 * 1024 * 1024)) + '\nnum04: preparation stopped during guarded start\nlast line\n')
        result = provisioner.diagnostic_job_logs(self.logs, 'num04', tail_lines=2)
        self.assertEqual(result[0]['association'], 'device')
        self.assertEqual(result[0]['tail'], ['num04: preparation stopped during guarded start', 'last line'])
        self.assertLess(sum(map(len, result[0]['tail'])), provisioner.DIAGNOSTIC_LOG_READ_BYTES)

        marker = b'num04: preparation stopped during guarded start\n'
        # The apparent marker is at the read boundary but in the middle of an actual line.
        payload = b'not a failure: ' + marker + b'x' * (provisioner.DIAGNOSTIC_LOG_READ_BYTES - len(marker))
        (self.logs / '20260919T210000Z-up.log').write_bytes(payload)
        result = provisioner.diagnostic_job_logs(self.logs, 'num04')
        self.assertEqual(result[-1]['association'], 'unscoped')

    def test_tail_limit_and_unreadable_files(self):
        path = self.log(210000, 'up-num04', ''.join(f'line {index}\n' for index in range(50)))
        self.assertEqual(provisioner.diagnostic_job_logs(self.logs, 'num04', tail_lines=3)[0]['tail'], ['line 47', 'line 48', 'line 49'])
        with patch.object(provisioner.os, 'open', side_effect=PermissionError('denied')):
            self.assertEqual(provisioner.diagnostic_job_logs(self.logs, 'num04'), [])
        self.assertTrue(path.exists())

    def test_directories_and_symlinks_are_ignored(self):
        (self.logs / '20260919T210000Z-up-num04.log').mkdir()
        target = self.logs / 'private-target.txt'
        target.write_text('num04: preparation stopped during guarded start\n', encoding='utf-8')
        link = self.logs / '20260919T210100Z-up.log'
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest('creating symlinks is unavailable on this platform')
        with patch.object(provisioner, '_diagnostic_log_tail', wraps=provisioner._diagnostic_log_tail) as reader:
            self.assertEqual(provisioner.diagnostic_job_logs(self.logs, 'num04'), [])
        reader.assert_not_called()
        self.assertIsNone(provisioner._diagnostic_log_tail(link))

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFO files require POSIX')
    def test_fifo_is_ignored_without_opening_it(self):
        os.mkfifo(self.logs / '20260919T210000Z-up-num04.log')
        with patch.object(provisioner.os, 'open') as opened:
            self.assertEqual(provisioner.diagnostic_job_logs(self.logs, 'num04'), [])
        opened.assert_not_called()

    def test_missing_directory_has_no_fabricated_logs(self):
        self.assertEqual(provisioner.diagnostic_job_logs(self.logs / 'missing', 'num04'), [])

    def test_text_output_marks_unscoped_and_accepts_old_report_shape(self):
        report = {'device': 'num04', 'allocated': True, 'record': {}, 'hold': None, 'wants_running': False,
                  'host': {'kernel': 'test', 'binderfs': True, 'binder_nodes': [], 'android_image': 'test',
                           'android_image_present': True, 'load_1m': 1, 'available_ram_gib': 4},
                  'files': {}, 'containers': {}, 'events': [],
                  'job_logs': [{'name': 'unscoped.log', 'tail': ['request rejected'], 'association': 'unscoped'},
                               {'name': 'old-shape.log', 'tail': ['recorded output']}]}
        output = io.StringIO()
        with redirect_stdout(output):
            provisioner.print_diagnosis(report)
        self.assertIn('unscoped.log [unscoped; device association not established]', output.getvalue())
        self.assertIn('job log old-shape.log:', output.getvalue())


if __name__ == '__main__':
    unittest.main()
