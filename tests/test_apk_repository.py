import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from ops import apk_repository
from ops.apk_repository import (RepositoryError, derive_identifier, import_apk, list_apps, remove_app,
                                stage_upload, validate_catalog)
from ops.secureio import read_private_json


SIGNER_A = 'a' * 64
SIGNER_B = 'b' * 64
ZIP = b'PK\x03\x04'


def badging(package='com.whatsapp', version='2.24.1.75', code='241075', label='WhatsApp',
            activity='com.whatsapp.Main'):
    return (f"package: name='{package}' versionCode='{code}' versionName='{version}' platformBuildVersionName='14'\n"
            f"sdkVersion:'21'\napplication-label:'{label}'\n"
            f"launchable-activity: name='{activity}'  label='{label}' icon=''\n")


@unittest.skipUnless(os.name != 'posix' or os.geteuid() == 0, 'repository files must be root-private')
class ApkRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.repository = self.root / 'repository'
        self.catalog = self.root / 'apps.json'
        self.trust = self.root / 'apk-trust.json'
        self.signers = {}
        self.badging = {}
        self.calls = []

    def runner(self, *argv, timeout=120):
        self.calls.append(argv)
        path = argv[-1]
        if argv[0] == 'aapt':
            return self.badging.get(path, badging())
        if argv[0] == 'apksigner':
            digest = self.signers.get(path, SIGNER_A)
            if digest is None:
                raise subprocess.CalledProcessError(1, argv, 'DOES NOT VERIFY')
            return f'Signer #1 certificate DN: CN=WhatsApp\nSigner #1 certificate SHA-256 digest: {digest}\n'
        raise AssertionError('unexpected tool')

    def source(self, name='WhatsApp.apk', payload=b'apk-bytes-v1'):
        path = self.root / name
        path.write_bytes(ZIP + payload)
        path.chmod(0o600)
        return path

    def do_import(self, path, **options):
        return import_apk(path, repository=self.repository, catalog_path=self.catalog, trust_path=self.trust,
                          runner=self.runner, clock=lambda: 1_700_000_000, **options)

    def test_first_import_registers_catalog_trust_and_reuses_the_stored_file(self):
        result = self.do_import(self.source())
        self.assertEqual(result['id'], 'whatsapp')
        self.assertEqual(result['package'], 'com.whatsapp')
        self.assertEqual(result['label'], 'WhatsApp')
        self.assertEqual(result['version_name'], '2.24.1.75')
        self.assertEqual(result['signers'], [SIGNER_A])
        self.assertFalse(result['signer_changed'])
        stored = self.repository / f"{result['sha256']}.apk"
        self.assertTrue(stored.is_file())
        self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.repository.stat().st_mode & 0o777, 0o700)
        catalog = read_private_json(self.catalog, 'catalog')
        self.assertEqual(catalog['schema_version'], 1)
        entry = catalog['apps'][0]
        self.assertEqual(entry['apk_path'], str(stored))
        self.assertEqual(entry['apk_package'], 'com.whatsapp')
        self.assertEqual(entry['apk_activity'], 'com.whatsapp.Main')
        self.assertEqual(entry['apk_permissions'], [])
        self.assertEqual(entry['apk_signers'], [SIGNER_A])
        trust = read_private_json(self.trust, 'trust')
        self.assertEqual(trust['packages']['com.whatsapp']['signers'], [SIGNER_A])
        # The catalog validator used by the API accepts what the importer wrote.
        self.assertIn('whatsapp', validate_catalog(catalog))
        # Re-importing identical bytes is idempotent: one file, one entry.
        again = self.do_import(self.source('copy.apk'))
        self.assertEqual(again['sha256'], result['sha256'])
        self.assertEqual(len(list(self.repository.glob('*.apk'))), 1)
        self.assertEqual(len(list_apps(self.catalog)), 1)

    def test_new_version_replaces_the_entry_in_place_and_prunes_the_old_file(self):
        first = self.do_import(self.source(), permissions=['android.permission.CAMERA'], label='واتس‌اپ')
        newer = self.source('WhatsApp-new.apk', b'apk-bytes-v2')
        self.badging[str(self.repository / (apk_repository.hashlib.sha256(ZIP + b'apk-bytes-v2').hexdigest() + '.apk'))] = \
            badging(version='2.24.2.10', code='242010')
        second = self.do_import(newer)
        self.assertEqual(second['id'], first['id'])
        self.assertNotEqual(second['sha256'], first['sha256'])
        self.assertEqual(second['version_name'], '2.24.2.10')
        self.assertEqual(second['label'], 'واتس‌اپ')  # operator label survives a re-import
        self.assertEqual(second['permissions'], ['android.permission.CAMERA'])
        self.assertEqual(second['pruned'], [f"{first['sha256']}.apk"])
        self.assertEqual({path.name for path in self.repository.glob('*.apk')}, {f"{second['sha256']}.apk"})
        self.assertEqual(len(list_apps(self.catalog)), 1)

    def test_signer_rotation_is_refused_unless_explicitly_allowed(self):
        self.do_import(self.source())
        rotated = self.source('rotated.apk', b'apk-bytes-rotated')
        stored_name = apk_repository.hashlib.sha256(ZIP + b'apk-bytes-rotated').hexdigest() + '.apk'
        self.signers[str(self.repository / stored_name)] = SIGNER_B
        with self.assertRaisesRegex(RepositoryError, 'signer differs'):
            self.do_import(rotated)
        # The refused artifact does not linger in the repository.
        self.assertFalse((self.repository / stored_name).exists())
        self.assertEqual(read_private_json(self.trust, 'trust')['packages']['com.whatsapp']['signers'], [SIGNER_A])
        result = self.do_import(rotated, allow_signer_change=True)
        self.assertTrue(result['signer_changed'])
        trust = read_private_json(self.trust, 'trust')['packages']['com.whatsapp']
        self.assertEqual(trust['signers'], [SIGNER_B])
        self.assertEqual(trust['previous_signers'], [SIGNER_A])

    def test_unreadable_or_unsafe_sources_are_rejected_without_side_effects(self):
        plain = self.root / 'notes.txt'
        plain.write_bytes(b'not an archive')
        plain.chmod(0o600)
        with self.assertRaisesRegex(RepositoryError, 'ZIP'):
            self.do_import(plain)
        loose = self.source('loose.apk')
        loose.chmod(0o666)
        with self.assertRaisesRegex(RepositoryError, 'root-owned'):
            self.do_import(loose)
        link = self.root / 'link.apk'
        link.symlink_to(self.source('target.apk'))
        with self.assertRaisesRegex(RepositoryError, 'regular file'):
            self.do_import(link)
        unsigned = self.source('unsigned.apk', b'unsigned')
        self.signers[str(self.repository / (apk_repository.hashlib.sha256(ZIP + b'unsigned').hexdigest() + '.apk'))] = None
        with self.assertRaises(subprocess.CalledProcessError):
            self.do_import(unsigned)
        self.assertEqual(list(self.repository.glob('*.apk')), [])
        self.assertFalse(self.catalog.exists())
        self.assertFalse(self.trust.exists())
        for bad in ({'permissions': ['android.permission.ROOT']}, {'activity': '; sh'}, {'app_id': 'Bad Id'},
                    {'label': 'x' * 81}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.do_import(self.source(), **bad)

    def test_identifier_conflicts_and_removal(self):
        self.do_import(self.source())
        other = self.source('other.apk', b'other-package')
        self.badging[str(self.repository / (apk_repository.hashlib.sha256(ZIP + b'other-package').hexdigest() + '.apk'))] = \
            badging(package='com.example.other', label='Other')
        with self.assertRaisesRegex(RepositoryError, 'already belongs'):
            self.do_import(other, app_id='whatsapp')
        result = self.do_import(other)
        self.assertEqual(result['id'], 'other')
        self.assertEqual(sorted(app['id'] for app in list_apps(self.catalog)), ['other', 'whatsapp'])
        removed = remove_app('whatsapp', catalog_path=self.catalog, repository=self.repository)
        self.assertEqual(len(removed['pruned']), 1)
        self.assertEqual([app['id'] for app in list_apps(self.catalog)], ['other'])
        with self.assertRaises(KeyError):
            remove_app('whatsapp', catalog_path=self.catalog, repository=self.repository)

    def test_identifier_derivation_is_always_valid(self):
        self.assertEqual(derive_identifier('com.whatsapp'), 'whatsapp')
        self.assertEqual(derive_identifier('org.telegram.messenger'), 'messenger')
        self.assertEqual(derive_identifier('com.example.QA_App'), 'qa-app')
        self.assertEqual(derive_identifier('com.example.x1'), 'x1-app')
        for value in ('com.example.a1', 'com.example.Numbers_9', 'io.x.Y' + 'z' * 80):
            with self.subTest(value=value):
                self.assertRegex(derive_identifier(value), apk_repository.IDENTIFIER_RE)
        with self.assertRaises(ValueError):
            derive_identifier('not a package')

    def test_catalog_validator_keeps_the_strict_shape_with_optional_metadata(self):
        app = {'id': 'qa-app', 'label': 'QA', 'apk_path': '/opt/apks/qa.apk', 'apk_sha256': 'A' * 64,
               'apk_package': 'com.example.qa'}
        entries = validate_catalog({'schema_version': 1, 'apps': [dict(app, apk_version_name='1.0', apk_version_code=3,
                                                                       apk_signers=[SIGNER_A], imported_at=5)]})
        self.assertEqual(entries['qa-app']['apk_sha256'], 'a' * 64)
        for broken in (dict(app, extra='field'), dict(app, apk_signers=['short']), dict(app, apk_version_code=-1),
                       dict(app, apk_path='../x.apk'), dict(app, id='Bad')):
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                validate_catalog({'schema_version': 1, 'apps': [broken]})
        with self.assertRaises(ValueError):
            validate_catalog({'schema_version': 2, 'apps': []})

    def test_stage_upload_is_private_bounded_and_prunes_stale_files(self):
        staging = self.root / 'web-uploads'
        token = stage_upload(io.BytesIO(ZIP + b'payload'), 4 + len(b'payload'), staging, clock=lambda: 1_000_000)
        path = staging / f'{token}.apk'
        self.assertRegex(token, r'^[0-9a-f]{32}$')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(staging.stat().st_mode & 0o777, 0o700)
        with self.assertRaisesRegex(ValueError, 'announced size'):
            stage_upload(io.BytesIO(ZIP), 10, staging)
        with self.assertRaisesRegex(ValueError, 'not an APK'):
            stage_upload(io.BytesIO(b'text-file'), 9, staging)
        with self.assertRaisesRegex(ValueError, 'outside'):
            stage_upload(io.BytesIO(ZIP), 0, staging)
        with self.assertRaisesRegex(ValueError, 'outside'):
            stage_upload(io.BytesIO(ZIP), apk_repository.MAX_SIZE + 1, staging)
        # Only the successful upload remains; a day later it is pruned.
        self.assertEqual([item.name for item in staging.iterdir()], [f'{token}.apk'])
        old = time.time() - 90_000
        os.utime(path, (old, old))
        stage_upload(io.BytesIO(ZIP + b'x'), 5, staging)
        self.assertFalse(path.exists())
        self.assertEqual(len(list(staging.iterdir())), 1)


if __name__ == '__main__':
    unittest.main()
