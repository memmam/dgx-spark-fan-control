# SPDX-License-Identifier: GPL-2.0-only
"""ACPI-only module toggle, using a temporary etc tree and a fake runner."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from test_fan_control_contract import CONTROL


class ModuleModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.etc = Path(self.temp.name)
        load = self.etc / "modules-load.d" / CONTROL.MODULES_LOAD_NAME
        load.parent.mkdir()
        load.write_text(CONTROL.MODULES_LOAD_TEXT, encoding="utf-8")
        foreign = self.etc / "modprobe.d" / "local-blacklist.conf"
        foreign.parent.mkdir()
        foreign.write_text("blacklist dgx_ec_fan_control\n", encoding="utf-8")
        (self.etc / "modprobe.d" / "local-install.conf").write_text(
            "install dgx_ec_fan_control /bin/true\n", encoding="utf-8",
        )
        (self.etc / "modprobe.d" / "local-options.conf").write_text(
            "options dgx_ec_fan_control allow_unverified_writes=1\n", encoding="utf-8",
        )
        self.loaded = True
        self.commands = []

    def runner(self, argv):
        self.commands.append(list(argv))
        if argv[:2] == ["modprobe", "-r"]:
            self.loaded = False
        elif argv[:1] == ["modprobe"] and "-r" not in argv:
            self.loaded = True

    def test_disable_is_idempotent_and_leaves_foreign_blacklists(self):
        with self.assertLogs(CONTROL.LOG, level="WARNING"):
            CONTROL.disable_kernel_module(self.etc, runner=self.runner, loaded=True)
            first = (self.etc / "modprobe.d" / CONTROL.MANAGED_BLACKLIST_NAME).read_text(
                encoding="utf-8"
            )
            CONTROL.disable_kernel_module(
                self.etc, runner=self.runner, loaded=self.loaded,
            )
        managed = self.etc / "modprobe.d" / CONTROL.MANAGED_BLACKLIST_NAME
        text = managed.read_text(encoding="utf-8")
        self.assertEqual(text, first)
        self.assertIn("blacklist dgx_ec_fan_control\n", text)
        self.assertIn("install dgx_ec_fan_control /bin/false\n", text)
        load = (self.etc / "modules-load.d" / CONTROL.MODULES_LOAD_NAME).read_text(
            encoding="utf-8"
        )
        self.assertEqual(load, CONTROL.MODULES_LOAD_DISABLED_TEXT)
        self.assertNotIn("\ndgx_ec_fan_control\n", "\n" + load)
        foreign = (self.etc / "modprobe.d" / "local-blacklist.conf").read_text(encoding="utf-8")
        self.assertEqual(foreign, "blacklist dgx_ec_fan_control\n")
        install = (self.etc / "modprobe.d" / "local-install.conf").read_text(encoding="utf-8")
        self.assertEqual(install, "install dgx_ec_fan_control /bin/true\n")
        unloads = [cmd for cmd in self.commands if cmd[:2] == ["modprobe", "-r"]]
        self.assertEqual(len(unloads), 1)
        self.assertIn(["systemctl", "disable", "--now", CONTROL.DAEMON_UNIT], self.commands)

    def test_enable_restores_autoload_and_is_idempotent(self):
        with self.assertLogs(CONTROL.LOG, level="WARNING"):
            CONTROL.disable_kernel_module(self.etc, runner=self.runner, loaded=True)
        self.commands.clear()
        with self.assertLogs(CONTROL.LOG, level="WARNING") as logs:
            CONTROL.enable_kernel_module(
                self.etc, runner=self.runner, loaded=self.loaded, control_supported=True,
            )
        self.assertTrue(any("still block loading" in line for line in logs.output))
        CONTROL.enable_kernel_module(
            self.etc, runner=self.runner, loaded=True, control_supported=True,
        )
        managed = self.etc / "modprobe.d" / CONTROL.MANAGED_BLACKLIST_NAME
        self.assertFalse(managed.exists())
        load = (self.etc / "modules-load.d" / CONTROL.MODULES_LOAD_NAME).read_text(
            encoding="utf-8"
        )
        self.assertEqual(load, CONTROL.MODULES_LOAD_TEXT)
        self.assertEqual(
            (self.etc / "modprobe.d" / "local-blacklist.conf").read_text(encoding="utf-8"),
            "blacklist dgx_ec_fan_control\n",
        )
        self.assertEqual(
            [cmd for cmd in self.commands if cmd[:1] == ["modprobe"] and "-r" not in cmd],
            [["modprobe", CONTROL.MODULE_NAME]],
        )

    def test_status_reports_acpi_only_and_initramfs_without_editing_it(self):
        initramfs = self.etc / "initramfs-tools" / "modules"
        initramfs.parent.mkdir()
        initramfs.write_text("dgx_ec_fan_control\n", encoding="utf-8")
        with self.assertLogs(CONTROL.LOG, level="WARNING") as logs:
            CONTROL.disable_kernel_module(self.etc, runner=self.runner, loaded=True)
        self.assertEqual(initramfs.read_text(encoding="utf-8"), "dgx_ec_fan_control\n")
        self.assertTrue(any("update-initramfs" in line for line in logs.output))
        report = CONTROL.module_status(
            self.etc, loaded=False, service_active="inactive", service_enabled="disabled",
        )
        self.assertIn("loaded: no\n", report)
        self.assertIn("managed-blacklist: yes\n", report)
        self.assertIn("local-blacklist.conf", report)
        self.assertIn("local-install.conf", report)
        self.assertNotIn("local-options.conf", report)
        self.assertIn("service-enabled: disabled\n", report)
        self.assertIn("service-active: inactive\n", report)
        self.assertIn("mode: ACPI-only\n", report)
        self.assertIn("initramfs: ", report)
        self.assertIn("service-exit: n/a\n", report)

    def test_foreign_modules_load_file_is_not_rewritten(self):
        load = self.etc / "modules-load.d" / CONTROL.MODULES_LOAD_NAME
        load.write_text("dgx_ec_fan_control\ncustom_module\n", encoding="utf-8")
        with self.assertLogs(CONTROL.LOG, level="WARNING"):
            CONTROL.disable_kernel_module(self.etc, runner=self.runner, loaded=False)
        self.assertEqual(load.read_text(encoding="utf-8"), "dgx_ec_fan_control\ncustom_module\n")

    def test_enable_leaves_the_service_disabled_when_control_is_unsupported(self):
        self.loaded = False
        import io
        import logging
        from contextlib import redirect_stderr, redirect_stdout
        stdout, stderr = io.StringIO(), io.StringIO()
        sentence = (
            "fan control unsupported on EC 3.13.7; telemetry only, "
            "service left disabled; use acpi-max on EC 3.5.8"
        )
        root = logging.getLogger()
        handler = logging.StreamHandler(stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        root.addHandler(handler)
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr), \
                 self.assertLogs(CONTROL.LOG, level="WARNING") as logs:
                CONTROL.enable_kernel_module(
                    self.etc, runner=self.runner, loaded=False,
                    control_supported=False, profile="EC 3.13.7",
                )
        finally:
            root.removeHandler(handler)
        combined = stdout.getvalue() + stderr.getvalue() + "\n".join(logs.output)
        self.assertEqual(combined.count(sentence), 1)
        self.assertEqual(stdout.getvalue().count(sentence), 1)
        self.assertNotIn(sentence, stderr.getvalue())
        self.assertIn(["modprobe", CONTROL.MODULE_NAME], self.commands)
        self.assertNotIn(
            ["systemctl", "enable", "--now", CONTROL.DAEMON_UNIT], self.commands,
        )
        self.assertIn(
            ["systemctl", "disable", "--now", CONTROL.DAEMON_UNIT], self.commands,
        )
        self.commands.clear()
        CONTROL.enable_kernel_module(
            self.etc, runner=self.runner, loaded=True,
            control_supported=False, profile="EC 3.13.7", force_service=True,
        )
        self.assertIn(
            ["systemctl", "enable", "--now", CONTROL.DAEMON_UNIT], self.commands,
        )

    def test_status_names_unsupported_control(self):
        report = CONTROL.module_status(
            self.etc, loaded=True, service_active="inactive", service_enabled="disabled",
            control=CONTROL.describe_control("unsupported", "EC 3.13.7"),
            exec_status=0,
            exit_timestamp="Fri 2026-11-06 00:12:01 UTC",
            start_monotonic_us=1_000,
            exit_monotonic_us=1_100,
        )
        self.assertIn("control: unsupported (EC 3.13.7, telemetry only)\n", report)
        self.assertIn("service-exit: n/a\n", report)
        self.assertNotIn("service-exit: 0", report)
        self.assertEqual(CONTROL.describe_control("supported", "EC 3.5.8"), "supported")
        self.assertEqual(
            CONTROL.pinned_ec_name(
                "pinned EC 3.13.7 capabilities fan0=975..4350 fan1=975..4350\n"
            ),
            "EC 3.13.7",
        )

    def test_status_shows_exit_only_after_the_current_run(self):
        when = "Fri 2026-11-06 01:02:03 UTC"
        current = CONTROL.module_status(
            self.etc, loaded=True, service_active="inactive", service_enabled="enabled",
            exec_status=69, exit_timestamp=when,
            start_monotonic_us=5_000, exit_monotonic_us=5_067,
        )
        self.assertIn(f"service-exit: 69 at {when}\n", current)
        failed = CONTROL.module_status(
            self.etc, loaded=True, service_active="failed", service_enabled="disabled",
            exec_status=1, exit_timestamp=when,
        )
        self.assertIn(f"service-exit: 1 at {when}\n", failed)
        running = CONTROL.module_status(
            self.etc, loaded=True, service_active="active", service_enabled="enabled",
            exec_status=0, exit_timestamp=when,
            start_monotonic_us=9_000, exit_monotonic_us=8_000,
        )
        self.assertIn("service-exit: n/a\n", running)
        stale_start = CONTROL.module_status(
            self.etc, loaded=True, service_active="inactive", service_enabled="enabled",
            exec_status=0, exit_timestamp=when,
            start_monotonic_us=9_000, exit_monotonic_us=8_000,
        )
        self.assertIn("service-exit: n/a\n", stale_start)
        never_ran = CONTROL.module_status(
            self.etc, loaded=True, service_active="inactive", service_enabled="enabled",
            exec_status=0, exit_monotonic_us=0,
        )
        self.assertIn("service-exit: n/a\n", never_ran)

    def test_watchdog_ignores_disabled_inactive_and_alarms_on_failed(self):
        self.assertFalse(CONTROL.daemon_needs_alarm("inactive", "disabled"))
        self.assertTrue(CONTROL.daemon_needs_alarm("inactive", "enabled"))
        self.assertTrue(CONTROL.daemon_needs_alarm("failed", "disabled"))
        self.assertFalse(CONTROL.daemon_needs_alarm("active", "enabled"))

    def test_watchdog_does_not_alarm_on_unsupported_firmware_exit(self):
        from threading import Event
        from unittest.mock import patch
        self.assertFalse(CONTROL.daemon_needs_alarm("inactive", "enabled", 69))
        self.assertTrue(CONTROL.daemon_needs_alarm("inactive", "enabled", 1))
        self.assertTrue(CONTROL.daemon_needs_alarm("failed", "disabled", 69))
        alerts = []
        actions = []
        stop = Event()
        with patch.object(CONTROL, "Event", return_value=stop), \
             patch.object(CONTROL.signal, "signal"), \
             patch.object(stop, "wait", side_effect=lambda delay: stop.set()), \
             self.assertLogs(CONTROL.LOG, level="WARNING") as logs:
            status = CONTROL.run_watchdog(
                Path("/tmp/dgx-fan-missing"),
                Path("/tmp/dgx-fan-missing"),
                unit_state=lambda: ("inactive", "enabled", 69),
                notifier=alerts.append,
                runner=lambda action: actions.append(action),
                monotonic=lambda: 0.0,
            )
        self.assertEqual(status, 0)
        self.assertEqual(alerts, [])
        self.assertEqual(actions, [])
        self.assertTrue(any("not alerting" in line for line in logs.output))
        self.assertFalse(any("is not active" in line for line in logs.output))

    def test_acpi_max_warns_when_the_module_is_loaded_but_still_writes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            thermal = root / "thermal"
            thermal.mkdir()
            fan = thermal / "cooling_device0"
            fan.mkdir()
            (fan / "type").write_text("Fan\n", encoding="ascii")
            (fan / "cur_state").write_text("40\n", encoding="ascii")
            (fan / "max_state").write_text("100\n", encoding="ascii")
            import io
            from contextlib import redirect_stdout
            stdout = io.StringIO()
            with redirect_stdout(stdout), self.assertLogs(CONTROL.LOG, level="WARNING") as logs:
                CONTROL.set_acpi_fans(thermal, 100, module_loaded=True)
            text = stdout.getvalue()
            self.assertEqual((fan / "cur_state").read_text(encoding="ascii"), "100\n")
            self.assertEqual(sum("module disable" in line for line in logs.output), 1)
            self.assertEqual(text.count("cooling_device0 (Fan): 100/100\n"), 1)
            self.assertEqual(text.count(CONTROL.ACPI_RPM_NOTE), 1)
            self.assertFalse(any("100/100" in line or CONTROL.ACPI_RPM_NOTE in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
