"""Regression coverage for Binder preparation on Ubuntu cloud kernels."""

from contextlib import ExitStack, redirect_stdout
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import call, patch

from installer import install


KERNEL = "6.8.0-111-generic"


class BinderBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(redirect_stdout(io.StringIO()))
        self.patches.enter_context(patch("installer.install.platform.release", return_value=KERNEL))
        self.ready = self.patches.enter_context(patch(
            "installer.install.binderfs_available", side_effect=[False, True]))
        self.command = self.patches.enter_context(patch(
            "installer.install._command",
            return_value=subprocess.CompletedProcess([], 0, "", "")))
        self.run = self.patches.enter_context(patch(
            "installer.install.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "", "")))

    def test_registered_binderfs_allows_builtin_binder_without_package_or_module_lookup(self):
        self.ready.side_effect = None
        self.ready.return_value = True
        install.prepare_binder()
        self.command.assert_not_called()
        self.run.assert_not_called()

    def test_available_module_loads_without_apt_and_checks_binderfs_registration(self):
        install.prepare_binder()
        self.assertEqual(self.command.call_args_list, [
            call(["modinfo", "-k", KERNEL, "binder_linux"]),
            call(["modprobe", "binder_linux", "devices=binder,hwbinder,vndbinder"]),
        ])
        self.assertEqual(self.ready.call_count, 2)
        self.run.assert_not_called()

    def test_missing_module_installs_exact_running_kernel_extras_then_rechecks(self):
        self.command.side_effect = [
            subprocess.CompletedProcess([], 1, "", "module missing"),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
        install.prepare_binder()
        self.assertEqual([item.args[0] for item in self.run.call_args_list], [
            ["apt-get", "update"],
            ["apt-get", "install", "-y", "--no-install-recommends",
             "linux-modules-extra-6.8.0-111-generic"],
        ])
        for invocation in self.run.call_args_list:
            self.assertTrue(invocation.kwargs["check"])
            self.assertEqual(invocation.kwargs["env"]["DEBIAN_FRONTEND"], "noninteractive")
        self.assertEqual(self.command.call_args_list, [
            call(["modinfo", "-k", KERNEL, "binder_linux"]),
            call(["depmod", "-a", KERNEL], check=True, timeout=120),
            call(["modinfo", "-k", KERNEL, "binder_linux"]),
            call(["modprobe", "binder_linux", "devices=binder,hwbinder,vndbinder"]),
        ])
        self.assertEqual(self.ready.call_count, 2)

    def test_apt_failure_reports_recovery_without_reboot_or_modprobe(self):
        self.command.return_value = subprocess.CompletedProcess([], 1, "", "missing")
        self.run.side_effect = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CalledProcessError(100, ["apt-get", "install"]),
        ]
        with patch("installer.install._binder_recovery_hint", return_value="recovery instructions"):
            with self.assertRaisesRegex(RuntimeError, "recovery instructions") as error:
                install.prepare_binder()
        self.assertIsInstance(error.exception.__cause__, subprocess.CalledProcessError)
        self.command.assert_called_once_with(["modinfo", "-k", KERNEL, "binder_linux"])
        self.assertEqual([item.args[0][0] for item in self.run.call_args_list], ["apt-get", "apt-get"])

    def test_unavailable_module_after_successful_package_install_does_not_boot_android(self):
        self.command.side_effect = [
            subprocess.CompletedProcess([], 1, "", "missing"),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 1, "", "still missing"),
        ]
        with patch("installer.install._binder_recovery_hint", return_value="recovery instructions"):
            with self.assertRaisesRegex(RuntimeError, "recovery instructions"):
                install.prepare_binder()
        self.assertFalse(any(item.args[0][0] == "modprobe" for item in self.command.call_args_list))
        self.assertEqual(self.ready.call_count, 1)

    def test_missing_module_on_custom_kernel_does_not_guess_ubuntu_package(self):
        custom_kernel = "6.8.0-custom"
        self.command.return_value = subprocess.CompletedProcess([], 1, "", "missing")
        with patch("installer.install.platform.release", return_value=custom_kernel), \
                patch("installer.install._binder_recovery_hint", return_value="custom kernel recovery"):
            with self.assertRaisesRegex(RuntimeError, "custom kernel recovery"):
                install.prepare_binder()
        self.run.assert_not_called()
        self.command.assert_called_once_with(["modinfo", "-k", custom_kernel, "binder_linux"])

    def test_module_load_failure_is_actionable_and_does_not_unload(self):
        self.command.side_effect = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 1, "", "Operation not permitted"),
        ]
        with self.assertRaisesRegex(RuntimeError, "Operation not permitted"):
            install.prepare_binder()
        self.assertEqual([item.args[0] for item in self.command.call_args_list], [
            ["modinfo", "-k", KERNEL, "binder_linux"],
            ["modprobe", "binder_linux", "devices=binder,hwbinder,vndbinder"],
        ])
        self.run.assert_not_called()

    def test_loaded_module_without_binderfs_fails_without_unloading(self):
        self.ready.side_effect = [False, False]
        with self.assertRaisesRegex(RuntimeError, "CONFIG_ANDROID_BINDERFS"):
            install.prepare_binder()
        self.assertEqual(self.command.call_count, 2)
        self.assertNotIn("-r", self.command.call_args.args[0])
        self.run.assert_not_called()

    def test_prepare_host_repairs_binder_even_when_other_prerequisites_are_present(self):
        self.ready.side_effect = None
        self.ready.return_value = True
        with patch("installer.install._bootstrap_required", return_value=False), \
                patch("installer.install.shutil.which", return_value=None), \
                patch("installer.install.subprocess.check_output", return_value="2.33.1\n"), \
                patch("installer.install._atomic_write"), \
                patch("installer.install.probe_binderfs") as probe, \
                patch("installer.install.prepare_binder") as prepare:
            install.prepare_host(Path("unused-source"))
        prepare.assert_called_once_with()
        probe.assert_called_once_with()
        self.run.assert_not_called()

    def test_skipping_bootstrap_still_requires_and_probes_binderfs(self):
        self.ready.side_effect = None
        self.ready.return_value = True
        with patch("installer.install.probe_binderfs") as probe:
            install.prepare_host(Path("unused-source"), skip=True)
        probe.assert_called_once_with()
        self.command.assert_not_called()
        self.run.assert_not_called()
        self.ready.return_value = False
        with patch("installer.install.probe_binderfs") as probe:
            with self.assertRaisesRegex(RuntimeError, "CONFIG_ANDROID_BINDERFS"):
                install.prepare_host(Path("unused-source"), skip=True)
        probe.assert_not_called()


class BinderfsProbeTests(unittest.TestCase):
    def test_probe_is_disposable_private_and_never_checks_host_global_nodes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("installer.install._command",
                       return_value=subprocess.CompletedProcess([], 0, "", "")) as command:
                install.probe_binderfs(root)
            arguments = command.call_args.args[0]
            self.assertEqual(arguments[:10], ["unshare", "--mount", "--propagation", "private",
                                              "--pid", "--fork", "--kill-child", "--", "sh", "-ec"])
            self.assertIn('mount -t binder -o nosuid,noexec binder "$1"', arguments[10])
            self.assertIn('[ ! -c "$1/binder-control" ]', arguments[10])
            self.assertNotIn("/dev/binder", arguments[10])
            self.assertEqual(Path(arguments[-1]).parent, root)
            self.assertEqual(command.call_args.kwargs["timeout"], 30)
            self.assertEqual(list(root.iterdir()), [])

    def test_probe_failures_stop_install_and_cleanup_parent_directory(self):
        outcomes = [
            subprocess.CompletedProcess([], 1, "", "unshare: Operation not permitted"),
            subprocess.CompletedProcess([], 32, "", "mount: unknown filesystem type binder"),
            subprocess.CompletedProcess([], 1, "", "BinderFS did not create a character binder-control device"),
            subprocess.TimeoutExpired(["unshare"], 30),
            FileNotFoundError("unshare missing"),
        ]
        for outcome in outcomes:
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with patch("installer.install._command") as command:
                    if isinstance(outcome, Exception):
                        command.side_effect = outcome
                    else:
                        command.return_value = outcome
                    with self.assertRaisesRegex(RuntimeError, "BinderFS") as error:
                        install.probe_binderfs(root)
                if isinstance(outcome, subprocess.CompletedProcess):
                    self.assertIn(outcome.stderr, str(error.exception))
                self.assertEqual(list(root.iterdir()), [])


class BinderRecoveryHintTests(unittest.TestCase):
    def test_reboot_hint_only_includes_installed_bootable_kernel_with_binder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            modules = root / "modules"
            boot = root / "boot"
            modules.mkdir()
            boot.mkdir()
            with_binder = "6.8.0-112-generic"
            without_binder = "6.8.0-113-generic"
            without_image = "6.8.0-114-generic"
            for kernel in (KERNEL, with_binder, without_binder, without_image):
                (modules / kernel).mkdir()
                if kernel != without_image:
                    (boot / f"vmlinuz-{kernel}").touch()
            with patch("installer.install._binder_module_available",
                       side_effect=lambda name: name == with_binder) as available:
                hint = install._binder_recovery_hint(KERNEL, modules_root=modules, boot_root=boot)
            self.assertIn(with_binder, hint)
            self.assertIn("sudo reboot", hint)
            self.assertNotIn(without_binder, hint)
            self.assertNotIn(without_image, hint)
            self.assertEqual(available.call_args_list, [call(with_binder), call(without_binder)])

    def test_no_reboot_suggestion_when_no_alternative_has_binder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("installer.install._binder_module_available", return_value=False):
                hint = install._binder_recovery_hint(
                    KERNEL, modules_root=root / "missing-modules", boot_root=root / "missing-boot")
        self.assertIn(f"linux-modules-extra-{KERNEL}", hint)
        self.assertNotIn("sudo reboot", hint)


if __name__ == "__main__":
    unittest.main()
