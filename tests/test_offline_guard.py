# SPDX-License-Identifier: GPL-2.0-only
"""Offline checks must fail before any host command or host-path write."""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["DGX_FAN_CONTROL_OFFLINE"] = "1"

from test_fan_control_contract import CONTROL


class OfflineGuardTests(unittest.TestCase):
    def setUp(self):
        self.run = CONTROL.subprocess.run
        self.assertTrue(getattr(self.run, "_dgx_offline_guard", False))
        self.bare = self.run._bare
        self.before = self.run.real_invocations

        def spy(*args, **kwargs):
            raise AssertionError(f"escaped to exec: {args!r} {kwargs!r}")

        self.run._bare = spy
        self.addCleanup(self._restore)

    def _restore(self):
        self.run._bare = self.bare

    def test_polkit_commands_and_power_actions_do_not_exec(self):
        with self.assertRaises(CONTROL.OfflineGuardError) as caught:
            CONTROL.enable_manual_oneshot()
        self.assertIn("systemctl", str(caught.exception))
        with self.assertRaises(CONTROL.OfflineGuardError):
            CONTROL.disable_manual_oneshot()
        with self.assertRaises(CONTROL.OfflineGuardError):
            CONTROL.run_watchdog_action("systemctl poweroff")
        with self.assertRaises(CONTROL.OfflineGuardError):
            CONTROL.run_watchdog_action("systemctl reboot")
        for argv in (
            ["modprobe", CONTROL.MODULE_NAME],
            ["insmod", f"{CONTROL.MODULE_NAME}.ko"],
            ["rmmod", CONTROL.MODULE_NAME],
            ["fwupdmgr", "get-devices", "--json"],
            ["nvidia-smi"],
            ["dmesg"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(CONTROL.OfflineGuardError):
                    CONTROL.subprocess.run(argv)
        with self.assertRaises(CONTROL.OfflineGuardError):
            CONTROL.subprocess.run("systemctl reboot", shell=True)
        self.assertEqual(self.run.real_invocations, self.before)

    def test_fallthrough_mock_still_cannot_exec(self):
        saved = self.run

        def fallthrough(*args, **kwargs):
            return saved(*args, **kwargs)

        with patch.object(CONTROL.subprocess, "run", side_effect=fallthrough):
            with self.assertRaises(CONTROL.OfflineGuardError):
                CONTROL.enable_manual_oneshot()
        self.assertEqual(saved.real_invocations, self.before)

    def test_service_query_and_kernel_log_do_not_exec(self):
        self.assertEqual(
            CONTROL.systemctl_query("is-enabled", CONTROL.MANUAL_UNIT),
            "unknown",
        )
        self.assertEqual(CONTROL.systemctl_query("is-active"), "unknown")
        self.assertEqual(CONTROL.read_kernel_log(), "")
        self.assertIsNone(CONTROL.command_succeeded("systemctl reboot"))
        self.assertEqual(CONTROL.run_nvidia_smi(["nvidia-smi"])[0], None)
        self.assertEqual(self.run.real_invocations, self.before)

    def test_host_writes_fail_before_creating_files(self):
        def exploded(*_args, **_kwargs):
            raise AssertionError("host filesystem call")

        with patch.object(Path, "mkdir", side_effect=exploded), \
             patch.object(Path, "write_text", side_effect=exploded), \
             patch.object(Path, "read_text", side_effect=exploded), \
             patch.object(Path, "unlink", side_effect=exploded):
            with self.assertRaises(CONTROL.OfflineGuardError):
                CONTROL.write_owned_module_option(Path("/etc"), 6, 2700)
            with self.assertRaises(CONTROL.OfflineGuardError):
                CONTROL.clear_owned_module_option(Path("/etc"))
            with self.assertRaises(CONTROL.OfflineGuardError):
                CONTROL.write_state(Path("/sys/class/thermal/cooling_device0"), 6)
            with self.assertRaises(CONTROL.OfflineGuardError):
                CONTROL.write_acpi_fan_level(
                    Path("/sys/class/thermal/cooling_device0"), 100,
                )
            with self.assertRaises(CONTROL.OfflineGuardError):
                CONTROL.ensure_state_dir(Path("/var/lib/dgx-fan-control"))

    def test_module_and_defaults_do_not_read_the_host(self):
        def exploded(self, *_args, **_kwargs):
            raise AssertionError(f"host read {self}")

        with patch.object(Path, "is_dir", autospec=True, side_effect=exploded), \
             patch.object(Path, "read_text", autospec=True, side_effect=exploded):
            self.assertFalse(CONTROL.module_is_loaded())
            self.assertEqual(CONTROL.read_defaults(), {})

    def test_unpatched_process_signal_is_refused(self):
        def spy(*_args, **_kwargs):
            raise AssertionError("signaled")

        process = unittest.mock.Mock()
        process.pid = 4242
        with patch.object(CONTROL.os, "pidfd_open", return_value=9), \
             patch.object(CONTROL.os, "close"), \
             patch.object(CONTROL.os, "killpg", spy):
            with self.assertRaises(CONTROL.OfflineGuardError):
                CONTROL._kill_resume_group(process)


if __name__ == "__main__":
    unittest.main()
