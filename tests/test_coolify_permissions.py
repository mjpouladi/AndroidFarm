"""Exercise Linux permission checks even when the test runner is Windows."""

from contextlib import ExitStack
from dataclasses import replace
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from installer import install, quickstart


class PosixPermissions:
    """Keep real file IO, simulating only ownership and permission metadata.

    Patching the install module's os reference avoids changing global os.name,
    which would make pathlib instantiate unsupported PosixPaths on Windows.
    """

    def __init__(self):
        self.metadata = {}
        self.links = set()
        self.chmod_calls = []
        self.chown_calls = []
        self.original_stat = Path.stat
        self.original_chmod = Path.chmod
        self.stack = ExitStack()

    def set(self, path, uid, mode, gid=0):
        self.metadata[self._key(path)] = (uid, gid, mode)

    @staticmethod
    def _key(path):
        # resolve() expands Windows short-name temp paths before auth writes.
        return Path(os.path.realpath(Path(path).absolute()))

    def _stat(self, path, *, follow_symlinks=True):
        info = self.original_stat(path, follow_symlinks=follow_symlinks)
        values = list(info)
        key = self._key(path)
        if key in self.metadata:
            uid, gid, mode = self.metadata[key]
            values[0] = stat.S_IFMT(info.st_mode) | mode
            values[4], values[5] = uid, gid
        if not follow_symlinks and path.absolute() in self.links:
            values[0] = stat.S_IFLNK | stat.S_IMODE(values[0])
        return os.stat_result(values)

    def _chmod(self, path, mode, *, follow_symlinks=True):
        self.chmod_calls.append((path.absolute(), mode))
        info = self._stat(path, follow_symlinks=follow_symlinks)
        self.set(path, info.st_uid, mode, info.st_gid)
        self.original_chmod(path, mode, follow_symlinks=follow_symlinks)

    def _chown(self, path, uid, gid):
        path = Path(path)
        self.chown_calls.append((path.absolute(), uid, gid))
        self.set(path, uid, stat.S_IMODE(self._stat(path).st_mode), gid)

    def __enter__(self):
        simulated_os = Mock(wraps=os, spec=os)
        simulated_os.name = "posix"
        simulated_os.geteuid = Mock(return_value=0)
        simulated_os.chown = Mock(side_effect=self._chown)
        self.stack.enter_context(patch("installer.install.os", simulated_os))
        self.stack.enter_context(patch.object(
            Path, "stat", autospec=True, side_effect=self._stat))
        self.stack.enter_context(patch.object(
            Path, "lstat", autospec=True,
            side_effect=lambda path: self._stat(path, follow_symlinks=False)))
        self.stack.enter_context(patch.object(
            Path, "chmod", autospec=True, side_effect=self._chmod))
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)


class CoolifyPermissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.dynamic = self.root / "dynamic"
        self.dynamic.mkdir()
        self.users = self.dynamic / "farm-users.htpasswd"
        self.permissions = PosixPermissions()
        self.permissions.set(self.dynamic, 9999, 0o700)
        self.permissions.__enter__()
        self.addCleanup(self.permissions.__exit__, None, None, None)

    def test_reported_coolify_parent_requires_explicit_trusted_owner(self):
        with self.assertRaisesRegex(RuntimeError, "trusted owner"):
            install._atomic_write(self.users, b"operator:$2b$test\n", 0o600)
        self.assertFalse(self.users.exists())

        install._atomic_write(self.users, b"operator:$2b$test\n", 0o600,
                              parent_owner_uids=install.COOLIFY_OWNER_UIDS)
        self.assertEqual(self.users.read_bytes(), b"operator:$2b$test\n")
        self.assertEqual(self.users.stat().st_uid, 0)
        self.assertEqual(stat.S_IMODE(self.users.stat().st_mode), 0o600)
        self.assertIn((self.users.absolute(), 0, 0), self.permissions.chown_calls)
        self.assertEqual(self.dynamic.stat().st_uid, 9999)
        self.assertEqual(stat.S_IMODE(self.dynamic.stat().st_mode), 0o700)
        self.assertNotIn(self.dynamic.absolute(),
                         [path for path, _ in self.permissions.chmod_calls])
        self.assertNotIn(self.dynamic.absolute(),
                         [path for path, _, _ in self.permissions.chown_calls])

    def test_coolify_exception_rejects_other_owners_and_writable_parents(self):
        for uid, mode in ((1000, 0o700), (9999, 0o720), (9999, 0o702), (0, 0o777)):
            with self.subTest(uid=uid, mode=oct(mode)):
                self.permissions.set(self.dynamic, uid, mode)
                valid, _ = install.traefik_dynamic_status(self.dynamic)
                self.assertFalse(valid)
                with self.assertRaisesRegex(RuntimeError, "trusted owner"):
                    install._atomic_write(self.users, b"secret", 0o600,
                                          parent_owner_uids=install.COOLIFY_OWNER_UIDS)
                self.assertFalse(self.users.exists())

    def test_dynamic_directory_rejects_symlink_and_symlink_ancestor(self):
        for linked in (self.dynamic, self.root):
            with self.subTest(linked=linked):
                self.permissions.links.add(linked.absolute())
                valid, detail = install.traefik_dynamic_status(self.dynamic)
                self.assertFalse(valid)
                self.assertIn("symlink", detail)
                self.permissions.links.remove(linked.absolute())

    def test_private_auth_file_accepts_root_or_coolify_after_upgrade(self):
        self.users.write_text("operator:$2b$test\n", encoding="utf-8")
        for uid in (0, 9999):
            with self.subTest(uid=uid):
                self.permissions.set(self.users, uid, 0o600)
                valid, detail = install.traefik_users_status(self.users)
                self.assertTrue(valid, detail)
                self.assertEqual(len(install.auth_file_revision(self.users)), 64)

    def test_auth_file_rejects_arbitrary_owner_or_group_other_access(self):
        self.users.write_text("operator:$2b$test\n", encoding="utf-8")
        for uid, mode in ((1000, 0o600), (9999, 0o640), (0, 0o604), (9999, 0o610)):
            with self.subTest(uid=uid, mode=oct(mode)):
                self.permissions.set(self.users, uid, mode)
                self.assertFalse(install.traefik_users_status(self.users)[0])
                with self.assertRaisesRegex(RuntimeError, "unsafe"):
                    install.auth_file_revision(self.users)

    def test_auth_file_rejects_symlink_wrong_name_and_unsafe_parent(self):
        self.users.write_text("operator:$2b$test\n", encoding="utf-8")
        self.permissions.set(self.users, 0, 0o600)
        self.permissions.links.add(self.users.absolute())
        self.assertFalse(install.traefik_users_status(self.users)[0])
        self.permissions.links.clear()
        other = self.dynamic / "other.htpasswd"
        other.write_text("operator:$2b$test\n", encoding="utf-8")
        self.permissions.set(other, 0, 0o600)
        self.assertFalse(install.traefik_users_status(other)[0])
        self.permissions.set(self.dynamic, 1000, 0o700)
        self.assertFalse(install.traefik_users_status(self.users)[0])

    def test_generic_private_files_still_require_root(self):
        credential = self.root / "proxy-password"
        credential.write_text("private\n", encoding="utf-8")
        self.permissions.set(credential, 9999, 0o600)
        self.assertFalse(install.private_path_status(credential)[0])
        self.permissions.set(credential, 0, 0o600)
        self.assertTrue(install.private_path_status(credential)[0])

    def test_quickstart_preserves_coolify_owned_auth_without_plaintext_password(self):
        self.users.write_text("operator:$2b$existing-record\n", encoding="utf-8")
        self.permissions.set(self.users, 9999, 0o600)
        config = self.root / "config"
        config.mkdir()
        self.permissions.set(config, 0, 0o700)
        paths = replace(install.Paths(), config_dir=config,
                        traefik_dynamic_dir=self.dynamic)
        state = {}
        with patch("installer.quickstart.require_private_directory") as directory:
            self.assertEqual(quickstart.prepare_auth(paths, state), (None, None))
        directory.assert_called_once_with(config, "farm config directory", create=True)
        self.assertEqual(state, {})
        self.assertFalse((config / "web-login-password").exists())
        self.assertEqual(self.users.read_text(encoding="utf-8"),
                         "operator:$2b$existing-record\n")
        self.assertEqual(self.users.stat().st_uid, 9999)
        self.assertEqual(self.permissions.chown_calls, [])

        self.permissions.set(self.users, 1000, 0o600)
        with patch("installer.quickstart.require_private_directory"):
            with self.assertRaises(RuntimeError):
                quickstart.prepare_auth(paths, state)

    def test_configure_auth_creates_files_and_resumes_after_coolify_chown(self):
        source = self.root / "source"
        (source / "traefik").mkdir(parents=True)
        from services.api.credentials import DEFAULT_MIDDLEWARE, render_middleware
        middleware = DEFAULT_MIDDLEWARE
        (source / "traefik" / "farm-auth.yml").write_bytes(middleware)
        password = self.root / "password"
        password.write_text("test-only-password\n", encoding="utf-8")
        self.permissions.set(password, 0, 0o600)
        settings = install.Settings(
            source=source, farm_domain="farm.example.com",
            console_domain="farm.example.com", coolify_network="coolify",
            compose_project="android-farm",
            paths=replace(install.Paths(), traefik_dynamic_dir=self.dynamic),
            auth_user="operator", auth_password_file=password,
        )
        record = "operator:$2b$test-record\n"
        with patch("installer.install._command", return_value=
                   subprocess.CompletedProcess([], 0, record, "")) as command:
            initial = install.configure_traefik_auth(settings)
        self.assertEqual(command.call_args.args[0], ["htpasswd", "-niB", "operator"])
        self.assertEqual(self.users.read_text(encoding="utf-8"), record)
        self.assertEqual((self.dynamic / "farm-auth.yml").read_bytes(),
                         render_middleware(middleware, initial['revision']))
        self.assertEqual(stat.S_IMODE(self.users.stat().st_mode), 0o600)
        self.assertEqual(self.users.stat().st_uid, 0)

        # Coolify's upgrade can recursively restore its own UID on its tree.
        self.permissions.set(self.users, 9999, 0o600)
        previous_chowns = len(self.permissions.chown_calls)
        with patch("installer.install._command", return_value=
                   subprocess.CompletedProcess([], 0, "", "")) as command:
            resumed = install.configure_traefik_auth(settings)
        self.assertEqual(command.call_args.args[0],
                         ["htpasswd", "-vi", str(self.users), "operator"])
        self.assertEqual(command.call_count, 1)
        self.assertEqual(initial["revision"], resumed["revision"])
        self.assertEqual(self.users.stat().st_uid, 9999)
        self.assertNotIn((self.users.absolute(), 0, 0),
                         self.permissions.chown_calls[previous_chowns:])
        self.assertEqual(self.dynamic.stat().st_uid, 9999)
        self.assertEqual(stat.S_IMODE(self.dynamic.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
