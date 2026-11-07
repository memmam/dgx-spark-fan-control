# SPDX-License-Identifier: GPL-2.0-only
"""Offline checks must fail before any host command or host-path write."""

import os
import tempfile
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

    def test_host_sysfs_does_not_reclassify_a_fixture(self):
        real_glob = Path.glob

        def guarded_glob(self, pattern):
            text = os.path.abspath(self)
            if text == "/sys/class/hwmon" or text.startswith("/sys/class/hwmon" + os.sep):
                raise AssertionError("read host hwmon")
            if text.startswith("/sys/firmware") or text.startswith("/sys/class/thermal"):
                raise AssertionError(f"read host sysfs {text}")
            return real_glob(self, pattern)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            thermal = root / "thermal"
            fan = thermal / "cooling_device0"
            fan.mkdir(parents=True)
            (fan / "type").write_text("Fan\n", encoding="ascii")
            (fan / "cur_state").write_text("40\n", encoding="ascii")
            (fan / "max_state").write_text("100\n", encoding="ascii")
            with patch.object(Path, "glob", guarded_glob):
                self.assertIsNone(CONTROL.find_fan_hwmon(Path("/sys/class/hwmon")))
                self.assertEqual(
                    CONTROL.classify_fan_binding(thermal, Path("/sys/class/hwmon")),
                    "absent",
                )
                self.assertIsNone(CONTROL.acpi_ec_version())
                self.assertEqual(CONTROL.cooling_devices(Path("/sys/class/thermal")), [])
                self.assertEqual(CONTROL.read_named_fan_rpms(Path("/sys/class/hwmon")), [])
            hwmon = root / "hwmon" / "hwmon0"
            hwmon.mkdir(parents=True)
            (hwmon / "name").write_text(CONTROL.HWMON_DEVICE_NAME + "\n", encoding="ascii")
            self.assertEqual(
                CONTROL.classify_fan_binding(thermal, root / "hwmon"),
                "unsupported",
            )

    def test_missing_nvidia_smi_stays_inside_target_discovery(self):
        guard = CONTROL.AcpiThermalGuard({})
        before = self.run.real_invocations
        with self.assertLogs(CONTROL.LOG, level="ERROR") as logs:
            self.assertEqual(guard.discover_targets(), {})
        text = " ".join(logs.output)
        self.assertIn("nvidia-smi unavailable", text)
        self.assertNotIn("observation failed", text)
        self.assertFalse(guard.discovery_complete)
        self.assertEqual(self.run.real_invocations, before)

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
