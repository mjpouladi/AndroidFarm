import subprocess
import unittest

from ops import adb_helper
from ops.device_profiles import validate


PROFILE = validate({
    'schema_version': 2, 'android_version': 12, 'resolution': {'width': 720, 'height': 1280},
    'dpi': 240, 'fps': 20, 'device_model': 'Android Farm QA Phone HD', 'locale': 'fa-IR',
    'timezone': 'Asia/Tehran',
})


class FakeGuest:
    """Answers getprop/setprop/service checks like a booted Redroid guest."""

    def __init__(self, props=None, boot_after=0, framework_after=0):
        self.props = dict(props or {})
        self.calls = []
        self.boot_after = boot_after
        self.framework_after = framework_after

    def __call__(self, *argv, timeout=60):
        self.calls.append(argv)
        self.assertion = argv[:5]
        assert argv[:2] == ('docker', 'exec') and argv[3:5] == ('adb', '-s'), argv
        shell = list(argv[7:])
        if shell[:2] == ['getprop', 'sys.boot_completed']:
            self.boot_after -= 1
            return '1\n' if self.boot_after < 0 else '\n'
        if shell[0] == 'getprop':
            return self.props.get(shell[1], '') + '\n'
        if shell[0] == 'setprop':
            if shell[1] == 'ctl.restart':
                self.props['restarted'] = shell[2]
                return ''
            self.props[shell[1]] = shell[2]
            return ''
        if shell[:3] == ['service', 'check', 'activity']:
            self.framework_after -= 1
            return 'Service activity: found\n' if self.framework_after < 0 else 'Service activity: not found\n'
        if shell[:4] == ['settings', 'put', 'global', 'auto_time_zone']:
            self.props['auto_time_zone'] = shell[4]
            return ''
        raise AssertionError(f'unexpected guest command {shell}')


class AdbHelperTests(unittest.TestCase):
    def test_commands_are_fixed_argv_through_the_private_control_network(self):
        argv = adb_helper.command('num01', 'shell', 'getprop', 'ro.serialno')
        self.assertEqual(argv, ['docker', 'exec', 'screen-num01', 'adb', '-s', '10.232.0.2:5555',
                                'shell', 'getprop', 'ro.serialno'])
        self.assertEqual(adb_helper.target('num03'), '10.232.0.18:5555')
        with self.assertRaises(ValueError):
            adb_helper.command('dev01')

    def test_property_tokens_are_validated_before_reaching_the_guest(self):
        guest = FakeGuest()
        for name, value in (('persist.sys.timezone; reboot', 'UTC'), ('persist.sys.timezone', 'Asia/Tehran && rm'),
                            ('', 'UTC'), ('persist.sys.timezone', 'x' * 92), ('persist.sys.timezone', 7)):
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                adb_helper.setprop('num01', name, value, runner=guest)
        self.assertEqual(guest.calls, [])
        self.assertEqual(adb_helper.setprop('num01', 'persist.sys.timezone', 'Asia/Tehran', runner=guest), 'Asia/Tehran')
        guest.props['persist.sys.timezone'] = 'UTC'  # a rejected write is detected, never assumed

        def stubborn(*argv, timeout=60):
            guest.calls.append(argv)
            return 'UTC\n' if 'getprop' in argv else ''

        with self.assertRaisesRegex(RuntimeError, 'did not accept'):
            adb_helper.setprop('num01', 'persist.sys.timezone', 'Asia/Tehran', runner=stubborn)

    def test_wait_for_boot_is_bounded(self):
        guest = FakeGuest(boot_after=2)
        clock = iter(range(0, 1000, 5))
        adb_helper.wait_for_boot('num01', timeout=300, runner=guest, clock=lambda: next(clock), sleep=lambda _: None)
        self.assertEqual(sum(1 for call in guest.calls if call[-1] == 'sys.boot_completed'), 3)

        def never(*argv, timeout=60):
            raise subprocess.CalledProcessError(1, argv, 'device offline')

        ticks = iter([0, 100, 200, 301, 400])
        with self.assertRaisesRegex(RuntimeError, 'boot timed out'):
            adb_helper.wait_for_boot('num01', timeout=300, runner=never, clock=lambda: next(ticks), sleep=lambda _: None)

    def test_environment_is_applied_once_and_verified_afterwards(self):
        guest = FakeGuest(framework_after=1)
        clock = iter(range(0, 1000, 5))
        result = adb_helper.apply_environment('num01', PROFILE, runner=guest, clock=lambda: next(clock), sleep=lambda _: None)
        # The locale already reaches Android at boot; only the timezone is a persisted write by default.
        self.assertEqual(result['applied'], {'persist.sys.timezone': 'Asia/Tehran'})
        self.assertEqual(guest.props['persist.sys.timezone'], 'Asia/Tehran')
        self.assertNotIn('persist.sys.locale', guest.props)
        self.assertEqual(guest.props['auto_time_zone'], '0')
        self.assertEqual(guest.props['restarted'], 'zygote')
        with_locale = FakeGuest(props={'persist.sys.timezone': 'Asia/Tehran'}, framework_after=0)
        explicit = adb_helper.apply_environment('num01', PROFILE, runner=with_locale, include_locale=True,
                                                clock=lambda: next(clock), sleep=lambda _: None)
        self.assertEqual(explicit['applied'], {'persist.sys.locale': 'fa-IR'})
        # Second start: values already persisted, nothing is written and no restart happens.
        settled = FakeGuest(props={'persist.sys.timezone': 'Asia/Tehran', 'persist.sys.locale': 'fa-IR'})
        again = adb_helper.apply_environment('num01', PROFILE, runner=settled)
        self.assertEqual(again['applied'], {})
        self.assertNotIn('restarted', settled.props)
        self.assertTrue(all('setprop' not in call for call in settled.calls))
        # A profile without a timezone leaves the guest's zone alone.
        plain = validate({
            'schema_version': 1, 'android_version': 12, 'resolution': {'width': 720, 'height': 1280},
            'dpi': 240, 'fps': 20, 'device_model': 'Android Farm QA Phone HD', 'locale': 'en-US'})
        neutral = FakeGuest(props={'persist.sys.locale': 'en-US'})
        self.assertEqual(adb_helper.apply_environment('num01', plain, runner=neutral)['applied'], {})
        self.assertNotIn('persist.sys.timezone', neutral.props)

    def test_framework_restart_wait_is_bounded(self):
        guest = FakeGuest(props={'persist.sys.locale': 'fa-IR', 'persist.sys.timezone': 'UTC'}, framework_after=10 ** 6)
        ticks = iter([0, 0, 50, 100, 150, 181, 200, 250])
        with self.assertRaisesRegex(RuntimeError, 'framework did not return'):
            adb_helper.apply_environment('num01', PROFILE, runner=guest, clock=lambda: next(ticks), sleep=lambda _: None)


if __name__ == '__main__':
    unittest.main()
