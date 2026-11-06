# SPDX-License-Identifier: GPL-2.0-only
"""Failure-path checks without touching the host's thermal interfaces."""

import errno
import io
import logging
import math
from contextlib import redirect_stderr, redirect_stdout
from threading import Event
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_fan_control_contract import CONTROL


def run_cli(argv):
    """Run main() and return (code, stdout, stderr), including log lines."""
    stdout, stderr = io.StringIO(), io.StringIO()
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    for handler in saved_handlers:
        root.removeHandler(handler)
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = CONTROL.main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)


class UserlandFailureTests(unittest.TestCase):
    def test_invalid_temperatures_are_rejected(self):
        for temperature in (0, -1, math.nan, math.inf, -math.inf):
            with self.subTest(temperature=temperature):
                with self.assertRaises(ValueError):
                    CONTROL.curve_state(temperature)

    def test_missing_and_duplicate_devices_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(RuntimeError):
                CONTROL.find_cooling_device(root)
            for index in range(2):
                device = root / f"cooling_device{index}"
                device.mkdir()
                (device / "type").write_text(CONTROL.COOLING_DEVICE_TYPE)
            with self.assertRaises(RuntimeError):
                CONTROL.find_cooling_device(root)

    def test_bad_sensor_values_do_not_hide_valid_hottest_sensor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, value in enumerate(("bad", "0", "200001", "61000", "72000")):
                zone = root / f"thermal_zone{index}"
                zone.mkdir()
                (zone / "temp").write_text(value)
            self.assertEqual(CONTROL.read_hottest_temperature_c(root), 72)
            for path in root.glob("thermal_zone*/temp"):
                path.write_text("bad")
            with self.assertRaises(RuntimeError):
                CONTROL.read_hottest_temperature_c(root)

    def test_write_detects_readback_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            device = Path(directory)
            with patch.object(CONTROL, "read_state", return_value=0):
                with self.assertRaisesRegex(RuntimeError, "readback mismatch"):
                    CONTROL.write_state(device, 3)

    def test_sensor_loss_requests_maximum_then_signal_restores_automatic(self):
        # Deliver a real daemon stop callback at its first sleep boundary.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            device = root / "cooling_device0"
            device.mkdir()
            for name, value in (("type", CONTROL.COOLING_DEVICE_TYPE),
                                ("cur_state", "0"), ("max_state", "12")):
                (device / name).write_text(value)
            handlers = {}
            observed = []
            stop = Event()

            def register(signum, callback):
                handlers[signum] = callback

            def interrupt_sleep(seconds):
                observed.append(CONTROL.read_state(device))
                handlers[CONTROL.signal.SIGTERM](CONTROL.signal.SIGTERM, None)

            with patch.object(CONTROL.signal, "signal", side_effect=register), \
                 patch.object(CONTROL, "Event", return_value=stop), \
                 patch.object(stop, "wait", side_effect=interrupt_sleep), \
                 self.assertLogs(CONTROL.LOG, level="ERROR"):
                self.assertEqual(CONTROL.run_daemon(root, 2), 0)
            self.assertEqual(observed, [12])
            self.assertEqual(CONTROL.read_state(device), 0)

    def test_transient_read_failure_recovers_without_exiting(self):
        stop = Event()
        states = iter((OSError(errno.EBUSY, "busy"), 0))
        waits = []

        def read(device):
            result = next(states)
            if isinstance(result, Exception):
                raise result
            return result

        def wait(delay):
            waits.append(delay)
            if len(waits) == 2:
                stop.set()

        with patch.object(CONTROL, "require_control", return_value=Path("device")), \
             patch.object(CONTROL, "Event", return_value=stop), \
             patch.object(CONTROL.signal, "signal"), \
             patch.object(CONTROL, "read_state", side_effect=read), \
             patch.object(CONTROL, "read_hottest_temperature_c", return_value=70), \
             patch.object(CONTROL, "write_state") as write, \
             patch.object(stop, "wait", side_effect=wait), \
             self.assertLogs(CONTROL.LOG, level="INFO") as logs:
            self.assertEqual(CONTROL.run_daemon(Path("unused"), 2), 0)
        self.assertEqual([call.args[1] for call in write.call_args_list], [2, 0])
        self.assertTrue(any("recovered" in line for line in logs.output))

    def test_persistent_failure_stops_and_preserves_original_error(self):
        stop = Event()
        original = OSError(errno.ETIMEDOUT, "initial timeout")
        with patch.object(CONTROL, "require_control", return_value=Path("device")), \
             patch.object(CONTROL, "Event", return_value=stop), \
             patch.object(CONTROL.signal, "signal"), \
             patch.object(CONTROL, "read_state", side_effect=original) as read, \
             patch.object(CONTROL, "write_state", side_effect=OSError(errno.EBUSY, "cleanup busy")), \
             patch.object(stop, "wait") as wait, \
             self.assertLogs(CONTROL.LOG, level="WARNING") as logs:
            with self.assertRaises(OSError) as raised:
                CONTROL.run_daemon(Path("unused"), 2)
        self.assertIs(raised.exception, original)
        self.assertEqual(read.call_count, 3)
        self.assertEqual([call.args[0] for call in wait.call_args_list], [2, 4])
        self.assertTrue(any("initial timeout" in line for line in logs.output))
        self.assertTrue(any("cleanup busy" in line for line in logs.output))

    def test_reply_guard_refusal_waits_out_recovery_cooldown(self):
        stop = Event()
        delays = []
        states = iter((OSError(errno.EPROTO, "reply guard"), 1))

        def read(device):
            result = next(states)
            if isinstance(result, Exception):
                raise result
            return result

        def wait(delay):
            delays.append(delay)
            if len(delays) == 2:
                stop.set()

        with patch.object(CONTROL, "require_control", return_value=Path("device")), \
             patch.object(CONTROL, "Event", return_value=stop), \
             patch.object(CONTROL.signal, "signal"), \
             patch.object(CONTROL, "read_state", side_effect=read), \
             patch.object(CONTROL, "read_hottest_temperature_c", return_value=70), \
             patch.object(CONTROL, "write_state") as write, \
             patch.object(stop, "wait", side_effect=wait), \
             self.assertLogs(CONTROL.LOG, level="INFO"):
            self.assertEqual(CONTROL.run_daemon(Path("unused"), 2), 0)
        self.assertGreaterEqual(delays[0], CONTROL.RECOVERY_COOLDOWN_SECONDS)
        self.assertEqual(delays[0], 31.0)
        self.assertNotIn(0, [call.args[1] for call in write.call_args_list[:-1]])
        self.assertEqual(write.call_args_list[-1].args[1], 0)

    def test_reply_guard_refusal_is_bounded(self):
        stop = Event()
        original = OSError(errno.EPROTO, "reply guard")
        with patch.object(CONTROL, "require_control", return_value=Path("device")), \
             patch.object(CONTROL, "Event", return_value=stop), \
             patch.object(CONTROL.signal, "signal"), \
             patch.object(CONTROL, "read_state", side_effect=original) as read, \
             patch.object(CONTROL, "write_state"), \
             patch.object(stop, "wait") as wait, \
             self.assertLogs(CONTROL.LOG, level="WARNING"):
            with self.assertRaises(OSError) as raised:
                CONTROL.run_daemon(Path("unused"), 2)
        self.assertIs(raised.exception, original)
        self.assertEqual(read.call_count, 3)
        self.assertEqual([call.args[0] for call in wait.call_args_list], [31.0, 31.0])

    def test_idle_guard_keeps_the_short_retry(self):
        stop = Event()
        original = OSError(errno.EAGAIN, "idle guard")
        with patch.object(CONTROL, "require_control", return_value=Path("device")), \
             patch.object(CONTROL, "Event", return_value=stop), \
             patch.object(CONTROL.signal, "signal"), \
             patch.object(CONTROL, "read_state", side_effect=original) as read, \
             patch.object(CONTROL, "write_state"), \
             patch.object(stop, "wait") as wait, \
             self.assertLogs(CONTROL.LOG, level="WARNING"):
            with self.assertRaises(OSError) as raised:
                CONTROL.run_daemon(Path("unused"), 2)
        self.assertIs(raised.exception, original)
        self.assertEqual(read.call_count, 3)
        self.assertEqual([call.args[0] for call in wait.call_args_list], [2.0, 4.0])

    def test_foreign_floor_is_not_retried(self):
        stop = Event()
        with patch.object(CONTROL, "require_control", return_value=Path("device")), \
             patch.object(CONTROL, "Event", return_value=stop), \
             patch.object(CONTROL.signal, "signal"), \
             patch.object(CONTROL, "read_state", side_effect=OSError(errno.ESTALE, "foreign floor")) as read, \
             patch.object(CONTROL, "write_state"), \
             patch.object(stop, "wait") as wait, \
             self.assertLogs(CONTROL.LOG, level="ERROR"):
            with self.assertRaises(OSError):
                CONTROL.run_daemon(Path("unused"), 2)
        self.assertEqual(read.call_count, 1)
        wait.assert_not_called()

    def test_state_is_resynchronized_after_kernel_restores_automatic(self):
        stop = Event()
        actual = [12]
        observations = []
        writes = []

        def write(device, state):
            writes.append(state)
            actual[0] = state

        def wait(delay):
            observations.append(actual[0])
            if len(observations) == 1:
                actual[0] = 0
            else:
                stop.set()

        with patch.object(CONTROL, "require_control", return_value=Path("device")), \
             patch.object(CONTROL, "Event", return_value=stop), \
             patch.object(CONTROL.signal, "signal"), \
             patch.object(CONTROL, "read_state", side_effect=lambda device: actual[0]), \
             patch.object(CONTROL, "read_hottest_temperature_c", return_value=70), \
             patch.object(CONTROL, "write_state", side_effect=write), \
             patch.object(stop, "wait", side_effect=wait):
            self.assertEqual(CONTROL.run_daemon(Path("unused"), 2), 0)
        self.assertEqual(observations, [12, 2])
        self.assertEqual(writes, [2, 0])

    def test_stop_interrupts_backoff_and_cleans_up(self):
        stop = Event()
        handlers = {}
        def register(sig, callback):
            handlers[sig] = callback
        with patch.object(CONTROL, "require_control", return_value=Path("device")), \
             patch.object(CONTROL, "Event", return_value=stop), \
             patch.object(CONTROL.signal, "signal", side_effect=register), \
             patch.object(CONTROL, "read_state", side_effect=OSError(errno.EBUSY, "busy")) as read, \
             patch.object(CONTROL, "write_state") as write, \
             patch.object(stop, "wait", side_effect=lambda delay: handlers[CONTROL.signal.SIGTERM](0, None)), \
             self.assertLogs(CONTROL.LOG, level="WARNING"):
            self.assertEqual(CONTROL.run_daemon(Path("unused"), 2), 0)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(write.call_args.args[1], 0)

    def test_telemetry_only_firmware_does_not_loop_or_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            thermal = root / "thermal"
            hwmon = root / "hwmon" / "hwmon0"
            thermal.mkdir()
            hwmon.mkdir(parents=True)
            (hwmon / "name").write_text(CONTROL.HWMON_DEVICE_NAME + "\n", encoding="ascii")
            argv = [
                "--thermal-root", str(thermal),
                "--hwmon-root", str(root / "hwmon"),
            ]
            code, stdout, stderr = run_cli([*argv, "status"])
            self.assertEqual(code, 0)
            combined = stdout + stderr
            self.assertEqual(combined.count(CONTROL.CONTROL_UNSUPPORTED), 1)
            self.assertEqual(stdout.count(CONTROL.CONTROL_UNSUPPORTED), 1)
            self.assertNotIn(CONTROL.CONTROL_UNSUPPORTED, stderr)
            self.assertNotIn("ERROR", stdout)

            code, stdout, stderr = run_cli([*argv, "daemon"])
            self.assertEqual(code, CONTROL.UNSUPPORTED_FIRMWARE_EXIT)
            combined = stdout + stderr
            self.assertEqual(combined.count(CONTROL.CONTROL_UNSUPPORTED), 1)
            self.assertEqual(stderr.count(CONTROL.CONTROL_UNSUPPORTED), 1)
            self.assertNotIn(CONTROL.CONTROL_UNSUPPORTED, stdout)

            for command in (["set-state", "1"], ["automatic"]):
                code, stdout, stderr = run_cli([*argv, *command])
                self.assertEqual(code, 1)
                combined = stdout + stderr
                self.assertEqual(combined.count(CONTROL.CONTROL_UNSUPPORTED), 1)
                self.assertEqual(stderr.count(CONTROL.CONTROL_UNSUPPORTED), 1)
                self.assertNotIn(CONTROL.CONTROL_UNSUPPORTED, stdout)

    def test_quiet_curve_never_selects_zero_and_is_monotonic(self):
        thresholds = [temperature for temperature, _state in CONTROL.QUIET_CURVE]
        states = [state for _temperature, state in CONTROL.QUIET_CURVE]
        self.assertEqual(thresholds, sorted(thresholds))
        self.assertEqual(states, sorted(states))
        self.assertGreaterEqual(min(states), 1)
        self.assertEqual(CONTROL.curve_state(51.9, CONTROL.QUIET_CURVE), 1)
        self.assertEqual(CONTROL.curve_state(52.0, CONTROL.QUIET_CURVE), 2)
        self.assertEqual(CONTROL.curve_state(78.0, CONTROL.QUIET_CURVE), 12)
        previous = 0
        for temperature in (0.1, 20, 51.9, 52, 57, 62, 66, 70, 73, 76, 78, 100):
            state = CONTROL.curve_state(temperature, CONTROL.QUIET_CURVE)
            self.assertGreaterEqual(state, previous)
            self.assertGreaterEqual(state, 1)
            for current in range(CONTROL.MAX_STATE + 1):
                self.assertGreaterEqual(
                    CONTROL.next_state(current, temperature, CONTROL.QUIET_CURVE), 1,
                )
            previous = state
        self.assertEqual(CONTROL.curve_name("quiet"), "quiet")
        self.assertEqual(CONTROL.curve_name("performance"), "performance")
        with patch.dict("os.environ", {"DGX_FAN_CURVE": "quiet"}, clear=False):
            self.assertEqual(CONTROL.curve_name(None), "quiet")
            self.assertIs(CONTROL.CURVES[CONTROL.curve_name(None)], CONTROL.QUIET_CURVE)

    def test_watchdog_alerts_when_the_daemon_is_not_active_before_temperature(self):
        alerts = []
        actions = []
        stop = Event()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(CONTROL, "Event", return_value=stop), \
                 patch.object(CONTROL.signal, "signal"), \
                 patch.object(stop, "wait", side_effect=lambda delay: stop.set()), \
                 self.assertLogs(CONTROL.LOG, level="WARNING") as logs:
                status = CONTROL.run_watchdog(
                    root,
                    root,
                    unit_state=lambda: "inactive",
                    notifier=alerts.append,
                    runner=lambda action: actions.append(action),
                )
        self.assertEqual(status, 0)
        self.assertEqual(actions, [])
        self.assertTrue(any("not active" in message for message in alerts))
        self.assertTrue(any("not active" in line for line in logs.output))
        self.assertFalse(CONTROL.daemon_is_down("active"))
        self.assertTrue(CONTROL.daemon_is_down("failed"))
        self.assertFalse(CONTROL.daemon_needs_alarm("inactive", "disabled"))
        self.assertTrue(CONTROL.daemon_needs_alarm("failed", "disabled"))

    def test_stall_requires_both_fans_hot_for_more_than_ten_seconds(self):
        self.assertEqual(CONTROL.WATCHDOG_SOC_CELSIUS, 85.0)
        self.assertFalse(CONTROL.fans_stalled([0, 0], 85.0, 10.0))
        self.assertTrue(CONTROL.fans_stalled([0, 0], 85.0, 10.01))
        self.assertFalse(CONTROL.fans_stalled([0, 0], 84.9, 30.0))
        self.assertFalse(CONTROL.fans_stalled([0, 400], 90.0, 30.0))
        self.assertFalse(CONTROL.fans_stalled([0], 90.0, 30.0))
        self.assertEqual(CONTROL.WATCHDOG_DEFAULT_ACTION, "systemctl poweroff")

    def test_watchdog_reads_acpi_fans_without_the_kernel_module(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            thermal = root / "thermal" / "thermal_zone0"
            thermal.mkdir(parents=True)
            (thermal / "temp").write_text("79000\n", encoding="ascii")
            for index in (0, 1):
                device = root / "hwmon" / f"hwmon{index}"
                device.mkdir(parents=True)
                (device / "name").write_text("acpi_fan\n", encoding="ascii")
                (device / "fan1_input").write_text("0\n", encoding="ascii")
            clock = [0.0]
            stop = Event()
            actions = []

            def wait(delay):
                clock[0] += delay
                if clock[0] > 30:
                    stop.set()

            def runner(action):
                actions.append(action)
                return 0

            with patch.object(CONTROL, "Event", return_value=stop), \
                 patch.object(CONTROL.signal, "signal"), \
                 patch.object(stop, "wait", side_effect=wait), \
                 self.assertLogs(CONTROL.LOG, level="CRITICAL") as logs:
                status = CONTROL.run_watchdog(
                    root / "thermal",
                    root / "hwmon",
                    poll_seconds=2,
                    zero_seconds=10,
                    soc_celsius=70,
                    monotonic=lambda: clock[0],
                    runner=runner,
                    unit_state=lambda: "active",
                    notifier=lambda message: None,
                    gpu_jobs=lambda: True,
                )
            self.assertEqual(status, 0)
            self.assertEqual(actions, [CONTROL.WATCHDOG_DEFAULT_ACTION])
            self.assertTrue(any("fan stall" in line for line in logs.output))
            self.assertFalse((root / "thermal").joinpath("cooling_device0").exists())
            self.assertEqual((root / "hwmon" / "hwmon0" / "fan1_input").read_text(), "0\n")

    def test_watchdog_does_not_trip_when_cool_or_a_fan_spins(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            thermal = root / "thermal" / "thermal_zone0"
            thermal.mkdir(parents=True)
            (thermal / "temp").write_text("42000\n", encoding="ascii")
            device = root / "hwmon" / "hwmon0"
            device.mkdir(parents=True)
            (device / "name").write_text("acpi_fan\n", encoding="ascii")
            (device / "fan1_input").write_text("0\n", encoding="ascii")
            (device / "fan2_input").write_text("2000\n", encoding="ascii")
            clock = [0.0]
            stop = Event()
            actions = []

            def wait(delay):
                clock[0] += delay
                stop.set()

            with patch.object(CONTROL, "Event", return_value=stop), \
                 patch.object(CONTROL.signal, "signal"), \
                 patch.object(stop, "wait", side_effect=wait), \
                 self.assertLogs(CONTROL.LOG, level="INFO"):
                status = CONTROL.run_watchdog(
                    root / "thermal",
                    root / "hwmon",
                    monotonic=lambda: clock[0],
                    runner=lambda action: actions.append(action),
                    unit_state=lambda: "active",
                    notifier=lambda message: None,
                )
            self.assertEqual(status, 0)
            self.assertEqual(actions, [])

    def _stopped_acpi_fans(self, root: Path, rpm: tuple[int, ...], celsius: float):
        thermal = root / "thermal" / "thermal_zone0"
        thermal.mkdir(parents=True)
        (thermal / "temp").write_text(f"{int(celsius * 1000)}\n", encoding="ascii")
        for index, name in ((0, "cooling_device0"), (1, "cooling_device1")):
            device = root / "thermal" / name
            device.mkdir()
            (device / "type").write_text("Fan\n", encoding="ascii")
            (device / "cur_state").write_text("40\n", encoding="ascii")
            (device / "max_state").write_text("100\n", encoding="ascii")
        hwmon = root / "hwmon" / "hwmon0"
        hwmon.mkdir(parents=True)
        (hwmon / "name").write_text("acpi_fan\n", encoding="ascii")
        for index, value in enumerate(rpm, start=1):
            (hwmon / f"fan{index}_input").write_text(f"{value}\n", encoding="ascii")
        return hwmon

    def test_cold_stall_alerts_once_and_does_not_power_off(self):
        self.assertEqual(CONTROL.WATCHDOG_STALL_SECONDS, 15.0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._stopped_acpi_fans(root, (0, 0), 39.0)
            clock = [0.0]
            stop = Event()
            alerts = []
            actions = []

            def wait(delay):
                clock[0] += delay
                if clock[0] > 30:
                    stop.set()

            with patch.object(CONTROL, "Event", return_value=stop), \
                 patch.object(CONTROL.signal, "signal"), \
                 patch.object(CONTROL, "module_is_loaded", return_value=False), \
                 patch.object(stop, "wait", side_effect=wait), \
                 self.assertLogs(CONTROL.LOG, level="CRITICAL") as logs:
                status = CONTROL.run_watchdog(
                    root / "thermal",
                    root / "hwmon",
                    poll_seconds=2,
                    stall_seconds=15,
                    zero_seconds=10,
                    soc_celsius=70,
                    monotonic=lambda: clock[0],
                    runner=lambda action: actions.append(action),
                    unit_state=lambda: "active",
                    notifier=alerts.append,
                )
            self.assertEqual(status, 0)
            self.assertEqual(actions, [])
            self.assertEqual(len(alerts), 1)
            self.assertIn("fans are stopped", alerts[0])
            self.assertEqual(sum("wrote ACPI Fan level 100" in line for line in logs.output), 1)
            for name in ("cooling_device0", "cooling_device1"):
                self.assertEqual(
                    (root / "thermal" / name / "cur_state").read_text(), "100\n",
                )

            clock[0] = 0.0
            stop.clear()
            alerts.clear()
            for name in ("cooling_device0", "cooling_device1"):
                (root / "thermal" / name / "cur_state").write_text("40\n", encoding="ascii")
            with patch.object(CONTROL, "Event", return_value=stop), \
                 patch.object(CONTROL.signal, "signal"), \
                 patch.object(CONTROL, "module_is_loaded", return_value=True), \
                 patch.object(stop, "wait", side_effect=wait), \
                 self.assertLogs(CONTROL.LOG, level="ERROR") as logs:
                status = CONTROL.run_watchdog(
                    root / "thermal",
                    root / "hwmon",
                    poll_seconds=2,
                    stall_seconds=15,
                    monotonic=lambda: clock[0],
                    runner=lambda action: actions.append(action),
                    unit_state=lambda: "active",
                    notifier=alerts.append,
                )
            self.assertEqual(status, 0)
            self.assertEqual(actions, [])
            self.assertEqual(len(alerts), 1)
            self.assertEqual(sum("not writing ACPI" in line for line in logs.output), 1)
            self.assertEqual(
                (root / "thermal" / "cooling_device0" / "cur_state").read_text(), "40\n",
            )

    def _drive_watchdog(self, root, *, rpm, celsius, gpu, stop_after, boot="boot-a",
                        marker=None, module=False, soc=85.0, reboot_seconds=60):
        self._stopped_acpi_fans(root, rpm, celsius)
        if module:
            device = root / "thermal" / "cooling_device2"
            device.mkdir()
            (device / "type").write_text(
                CONTROL.COOLING_DEVICE_TYPE + "\n", encoding="ascii",
            )
            (device / "cur_state").write_text("1\n", encoding="ascii")
            (device / "max_state").write_text("12\n", encoding="ascii")
        if marker is None:
            marker = root / "marker" / "true-zero-reboot"
        clock = [0.0]
        stop = Event()
        actions = []
        alerts = []

        def wait(delay):
            clock[0] += 2
            if clock[0] > stop_after:
                stop.set()

        with patch.object(CONTROL, "Event", return_value=stop), \
             patch.object(CONTROL.signal, "signal"), \
             patch.object(CONTROL, "module_is_loaded", return_value=module), \
             patch.object(stop, "wait", side_effect=wait), \
             self.assertLogs(CONTROL.LOG, level="INFO") as logs:
            status = CONTROL.run_watchdog(
                root / "thermal",
                root / "hwmon",
                poll_seconds=2,
                stall_seconds=15,
                reboot_seconds=reboot_seconds,
                soc_celsius=soc,
                monotonic=lambda: clock[0],
                wall_time=lambda: clock[0],
                runner=lambda action: actions.append(action) or 0,
                unit_state=lambda: "active",
                notifier=alerts.append,
                gpu_jobs=gpu,
                marker_path=marker,
                boot_id=boot,
                nonzero_clear_seconds=30,
            )
        return status, actions, alerts, logs.output, marker

    def test_true_zero_reboots_once_without_a_job_and_powers_off_next_boot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status, actions, _alerts, logs, marker = self._drive_watchdog(
                root, rpm=(0, 0), celsius=40, gpu=lambda: False, stop_after=80,
            )
            self.assertEqual(status, 0)
            self.assertEqual(actions, [CONTROL.WATCHDOG_REBOOT_ACTION])
            self.assertIn("no GPU job", "\n".join(logs))
            self.assertIn("boot_id=boot-a", marker.read_text(encoding="ascii"))

            root2 = Path(directory) / "next"
            root2.mkdir()
            marker.write_text("boot_id=boot-a\ntime=1.000\n", encoding="ascii")
            status, actions, _alerts, logs, _marker = self._drive_watchdog(
                root2, rpm=(0, 0), celsius=40, gpu=lambda: False, stop_after=80,
                boot="boot-b", marker=marker,
            )
            self.assertEqual(status, 0)
            self.assertEqual(actions, [CONTROL.WATCHDOG_DEFAULT_ACTION])
            self.assertIn("already rebooted once", "\n".join(logs))
            self.assertNotIn(CONTROL.WATCHDOG_REBOOT_ACTION, actions)

    def test_true_zero_defers_for_a_job_then_reboots_when_it_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock_box = {"job": True}

            def gpu():
                return clock_box["job"]

            self._stopped_acpi_fans(root, (0, 0), 40)
            clock = [0.0]
            stop = Event()
            actions = []

            def wait(delay):
                clock[0] += 2
                if clock[0] >= 70:
                    clock_box["job"] = False
                if actions or clock[0] > 90:
                    stop.set()

            with patch.object(CONTROL, "Event", return_value=stop), \
                 patch.object(CONTROL.signal, "signal"), \
                 patch.object(CONTROL, "module_is_loaded", return_value=False), \
                 patch.object(stop, "wait", side_effect=wait), \
                 self.assertLogs(CONTROL.LOG, level="INFO") as logs:
                CONTROL.run_watchdog(
                    root / "thermal", root / "hwmon",
                    poll_seconds=2, stall_seconds=15, reboot_seconds=60,
                    soc_celsius=85, monotonic=lambda: clock[0],
                    wall_time=lambda: 10.0,
                    runner=lambda action: actions.append(action) or 0,
                    unit_state=lambda: "active", notifier=lambda message: None,
                    gpu_jobs=gpu, marker_path=root / "marker", boot_id="boot-a",
                )
            self.assertEqual(actions, [CONTROL.WATCHDOG_REBOOT_ACTION])
            text = "\n".join(logs.output)
            self.assertIn("deferring reboot", text)
            self.assertLess(
                text.index("deferring reboot"),
                text.index("no GPU job and fans still"),
            )

    def test_true_zero_with_a_job_powers_off_at_85c(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _status, actions, _alerts, logs, _marker = self._drive_watchdog(
                root, rpm=(0, 0), celsius=86, gpu=lambda: True, stop_after=40,
                soc=85,
            )
            self.assertEqual(actions, [CONTROL.WATCHDOG_DEFAULT_ACTION])
            self.assertIn("hottest zone", "\n".join(logs))
            self.assertNotIn(CONTROL.WATCHDOG_REBOOT_ACTION, actions)

    def test_nvidia_smi_failure_counts_as_a_running_job(self):
        self.assertIsNone(self._gpu_failure())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _status, actions, _alerts, logs, _marker = self._drive_watchdog(
                root, rpm=(0, 0), celsius=40, gpu=lambda: None, stop_after=80,
            )
            self.assertEqual(actions, [])
            self.assertIn("nvidia-smi failed; treating a GPU job as running", "\n".join(logs))
            self.assertIn("deferring reboot", "\n".join(logs))

    def _gpu_failure(self):
        with patch.object(CONTROL.subprocess, "run", side_effect=OSError("missing")):
            return CONTROL.gpu_job_running()

    def test_one_spinning_fan_does_not_reboot_or_power_off(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _status, actions, alerts, _logs, marker = self._drive_watchdog(
                root, rpm=(0, 2000), celsius=90, gpu=lambda: False, stop_after=80,
                soc=85,
            )
            self.assertEqual(actions, [])
            self.assertEqual(alerts, [])
            self.assertFalse(marker.exists())

    def test_loaded_module_stall_uses_the_module_floor_not_acpi(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _status, actions, _alerts, logs, _marker = self._drive_watchdog(
                root, rpm=(0, 0), celsius=40, gpu=lambda: True, stop_after=20,
                module=True, soc=85,
            )
            device = root / "thermal" / "cooling_device2"
            self.assertEqual(actions, [])
            self.assertEqual((device / "cur_state").read_text(encoding="ascii"), "12\n")
            self.assertEqual(
                (root / "thermal" / "cooling_device0" / "cur_state").read_text(),
                "40\n",
            )
            self.assertIn("did not write ACPI", "\n".join(logs))

    def test_daemon_down_wall_is_on_change_and_then_half_hourly(self):
        clock = [0.0]
        stop = Event()
        alerts = []

        def wait(delay):
            if clock[0] < 40:
                clock[0] += 10
            elif clock[0] == 40:
                clock[0] = 1800
            else:
                stop.set()

        with patch.object(CONTROL, "Event", return_value=stop), \
             patch.object(CONTROL.signal, "signal"), \
             patch.object(stop, "wait", side_effect=wait), \
             self.assertLogs(CONTROL.LOG, level="WARNING") as logs:
            CONTROL.run_watchdog(
                Path("/tmp/dgx-fan-missing"),
                Path("/tmp/dgx-fan-missing"),
                monotonic=lambda: clock[0],
                unit_state=lambda: ("inactive", "enabled", 0),
                notifier=alerts.append,
                alert_repeat_seconds=1800,
                gpu_jobs=lambda: False,
            )
        self.assertEqual(len(alerts), 2)
        warnings = [line for line in logs.output if "is not active" in line]
        self.assertGreaterEqual(len(warnings), 6)

    def test_foreign_floor_is_one_broadcast_and_does_not_reboot(self):
        clock = [0.0]
        stop = Event()
        alerts = []
        actions = []

        def wait(delay):
            clock[0] += 2
            if clock[0] > 10:
                stop.set()

        with patch.object(CONTROL, "Event", return_value=stop), \
             patch.object(CONTROL.signal, "signal"), \
             patch.object(stop, "wait", side_effect=wait), \
             self.assertLogs(CONTROL.LOG, level="INFO") as logs:
            status = CONTROL.run_watchdog(
                Path("/tmp/dgx-fan-missing-floor"),
                Path("/tmp/dgx-fan-missing-floor"),
                monotonic=lambda: clock[0],
                unit_state=lambda: ("inactive", "enabled", CONTROL.FOREIGN_FLOOR_EXIT),
                notifier=alerts.append,
                runner=lambda action: actions.append(action) or 0,
                gpu_jobs=lambda: False,
            )
        self.assertEqual(status, 0)
        self.assertEqual(actions, [])
        self.assertEqual(len(alerts), 1)
        text = "\n".join(logs.output)
        self.assertGreater(text.count("a reboot clears the ACPI floor"), 1)
        self.assertIn("not rebooting", text)

    def test_foreign_floor_daemon_exits_78(self):
        message = (
            "no dgx_ec_fan_floor cooling device: probe refused a foreign lower floor. "
            "a reboot clears the ACPI floor."
        )
        with patch.object(CONTROL, "require_control", side_effect=CONTROL.ForeignFloor(message)):
            code, stdout, stderr = run_cli(["daemon"])
        combined = stdout + stderr
        self.assertEqual(code, CONTROL.FOREIGN_FLOOR_EXIT)
        self.assertEqual(combined.count("a reboot clears the ACPI floor"), 1)
        self.assertNotIn("Traceback", combined)

    def test_one_spinning_fan_or_one_input_does_not_alert(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._stopped_acpi_fans(root, (0, 2000), 39.0)
            clock = [0.0]
            stop = Event()
            alerts = []
            actions = []

            def wait(delay):
                clock[0] += delay
                if clock[0] > 30:
                    stop.set()

            with patch.object(CONTROL, "Event", return_value=stop), \
                 patch.object(CONTROL.signal, "signal"), \
                 patch.object(stop, "wait", side_effect=wait), \
                 self.assertLogs(CONTROL.LOG, level="INFO"):
                status = CONTROL.run_watchdog(
                    root / "thermal",
                    root / "hwmon",
                    poll_seconds=2,
                    stall_seconds=15,
                    monotonic=lambda: clock[0],
                    runner=lambda action: actions.append(action),
                    unit_state=lambda: "active",
                    notifier=alerts.append,
                )
            self.assertEqual(status, 0)
            self.assertEqual(alerts, [])
            self.assertEqual(actions, [])

            (root / "hwmon" / "hwmon0" / "fan2_input").unlink()
            clock[0] = 0.0
            stop.clear()
            with patch.object(CONTROL, "Event", return_value=stop), \
                 patch.object(CONTROL.signal, "signal"), \
                 patch.object(stop, "wait", side_effect=wait), \
                 self.assertLogs(CONTROL.LOG, level="WARNING") as logs:
                status = CONTROL.run_watchdog(
                    root / "thermal",
                    root / "hwmon",
                    poll_seconds=2,
                    stall_seconds=15,
                    monotonic=lambda: clock[0],
                    runner=lambda action: actions.append(action),
                    unit_state=lambda: "active",
                    notifier=alerts.append,
                )
            self.assertEqual(status, 0)
            self.assertEqual(alerts, [])
            self.assertEqual(actions, [])
            self.assertTrue(any("will not trip" in line for line in logs.output))

    def _acpi_tree(self, root: Path) -> tuple[Path, Path, Path]:
        thermal = root / "thermal"
        thermal.mkdir()
        ours = thermal / "cooling_device0"
        ours.mkdir()
        (ours / "type").write_text("dgx_ec_fan_floor\n", encoding="ascii")
        (ours / "cur_state").write_text("0\n", encoding="ascii")
        (ours / "max_state").write_text("12\n", encoding="ascii")
        fans = []
        for index, name in ((1, "cooling_device1"), (2, "cooling_device2")):
            device = thermal / name
            device.mkdir()
            (device / "type").write_text("Fan\n", encoding="ascii")
            (device / "cur_state").write_text("40\n", encoding="ascii")
            (device / "max_state").write_text("100\n", encoding="ascii")
            fans.append(device)
        return thermal, fans[0], fans[1]

    def test_acpi_max_writes_only_fan_devices(self):
        with tempfile.TemporaryDirectory() as directory:
            thermal, first, second = self._acpi_tree(Path(directory))
            code, stdout, stderr = run_cli([
                "--thermal-root", str(thermal), "acpi-max",
            ])
            text = stdout + stderr
            self.assertEqual(code, 0)
            self.assertEqual((first / "cur_state").read_text(), "100\n")
            self.assertEqual((second / "cur_state").read_text(), "100\n")
            self.assertEqual(
                (thermal / "cooling_device0" / "cur_state").read_text(), "0\n"
            )
            self.assertEqual(stdout.count("cooling_device1 (Fan): 100/100\n"), 1)
            self.assertEqual(stdout.count("cooling_device2 (Fan): 100/100\n"), 1)
            self.assertNotIn("cooling_device", stderr)
            self.assertNotIn("cooling_device0", text)
            self.assertEqual(text.count(CONTROL.ACPI_RPM_NOTE), 1)
            self.assertIn("without changing RPM", stdout)
            self.assertIn("check fan RPM afterward", stdout)

    def test_acpi_max_exits_nonzero_without_a_fan_or_when_a_write_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            empty = root / "empty"
            empty.mkdir()
            code, stdout, stderr = run_cli(["--thermal-root", str(empty), "acpi-max"])
            self.assertEqual(code, 1)
            self.assertEqual((stdout + stderr).count("no ACPI Fan cooling device found"), 1)
            self.assertNotIn("no ACPI Fan cooling device found", stdout)
            self.assertNotIn(CONTROL.ACPI_RPM_NOTE, stdout + stderr)

            thermal, first, second = self._acpi_tree(root)
            original = CONTROL.write_acpi_fan_level

            def broken_write(device, level):
                if device == second:
                    raise OSError("write failed")
                original(device, level)

            with patch.object(CONTROL, "write_acpi_fan_level", side_effect=broken_write):
                code, stdout, stderr = run_cli(["--thermal-root", str(thermal), "acpi-max"])
            self.assertEqual(code, 1)
            self.assertEqual((stdout + stderr).count("failed on 1 device"), 1)
            self.assertNotIn("failed on 1 device", stdout)
            self.assertNotIn(CONTROL.ACPI_RPM_NOTE, stdout + stderr)
            self.assertNotIn("cooling_device", stdout)

    def test_acpi_level_refuses_below_forty_and_fail_hot_rewrites_one_hundred(self):
        with tempfile.TemporaryDirectory() as directory:
            thermal, first, second = self._acpi_tree(Path(directory))
            code, stdout, stderr = run_cli([
                "--thermal-root", str(thermal), "acpi-max", "--level", "20",
            ])
            self.assertEqual(code, 1)
            self.assertEqual((stdout + stderr).count("refusing ACPI fan level 20"), 1)
            self.assertIn("stops both fans", stderr)
            self.assertNotIn("refusing ACPI fan level", stdout)
            self.assertNotIn("Traceback", stdout + stderr)
            self.assertEqual((first / "cur_state").read_text(), "40\n")
            self.assertEqual((second / "cur_state").read_text(), "40\n")
            code, stdout, stderr = run_cli([
                "--thermal-root", str(thermal), "acpi-max", "--level", "40",
            ])
            self.assertEqual(code, 0)
            self.assertEqual((stdout + stderr).count(CONTROL.ACPI_RPM_NOTE), 1)
            self.assertEqual(stdout.count("cooling_device1 (Fan): 40/100\n"), 1)
            self.assertNotIn("40/100", stderr)
            self.assertEqual((first / "cur_state").read_text(), "40\n")
            (second / "cur_state").write_text("7\n", encoding="ascii")

            original = CONTROL.write_acpi_fan_level

            def broken_write(device, level):
                if device == second and level == 60:
                    raise OSError("write failed")
                original(device, level)

            with patch.object(CONTROL, "write_acpi_fan_level", side_effect=broken_write):
                with self.assertRaises(RuntimeError):
                    CONTROL.set_acpi_fans(thermal, 60)
            self.assertEqual((first / "cur_state").read_text(), "100\n")
            self.assertEqual((second / "cur_state").read_text(), "100\n")

    def test_supported_commands_do_not_repeat_their_output(self):
        with tempfile.TemporaryDirectory() as directory:
            thermal = Path(directory)
            device = thermal / "cooling_device0"
            device.mkdir()
            (device / "type").write_text(CONTROL.COOLING_DEVICE_TYPE + "\n", encoding="ascii")
            (device / "cur_state").write_text("4\n", encoding="ascii")
            (device / "max_state").write_text("12\n", encoding="ascii")
            zone = thermal / "thermal_zone0"
            zone.mkdir()
            (zone / "temp").write_text("50000\n", encoding="ascii")
            argv = ["--thermal-root", str(thermal)]
            code, stdout, stderr = run_cli([*argv, "status"])
            self.assertEqual(code, 0)
            self.assertTrue(stdout.startswith("state=4/12 hottest=50.0C\n"))
            self.assertIn("watchdog-active:", stdout)
            self.assertIn("watchdog-enabled:", stdout)
            self.assertEqual(stderr, "")
            code, stdout, stderr = run_cli([*argv, "set-state", "6"])
            self.assertEqual(code, 0)
            self.assertEqual(stdout, "")
            self.assertEqual(stderr, "")
            self.assertEqual((device / "cur_state").read_text(), "6\n")
            code, stdout, stderr = run_cli([*argv, "automatic"])
            self.assertEqual(code, 0)
            self.assertEqual(stdout, "")
            self.assertEqual(stderr, "")
            self.assertEqual((device / "cur_state").read_text(), "0\n")
            code, stdout, stderr = run_cli([*argv, "status"])
            self.assertEqual(code, 0)
            self.assertTrue(
                stdout.startswith("state=0/12 (no floor; EC curve) hottest=50.0C\n")
            )
            self.assertEqual(stdout.count("watchdog-active:"), 1)
            self.assertEqual(stdout.count("watchdog-enabled:"), 1)
            self.assertEqual(stderr, "")

    def test_unknown_curve_and_unprivileged_module_are_reported_once(self):
        with patch.dict("os.environ", {"DGX_FAN_CURVE": "nope"}):
            code, stdout, stderr = run_cli(["daemon"])
        self.assertEqual(code, 1)
        self.assertEqual((stdout + stderr).count("unknown fan curve"), 1)
        self.assertNotIn("unknown fan curve", stdout)
        with patch.object(CONTROL.os, "geteuid", return_value=1000):
            for action in ("status", "enable", "disable"):
                code, stdout, stderr = run_cli(["module", action])
                self.assertEqual(code, 1)
                self.assertEqual((stdout + stderr).count("requires root"), 1)
                self.assertNotIn("requires root", stdout)

    def test_daemon_failures_are_logged_once(self):
        timeout = OSError(errno.ETIMEDOUT, "initial timeout")
        with patch.object(CONTROL, "require_control", return_value=Path("device")), \
             patch.object(CONTROL, "read_state", side_effect=timeout), \
             patch.object(CONTROL, "write_state"), \
             patch.object(CONTROL.signal, "signal"), \
             patch.object(CONTROL.Event, "wait", return_value=False):
            code, stdout, stderr = run_cli(["daemon"])
        combined = stdout + stderr
        self.assertEqual(code, 1)
        self.assertEqual(combined.count("fan control unavailable"), 1)
        self.assertEqual(combined.count("initial timeout"), 3)
        self.assertFalse(any(
            line.strip() == "ERROR [Errno 110] initial timeout" for line in combined.splitlines()
        ))
        self.assertNotIn("initial timeout", stdout)

        stop = Event()

        def wait(delay):
            stop.set()

        def write(device, state):
            if state == 0:
                raise OSError(errno.EBUSY, "cleanup busy")

        with patch.object(CONTROL, "require_control", return_value=Path("device")), \
             patch.object(CONTROL, "Event", return_value=stop), \
             patch.object(CONTROL.signal, "signal"), \
             patch.object(CONTROL, "read_state", return_value=12), \
             patch.object(CONTROL, "read_hottest_temperature_c", return_value=70.0), \
             patch.object(CONTROL, "write_state", side_effect=write), \
             patch.object(stop, "wait", side_effect=wait):
            code, stdout, stderr = run_cli(["daemon"])
        combined = stdout + stderr
        self.assertEqual(code, 1)
        self.assertEqual(combined.count("automatic restoration failed"), 1)
        self.assertEqual(sum(
            1 for line in combined.splitlines()
            if line.startswith("ERROR ") and "cleanup busy" in line
        ), 1)
        self.assertNotIn("Traceback", combined)
        self.assertNotIn("cleanup busy", stdout)

    def test_watchdog_alert_is_logged_once_and_not_printed(self):
        stop = Event()
        alerts = []
        stdout, stderr = io.StringIO(), io.StringIO()
        root = logging.getLogger()
        handler = logging.StreamHandler(stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        root.addHandler(handler)
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr), \
                 patch.object(CONTROL, "Event", return_value=stop), \
                 patch.object(CONTROL.signal, "signal"), \
                 patch.object(stop, "wait", side_effect=lambda delay: stop.set()):
                code = CONTROL.run_watchdog(
                    Path("/tmp/dgx-fan-missing"),
                    Path("/tmp/dgx-fan-missing"),
                    unit_state=lambda: ("inactive", "enabled", 1),
                    notifier=alerts.append,
                    monotonic=lambda: 0.0,
                )
        finally:
            root.removeHandler(handler)
        self.assertEqual(code, 0)
        self.assertEqual(len(alerts), 1)
        combined = stdout.getvalue() + stderr.getvalue()
        self.assertEqual(combined.count(alerts[0]), 1)
        self.assertEqual(combined.count("fan-stall watchdog watching"), 1)
        self.assertEqual(stdout.getvalue(), "")

    def test_status_explains_a_missing_cooling_device(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            thermal = root / "thermal"
            hwmon = root / "hwmon"
            thermal.mkdir()
            hwmon.mkdir()
            argv = ["--thermal-root", str(thermal), "--hwmon-root", str(hwmon), "status"]
            with patch.object(CONTROL, "module_is_loaded", return_value=False):
                code, stdout, stderr = run_cli(argv)
            combined = stdout + stderr
            self.assertEqual(code, 1)
            self.assertIn("dgx_ec_fan_control is not loaded", combined)
            self.assertNotIn("found 0", combined)
            self.assertEqual(combined.count("is not loaded"), 1)
            self.assertNotIn("Traceback", combined)

            foreign = "refusing to replace existing lower floor 0x10fe (4350 RPM)\n"
            with patch.object(CONTROL, "module_is_loaded", return_value=True), \
                 patch.object(CONTROL, "read_kernel_log", return_value=foreign):
                code, stdout, stderr = run_cli(argv)
            combined = stdout + stderr
            self.assertEqual(code, 1)
            self.assertIn("probe refused a foreign lower floor", combined)
            self.assertIn("a reboot clears the ACPI floor", combined)
            self.assertIn("do not write ACPI level 0", combined)
            self.assertNotIn("found 0", combined)
            self.assertEqual(combined.count("foreign lower floor"), 1)

            device = hwmon / "hwmon0"
            device.mkdir()
            (device / "name").write_text(CONTROL.HWMON_DEVICE_NAME + "\n", encoding="ascii")
            code, stdout, stderr = run_cli(argv)
            combined = stdout + stderr
            self.assertEqual(code, 0)
            self.assertEqual(combined.count(CONTROL.CONTROL_UNSUPPORTED), 1)
            self.assertNotIn(CONTROL.CONTROL_UNSUPPORTED, stderr)

    def test_estale_names_the_outside_floor_change_once(self):
        with tempfile.TemporaryDirectory() as directory:
            thermal = Path(directory)
            device = thermal / "cooling_device0"
            device.mkdir()
            (device / "type").write_text(CONTROL.COOLING_DEVICE_TYPE + "\n", encoding="ascii")
            (device / "cur_state").write_text("12\n", encoding="ascii")
            (device / "max_state").write_text("12\n", encoding="ascii")
            zone = thermal / "thermal_zone0"
            zone.mkdir()
            (zone / "temp").write_text("39000\n", encoding="ascii")
            log = (
                "floor ownership mismatch: observed=0x10fe (4350 RPM) "
                "confirmed=0x34bc (13500 RPM)\n"
            )
            original = Path.read_text

            def stale_cur_state(self, *args, **kwargs):
                if self.name == "cur_state":
                    raise OSError(errno.ESTALE, "Stale file handle")
                return original(self, *args, **kwargs)

            argv = ["--thermal-root", str(thermal), "status"]
            with patch.object(Path, "read_text", stale_cur_state), \
                 patch.object(CONTROL, "read_kernel_log", return_value=log):
                code, stdout, stderr = run_cli(argv)
            combined = stdout + stderr
            self.assertEqual(code, 1)
            self.assertIn(
                "floor was changed outside this driver (observed 4350 RPM, expected 13500)",
                combined,
            )
            self.assertEqual(combined.count("floor was changed outside this driver"), 1)
            self.assertNotIn("Stale file handle", combined)
            self.assertNotIn("Traceback", combined)
            self.assertIn("Do not write ACPI level 0", combined)

    def test_cur_state_timeout_exits_once_without_a_traceback(self):
        timeout = TimeoutError(errno.ETIMEDOUT, "Connection timed out")
        with patch.object(CONTROL, "require_control", return_value=Path("device")), \
             patch.object(CONTROL, "read_state", side_effect=timeout), \
             patch.object(CONTROL, "write_state"), \
             patch.object(CONTROL.signal, "signal"), \
             patch.object(CONTROL.Event, "wait", return_value=False):
            code, stdout, stderr = run_cli(["daemon"])
        combined = stdout + stderr
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", combined)
        self.assertEqual(combined.count("fan control unavailable"), 1)
        self.assertEqual(combined.count("Connection timed out"), 3)
        self.assertNotIn("Connection timed out", stdout)

    def test_acpi_max_refuses_a_live_module_and_level_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            thermal, first, _second = self._acpi_tree(Path(directory))
            argv = ["--thermal-root", str(thermal), "acpi-max"]
            with patch.object(CONTROL, "module_is_loaded", return_value=False), \
                 patch.object(CONTROL, "systemctl_is_active", return_value="active"):
                code, stdout, stderr = run_cli(argv)
            combined = stdout + stderr
            self.assertEqual(code, 1)
            self.assertEqual(combined.count("refusing ACPI write"), 1)
            self.assertIn("service is active", combined)
            self.assertIn("Do not write ACPI level 0", combined)
            self.assertNotIn("Traceback", combined)
            self.assertEqual((first / "cur_state").read_text(), "40\n")

            with patch.object(CONTROL, "module_is_loaded", return_value=False), \
                 patch.object(CONTROL, "systemctl_is_active", return_value="inactive"):
                code, stdout, stderr = run_cli([*argv, "--level", "0"])
            combined = stdout + stderr
            self.assertEqual(code, 1)
            self.assertIn("stops both fans", combined)
            self.assertEqual(combined.count("refusing ACPI fan level 0"), 1)
            self.assertNotIn("Traceback", combined)
            self.assertEqual((first / "cur_state").read_text(), "40\n")

            stdout_buffer = io.StringIO()
            with redirect_stdout(stdout_buffer):
                CONTROL.set_acpi_fans(
                    thermal, 100, module_loaded=False, service_active="inactive",
                    profile="EC 3.13.7",
                )
            lines = stdout_buffer.getvalue().strip().splitlines()
            self.assertEqual(lines[-1], CONTROL.ACPI_RPM_NOTE)
            stdout_buffer = io.StringIO()
            with redirect_stdout(stdout_buffer):
                CONTROL.set_acpi_fans(
                    thermal, 100, module_loaded=False, service_active="inactive",
                    profile=None,
                )
            self.assertEqual(
                stdout_buffer.getvalue().strip().splitlines()[-1],
                CONTROL.ACPI_RPM_NOTE,
            )

    def test_runtime_daemon_error_stays_restartable(self):
        with patch.object(CONTROL, "run_daemon", side_effect=OSError(errno.ETIMEDOUT, "timeout")):
            code, stdout, stderr = run_cli(["daemon"])
        self.assertEqual(code, 1)
        self.assertEqual((stdout + stderr).count("timeout"), 1)
        self.assertNotIn("timeout", stdout)


if __name__ == "__main__":
    unittest.main()
