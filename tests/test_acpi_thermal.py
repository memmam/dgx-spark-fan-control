# SPDX-License-Identifier: GPL-2.0-only
"""ACPI escalation tests. All process, hook and shutdown effects are mocked."""
import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_fan_control_contract import CONTROL as C


class ThermalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        state = patch.object(C, "STALL_MARKER_DIR", self.root / "state")
        state.start()
        self.addCleanup(state.stop)
        boot = patch.object(C, "read_boot_id", return_value="test-boot")
        boot.start()
        self.addCleanup(boot.stop)
        self.targets = Mock(return_value={200: (12, "gpu_burn")})
        self.send = Mock()
        self.shutdown = Mock(return_value=0)
        self.hook = Mock(return_value=None)
        self.reboot = Mock(return_value=0)
        self.maximum = Mock()
        self.guard = self.make_guard()

    def make_guard(self, defaults=None):
        return C.AcpiThermalGuard(defaults or {}, targets=self.targets, send=self.send,
                                 hook=self.hook, shutdown=self.shutdown, reboot=self.reboot)

    def step(self, hot, now):
        self.guard.step(hot, now, self.maximum)

    def test_defaults_and_80_max_only(self):
        self.assertEqual((self.guard.fan_c, self.guard.stop_c, self.guard.kill_c,
                          self.guard.shutdown_c), (80, 85, 90, 95))
        self.step(79.9, 0)
        self.maximum.assert_not_called()
        self.step(80, 1)
        self.maximum.assert_called_once()
        self.assertTrue(C.acpi_record_current(C.ACPI_MAX_NAME))
        self.send.assert_not_called()
        self.shutdown.assert_not_called()

    def test_missing_hook_term_then_timeout_kill_then_shutdown(self):
        self.step(85, 0)
        self.send.assert_called_once_with({200: (12, "gpu_burn")}, signal.SIGTERM)
        self.step(86, 9.9)
        self.assertEqual(self.send.call_count, 1)
        self.step(86, 10)
        self.assertEqual(self.send.call_args.args[1], signal.SIGKILL)
        self.step(86, 14.9)
        self.shutdown.assert_not_called()
        self.step(86, 15)
        self.shutdown.assert_called_once()

    def test_90_preempts_hanging_hook_and_term_grace(self):
        process = Mock()
        process.poll.return_value = None
        self.hook.return_value = process
        self.step(85, 0)
        self.send.assert_not_called()
        self.step(90, 0.1)
        process.kill.assert_not_called()
        process.wait.assert_not_called()
        self.assertEqual([c.args[1] for c in self.send.call_args_list],
                         [signal.SIGTERM, signal.SIGKILL])
        self.shutdown.assert_not_called()
        self.step(95, 0.2)
        self.shutdown.assert_called_once()

    def test_direct_95_kills_then_reboots_when_the_stop_is_verified(self):
        self.step(95, 0)
        self.hook.assert_not_called()
        self.targets.assert_called_once()
        self.assertEqual([c.args[1] for c in self.send.call_args_list],
                         [signal.SIGTERM, signal.SIGKILL])
        self.shutdown.assert_not_called()
        self.reboot.assert_not_called()
        self.targets.return_value = {}
        with patch.object(self.guard, "identity", return_value=None):
            self.step(95, 1)
        self.reboot.assert_called_once()
        self.shutdown.assert_not_called()
        self.assertEqual(self.guard.stage, "reboot")
        self.step(96, 2)
        self.shutdown.assert_not_called()
        self.reboot.assert_called_once()

    def test_hook_success_gets_grace_and_cooling_prevents_signals(self):
        process = Mock()
        process.poll.return_value = 0
        self.hook.return_value = process
        self.step(85, 0)
        self.step(82, 10)
        self.send.assert_not_called()
        self.assertEqual(self.guard.stage, "cooling")
        self.step(85, 11)
        self.send.assert_called_once()

    def test_hook_failure_falls_back_immediately(self):
        process = Mock()
        process.poll.return_value = 1
        self.hook.return_value = process
        self.step(85, 0)
        self.assertEqual(self.send.call_args.args[1], signal.SIGTERM)

    def test_hook_start_failure_falls_back(self):
        self.hook.side_effect = OSError("no executable")
        self.step(85, 0)
        self.assertEqual(self.guard.stage, "term")

    def test_confirmed_stop_reboots_immediately_without_a_cool_wait(self):
        self.step(85, 0)
        self.step(95, 1)
        self.reboot.assert_not_called()
        self.shutdown.assert_not_called()
        self.targets.return_value = {}
        with patch.object(self.guard, "identity", return_value=None):
            self.step(95, 2)
        self.reboot.assert_called_once()
        self.assertEqual(self.guard.stage, "reboot")
        self.assertTrue(C.acpi_record_current(C.ACPI_MAX_NAME))
        self.assertTrue(C.acpi_incident_latched())
        self.shutdown.assert_not_called()
        self.step(95, 3)
        self.reboot.assert_called_once()
        self.shutdown.assert_not_called()

    def test_cooling_does_not_reboot_through_alive_jobs_or_missing_query(self):
        self.step(85, 0)
        self.step(77, 1)
        self.reboot.assert_not_called()
        self.targets.return_value = {}
        self.guard.discovery_complete = False
        with patch.object(self.guard, "identity", return_value=None):
            self.step(77, 2)
        self.reboot.assert_not_called()
        self.guard.discovery_complete = True
        self.guard.blocked_targets = {1}
        with patch.object(self.guard, "identity", return_value=None):
            self.step(77, 3)
        self.reboot.assert_not_called()

    def test_persistent_latch_blocks_startup_and_reset_requires_reboot_and_cooling(self):
        thermal, hwmon = self.fake_fans(77000)
        self.step(85, 0)
        self.assertEqual(C.run_daemon(thermal, 2, hwmon), C.HELD_FLOOR_EXIT)
        self.assertEqual(C.apply_manual_floor(thermal, hwmon), C.HELD_FLOOR_EXIT)
        with patch.object(C.os, "geteuid", return_value=0), \
             self.assertRaisesRegex(RuntimeError, "until after reboot"):
            C.reset_acpi_incident(thermal)
        with patch.object(C, "read_boot_id", return_value="next-boot"), \
             patch.object(C.os, "geteuid", return_value=0), \
             patch.object(C, "read_defaults", return_value={}):
            self.assertTrue(C.acpi_incident_latched())
            with self.assertRaisesRegex(RuntimeError, "incident latched"):
                C.arm_acpi_monitor(thermal, hwmon)
            self.assertEqual(C.run_daemon(thermal, 2, hwmon), C.HELD_FLOOR_EXIT)
            C.reset_acpi_incident(thermal)
            self.assertFalse(C.acpi_incident_latched())
        for fan in C.acpi_fan_devices(thermal):
            self.assertEqual((fan / "cur_state").read_text(), "35")

    def test_explicit_shutdown_disable_and_retry_failure(self):
        self.guard = self.make_guard({"ACPI_THERMAL_SHUTDOWN": "no"})
        self.step(95, 0)
        self.shutdown.assert_not_called()
        self.assertEqual(self.guard.stage, "kill")
        self.step(95, 1)
        self.shutdown.assert_not_called()
        self.assertEqual(self.guard.stage, "disabled")
        self.guard = self.make_guard()
        self.shutdown.return_value = 1
        self.step(95, 2)
        self.shutdown.assert_not_called()
        self.step(95, 3)
        self.step(95, 4)
        self.assertEqual(self.shutdown.call_count, 2)

    def test_missing_temperature_fails_conservatively_when_stop_is_unverified(self):
        self.step(None, 0)
        self.shutdown.assert_not_called()
        self.reboot.assert_not_called()
        self.step(None, 1)
        self.shutdown.assert_called_once()

    def test_invalid_config_rejected(self):
        for config in ({"ACPI_THERMAL_STOP_C": "79"},
                       {"ACPI_THERMAL_KILL_C": "85"},
                       {"ACPI_THERMAL_TERM_SECONDS": "0"},
                       {"ACPI_THERMAL_KILL_SECONDS": "nan"},
                       {"ACPI_THERMAL_SHUTDOWN": "maybe"},
                       {"ACPI_THERMAL_HYSTERESIS_C": "8"}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                self.make_guard(config)

    def test_custom_thresholds_and_timeouts(self):
        self.guard = self.make_guard({"ACPI_THERMAL_MAX_C": "79",
                                     "ACPI_THERMAL_STOP_C": "84",
                                     "ACPI_THERMAL_KILL_C": "89",
                                     "ACPI_THERMAL_SHUTDOWN_C": "94",
                                     "ACPI_THERMAL_TERM_SECONDS": "3",
                                     "ACPI_THERMAL_KILL_SECONDS": "2"})
        self.step(84, 0)
        self.step(84, 3)
        self.assertEqual(self.send.call_args.args[1], signal.SIGKILL)
        self.step(84, 5)
        self.shutdown.assert_called_once()

    def fake_fans(self, temp=80000):
        thermal = self.root / "thermal"
        hwmon = self.root / "hwmon"
        for i in range(2):
            fan = thermal / f"cooling_device{i}"
            fan.mkdir(parents=True)
            (fan / "type").write_text("Fan")
            (fan / "cur_state").write_text("35")
            (fan / "max_state").write_text("100")
            sensor = hwmon / f"hwmon{i}"
            sensor.mkdir(parents=True)
            (sensor / "name").write_text("acpi_fan")
            (sensor / "fan1_input").write_text("2000")
            zone = thermal / f"thermal_zone{i}"
            zone.mkdir()
            (zone / "temp").write_text(str(temp - i * 10000))
        return thermal, hwmon

    def test_poll_hottest_zone_sets_both_and_persistent_latch_blocks_lower(self):
        thermal, hwmon = self.fake_fans()
        C.record_acpi_boot(C.ACPI_HOLD_NAME)
        C.poll_acpi_thermal(self.guard, thermal, hwmon, 0)
        for fan in C.acpi_fan_devices(thermal):
            self.assertEqual((fan / "cur_state").read_text().strip(), "100")
            with self.assertRaises(RuntimeError):
                C.write_acpi_fan_level(fan, 35)
            for value in (0, 34):
                with self.assertRaises(ValueError):
                    C.write_acpi_fan_level(fan, value)
        self.assertTrue(C.acpi_record_current(C.ACPI_HOLD_NAME))
        with patch.object(C, "read_boot_id", return_value="next-boot"):
            self.assertFalse(C.acpi_record_current(C.ACPI_HOLD_NAME))
            self.assertFalse(C.acpi_record_current(C.ACPI_MAX_NAME))

    def test_unarmed_telemetry_and_true_zero_do_not_run_escalation(self):
        thermal, hwmon = self.fake_fans(95000)
        C.poll_acpi_thermal(self.guard, thermal, hwmon, 0)
        self.shutdown.assert_not_called()
        C.record_acpi_boot(C.ACPI_HOLD_NAME)
        with patch.object(C, "classify_fan_binding", return_value="unsupported"):
            C.poll_acpi_thermal(self.guard, thermal, hwmon, 0)
        self.shutdown.assert_not_called()
        for sensor in hwmon.glob("hwmon*"):
            (sensor / "fan1_input").write_text("0")
        C.poll_acpi_thermal(self.guard, thermal, hwmon, 0)
        self.send.assert_not_called()
        self.shutdown.assert_not_called()

    def test_arm_refuses_inactive_watchdog_and_3137_before_write(self):
        thermal, hwmon = self.fake_fans()
        with patch.object(C, "acpi_ec_version", return_value="3.5.8"), \
             patch.object(C, "systemctl_query", return_value="inactive"), \
             self.assertRaises(RuntimeError):
            C.arm_acpi_monitor(thermal, hwmon)
        self.assertFalse(C.acpi_record_current(C.ACPI_HOLD_NAME))
        with patch.object(C, "read_kernel_log", return_value="pinned EC 3.13.7 capabilities"), \
             self.assertRaises(C.ControlUnsupported):
            C.arm_acpi_monitor(thermal, hwmon)
        with patch.object(C, "acpi_ec_version", return_value="3.5.8"), \
             patch.object(C, "systemctl_query", return_value="active"), \
             patch.object(C, "read_defaults", return_value={}):
            C.arm_acpi_monitor(thermal, hwmon)
        self.assertTrue(C.acpi_record_current(C.ACPI_HOLD_NAME))

    def test_restart_same_boot_reboots_as_soon_as_the_stop_is_verified(self):
        self.step(85, 0)
        self.guard = self.make_guard()
        self.assertEqual(self.guard.stage, "incident")
        self.targets.return_value = {}
        self.step(90, 1)
        self.reboot.assert_called_once()
        self.shutdown.assert_not_called()
        self.step(95, 2)
        self.reboot.assert_called_once()
        self.shutdown.assert_not_called()

    def test_already_stopped_workload_reboots_instead_of_shutting_down(self):
        self.targets.return_value = {}
        self.step(85, 0)
        self.reboot.assert_called_once()
        self.shutdown.assert_not_called()
        self.step(95, 1)
        self.reboot.assert_called_once()
        self.shutdown.assert_not_called()

    def test_legacy_foreign_floor_reboot_cannot_bypass_incident_cooling(self):
        thermal, hwmon = self.fake_fans(80000)
        C.record_acpi_boot(C.ACPI_HOLD_NAME)
        for incident in (True, False):
            if incident:
                C.record_acpi_boot(C.ACPI_INCIDENT_NAME)
            else:
                (C.STALL_MARKER_DIR / C.ACPI_INCIDENT_NAME).unlink()
            event = Mock()
            event.is_set.side_effect = [False, True]
            event.wait.return_value = False
            runner = Mock(return_value=0)
            clock = iter([0, 10, 10])
            with patch.object(C, "Event", return_value=event), \
                 patch.object(C.signal, "signal"), \
                 patch.object(C, "observe_hot_job"), \
                 patch.object(C, "run_watchdog_action", return_value=0):
                self.assertEqual(C.run_watchdog(
                    thermal, hwmon, defaults={}, runner=runner,
                    unit_state=lambda: ("inactive", "enabled", C.FOREIGN_FLOOR_EXIT, "exit-code", 0),
                    notifier=lambda *_: None, monotonic=lambda: next(clock),
                    marker_path=C.STALL_MARKER_DIR / "legacy-stall", boot_id="test-boot",
                    gpu_jobs=lambda: False, reboot_seconds=0), 0)
            if incident:
                runner.assert_not_called()
            else:
                runner.assert_called_once_with(C.WATCHDOG_REBOOT_ACTION)

    def test_true_zero_hot_path_survives_invalid_new_config_without_gpu_probe(self):
        thermal, hwmon = self.fake_fans(85000)
        for sensor in hwmon.glob("hwmon*"):
            (sensor / "fan1_input").write_text("0")
        C.record_acpi_boot(C.ACPI_HOLD_NAME)
        power = Mock(return_value=0)
        with patch.object(C.signal, "signal"), \
             patch.object(C, "run_watchdog_action", side_effect=AssertionError("new policy must not run")), \
             patch.object(C, "gpu_job_running", side_effect=AssertionError("must not probe GPU")):
            status = C.run_watchdog(thermal, hwmon, defaults={"ACPI_THERMAL_STOP_C": "nan"},
                                    runner=power, unit_state=lambda: ("inactive", "disabled", 0, "success", 0),
                                    notifier=lambda *_: None, monotonic=lambda: 0,
                                    marker_path=C.STALL_MARKER_DIR / "true-zero", boot_id="test-boot")
        self.assertEqual(status, 0)
        power.assert_called_once_with(C.WATCHDOG_DEFAULT_ACTION)
        self.assertFalse(C.acpi_incident_latched())

    def test_tool_and_stall_repair_arm_before_writing_and_guard_survives_exit(self):
        thermal, hwmon = self.fake_fans(85000)
        with patch.object(C, "acpi_ec_version", return_value="3.5.8"), \
             patch.object(C, "systemctl_query", return_value="active"), \
             patch.object(C, "read_defaults", return_value={}):
            C.set_acpi_fans(thermal, 35, module_loaded=False,
                            service_active="inactive", report=False)
            self.assertTrue(C.acpi_record_current(C.ACPI_HOLD_NAME))
            C.poll_acpi_thermal(self.guard, thermal, hwmon, 0)
            self.assertEqual(self.guard.stage, "term")
            with self.assertRaisesRegex(RuntimeError, "incident latched"):
                C.set_acpi_fans(thermal, 35, module_loaded=False,
                                service_active="inactive", report=False)
        # New simulated boot, cleared incident by explicit reset for the fixture.
        (C.STALL_MARKER_DIR / C.ACPI_INCIDENT_NAME).unlink()
        (C.STALL_MARKER_DIR / C.ACPI_HOLD_NAME).unlink()
        with patch.object(C, "acpi_ec_version", return_value="3.5.8"), \
             patch.object(C, "systemctl_query", return_value="active"), \
             patch.object(C, "read_defaults", return_value={}):
            C.recover_fan_stall(thermal, module_loaded=False)
        self.assertTrue(C.acpi_record_current(C.ACPI_HOLD_NAME))

    def test_sweep_arms_same_shared_monitor(self):
        from test_acpi_floor_sweep import sweep_mod, FakeSpark
        ctl = sweep_mod.controller()
        spark = FakeSpark(self.root / "sweep")
        with patch.object(ctl, "STALL_MARKER_DIR", C.STALL_MARKER_DIR), \
             patch.object(ctl, "read_boot_id", return_value="test-boot"), \
             patch.object(ctl, "acpi_ec_version", return_value="3.5.8"), \
             patch.object(ctl, "systemctl_query", return_value="active"), \
             patch.object(ctl, "read_defaults", return_value={}):
            self.assertEqual(sweep_mod.sweep([35], 1, 1, 1000, 60,
                             spark.thermal, spark.hwmon, lambda _: None,
                             sleep=lambda _: None), 0)
            self.assertTrue(C.acpi_record_current(C.ACPI_HOLD_NAME))
            C.record_acpi_boot(C.ACPI_MAX_NAME)
            with self.assertRaisesRegex(RuntimeError, "latched until reboot"):
                sweep_mod.write_level([spark.cooler], 35)
        self.assertEqual(spark.level(), 100)

    def test_partial_maximum_failure_still_attempts_other_cooler(self):
        thermal, hwmon = self.fake_fans(80000)
        C.record_acpi_boot(C.ACPI_HOLD_NAME)
        real_write = C.write_acpi_fan_level
        def write(device, level):
            if device.name == "cooling_device0":
                raise OSError("unwritable")
            return real_write(device, level)
        with patch.object(C, "write_acpi_fan_level", side_effect=write):
            C.poll_acpi_thermal(self.guard, thermal, hwmon, 0)
        self.assertEqual((thermal / "cooling_device1/cur_state").read_text().strip(), "100")

    def test_esrt_detects_versions_without_dmesg_and_unknown_refuses(self):
        entry = self.root / "esrt" / "entry0"
        entry.mkdir(parents=True)
        (entry / "fw_class").write_text(C.EC_FWUPD_GUID)
        with patch.object(C, "read_kernel_log", return_value=""):
            (entry / "fw_version").write_text("0x03000508")
            self.assertEqual(C.acpi_ec_version(entry.parent), "3.5.8")
            (entry / "fw_version").write_text(str(0x03000d07))
            self.assertEqual(C.acpi_ec_version(entry.parent), "3.13.7")
        thermal, hwmon = self.fake_fans()
        with patch.object(C, "acpi_ec_version", return_value=None), \
             self.assertRaisesRegex(RuntimeError, "positively identify"):
            C.arm_acpi_monitor(thermal, hwmon)

    def test_process_query_matches_existing_gpu_job_query_and_filters(self):
        guard = C.AcpiThermalGuard({"ACPI_THERMAL_PIDS": "1 200 201"})
        result = subprocess.CompletedProcess([], 0, "200\n202\nN/A\n", "")
        with patch.object(C.subprocess, "run", return_value=result) as run, \
             patch.object(guard, "identity", side_effect=lambda p: (12, "job") if p == 200 else None):
            self.assertEqual(guard.discover_targets(), {200: (12, "job")})
            self.assertEqual(run.call_args.args[0],
                             ["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader"])
        with patch.object(C.subprocess, "run", side_effect=FileNotFoundError("nvidia-smi")), \
             patch.object(guard, "identity", return_value=None), \
             self.assertLogs(C.LOG, "ERROR") as logs:
            self.assertEqual(guard.discover_targets(), {})
        self.assertIn("nvidia-smi unavailable", " ".join(logs.output))

    def test_pidfd_rechecks_identity_and_never_uses_os_kill(self):
        guard = C.AcpiThermalGuard({})
        with patch.object(C.os, "pidfd_open", return_value=8) as opened, \
             patch.object(C.os, "close") as close, \
             patch.object(C.signal, "pidfd_send_signal") as send, \
             patch.object(C.os, "kill") as kill, \
             patch.object(guard, "identity", return_value=(12, "job")):
            guard.signal_targets({200: (12, "job")}, signal.SIGTERM)
            send.assert_called_once_with(8, signal.SIGTERM)
            close.assert_called_once_with(8)
            opened.assert_called_once_with(200)
            kill.assert_not_called()
        with patch.object(C.os, "pidfd_open", return_value=8), \
             patch.object(C.os, "close"), \
             patch.object(C.signal, "pidfd_send_signal") as send, \
             patch.object(guard, "identity", return_value=(13, "job")):
            guard.signal_targets({200: (12, "job")}, signal.SIGKILL)
            send.assert_not_called()

    def test_identity_protects_root_system_self_pid1_and_reused_pid(self):
        proc = self.root / "proc"
        pid = 999999
        base = proc / str(pid)
        base.mkdir(parents=True)
        def process(uid=1000, name="job", cgroup="0::/user.slice/test", cmd=b"job", start=12):
            (base / "status").write_text(f"Uid:\t{uid} {uid} {uid} {uid}\n")
            fields = ["S", "1"] + ["0"] * 17 + [str(start)]
            (base / "stat").write_text(f"{pid} ({name}) " + " ".join(fields))
            (base / "cmdline").write_bytes(cmd)
            (base / "cgroup").write_text(cgroup)
        process()
        self.assertEqual(C.AcpiThermalGuard.identity(pid, proc), (12, "job"))
        for kwargs in ({"uid": 0}, {"name": "sshd"},
                       {"cgroup": "0::/system.slice/critical.service"}, {"cmd": b""}):
            process(**kwargs)
            self.assertIsNone(C.AcpiThermalGuard.identity(pid, proc))
        for protected in (0, 1, os.getpid(), os.getppid()):
            self.assertIsNone(C.AcpiThermalGuard.identity(protected, proc))

    def test_true_zero_with_unreadable_zone_does_not_use_acpi_shutdown(self):
        thermal, hwmon = self.fake_fans()
        C.record_acpi_boot(C.ACPI_HOLD_NAME)
        for sensor in hwmon.glob("hwmon*"):
            (sensor / "fan1_input").write_text("0")
        for zone in thermal.glob("thermal_zone*"):
            (zone / "temp").unlink()
        C.poll_acpi_thermal(self.guard, thermal, hwmon, 0)
        self.shutdown.assert_not_called()
        self.send.assert_not_called()
        for fan in C.acpi_fan_devices(thermal):
            self.assertEqual((fan / "cur_state").read_text().strip(), "35")

    def test_unreadable_or_hot_zone_does_not_replace_an_accepted_recovery_reboot(self):
        self.step(85, 0)
        self.targets.return_value = {}
        with patch.object(self.guard, "identity", return_value=None):
            self.step(92, 1)
        self.reboot.assert_called_once()
        self.shutdown.assert_not_called()
        self.assertEqual(self.guard.stage, "reboot")
        self.step(None, 2)
        self.step(95, 3)
        self.shutdown.assert_not_called()
        self.reboot.assert_called_once()
        self.assertEqual(self.guard.stage, "reboot")

    def test_incident_persist_is_retried_until_present(self):
        real = C.record_acpi_boot
        failed = False

        def flaky(name):
            nonlocal failed
            if name == C.ACPI_INCIDENT_NAME and not failed:
                failed = True
                raise OSError("disk")
            real(name)

        with patch.object(C, "record_acpi_boot", side_effect=flaky):
            self.step(85, 0)
            self.assertFalse(C.acpi_incident_latched())
            self.step(86, 1)
        self.assertTrue(C.acpi_record_current(C.ACPI_INCIDENT_NAME))

    def test_acpi_record_fsyncs_file_and_directory_once(self):
        with patch.object(C.os, "fsync") as fsync:
            C.record_acpi_boot(C.ACPI_INCIDENT_NAME)
            first = fsync.call_count
            C.record_acpi_boot(C.ACPI_INCIDENT_NAME)
            self.assertEqual(fsync.call_count, first)
        self.assertGreaterEqual(first, 2)
        path = C.STALL_MARKER_DIR / C.ACPI_INCIDENT_NAME
        path.unlink()
        with patch.object(C.os, "fsync") as fsync:
            C.record_acpi_boot(C.ACPI_INCIDENT_NAME)
        self.assertGreaterEqual(fsync.call_count, 2)
        self.assertTrue(C.acpi_record_current(C.ACPI_INCIDENT_NAME))

    def test_hook_kill_uses_spawn_time_pidfd_not_numeric_kill(self):
        process = Mock()
        process.poll.return_value = None
        process.hook_pidfd = 11
        process.pid = 4242
        self.guard.hook_process = process
        with patch.object(C.signal, "pidfd_send_signal") as send, \
             patch.object(C.os, "close") as close, \
             patch.object(C.os, "kill") as kill:
            self.guard.finish_hook()
        send.assert_called_once_with(11, signal.SIGKILL)
        close.assert_called_once_with(11)
        kill.assert_not_called()
        process.kill.assert_not_called()
        self.assertIsNone(self.guard.hook_process)

    def test_hook_is_async_argv_without_shell(self):
        guard = C.AcpiThermalGuard({"ACPI_THERMAL_STOP_HOOK": "/opt/stop-job --reason hot"})
        with patch.object(C.subprocess, "Popen") as popen:
            guard.start_hook()
            self.assertEqual(popen.call_args.args[0], ["/opt/stop-job", "--reason", "hot"])
            self.assertNotIn("shell", popen.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
