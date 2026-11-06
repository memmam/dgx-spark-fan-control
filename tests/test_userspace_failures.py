# SPDX-License-Identifier: GPL-2.0-only
"""Failure-path checks without touching the host's thermal interfaces."""

import errno
import io
import math
from contextlib import redirect_stdout
from threading import Event
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_fan_control_contract import CONTROL


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
            output = io.StringIO()
            with redirect_stdout(output), self.assertLogs(CONTROL.LOG, level="ERROR") as logs:
                self.assertEqual(CONTROL.main([*argv, "status"]), 0)
            self.assertIn(CONTROL.CONTROL_UNSUPPORTED, output.getvalue())
            self.assertTrue(any(CONTROL.CONTROL_UNSUPPORTED in line for line in logs.output))
            with self.assertLogs(CONTROL.LOG, level="ERROR"):
                self.assertEqual(CONTROL.main([*argv, "daemon"]), CONTROL.UNSUPPORTED_FIRMWARE_EXIT)
                self.assertEqual(CONTROL.main([*argv, "set-state", "1"]), 1)
                self.assertEqual(CONTROL.main([*argv, "automatic"]), 1)

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
                 self.assertLogs(CONTROL.LOG, level="CRITICAL") as logs:
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
        self.assertFalse(CONTROL.fans_stalled([0, 0], 70.0, 10.0))
        self.assertTrue(CONTROL.fans_stalled([0, 0], 70.0, 10.01))
        self.assertFalse(CONTROL.fans_stalled([0, 0], 69.9, 30.0))
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
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(CONTROL.main([
                    "--thermal-root", str(thermal), "acpi-max",
                ]), 0)
            text = stdout.getvalue()
            self.assertEqual((first / "cur_state").read_text(), "100\n")
            self.assertEqual((second / "cur_state").read_text(), "100\n")
            self.assertEqual(
                (thermal / "cooling_device0" / "cur_state").read_text(), "0\n"
            )
            self.assertIn("cooling_device1 (Fan): 100/100\n", text)
            self.assertIn("cooling_device2 (Fan): 100/100\n", text)
            self.assertNotIn("cooling_device0", text)
            self.assertEqual(text.count(CONTROL.ACPI_RPM_NOTE), 1)
            self.assertIn("without changing RPM", text)
            self.assertIn("check fan RPM afterward", text)

    def test_acpi_max_exits_nonzero_without_a_fan_or_when_a_write_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            empty = root / "empty"
            empty.mkdir()
            stdout = io.StringIO()
            with redirect_stdout(stdout), self.assertLogs(CONTROL.LOG, level="ERROR"):
                self.assertEqual(CONTROL.main([
                    "--thermal-root", str(empty), "acpi-max",
                ]), 1)
            self.assertNotIn(CONTROL.ACPI_RPM_NOTE, stdout.getvalue())

            thermal, first, second = self._acpi_tree(root)
            original = CONTROL.write_acpi_fan_level

            def broken_write(device, level):
                if device == second:
                    raise OSError("write failed")
                original(device, level)

            stdout = io.StringIO()
            with redirect_stdout(stdout), \
                 patch.object(CONTROL, "write_acpi_fan_level", side_effect=broken_write), \
                 self.assertLogs(CONTROL.LOG, level="ERROR"):
                self.assertEqual(CONTROL.main([
                    "--thermal-root", str(thermal), "acpi-max",
                ]), 1)
            self.assertNotIn(CONTROL.ACPI_RPM_NOTE, stdout.getvalue())
            self.assertNotIn("cooling_device", stdout.getvalue())

    def test_acpi_level_refuses_below_forty_and_fail_hot_rewrites_one_hundred(self):
        with tempfile.TemporaryDirectory() as directory:
            thermal, first, second = self._acpi_tree(Path(directory))
            with self.assertLogs(CONTROL.LOG, level="ERROR"):
                self.assertEqual(CONTROL.main([
                    "--thermal-root", str(thermal), "acpi-max", "--level", "20",
                ]), 1)
            self.assertEqual((first / "cur_state").read_text(), "40\n")
            self.assertEqual((second / "cur_state").read_text(), "40\n")
            self.assertEqual(CONTROL.main([
                "--thermal-root", str(thermal), "acpi-max", "--level", "40",
            ]), 0)
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

    def test_runtime_daemon_error_stays_restartable(self):
        with patch.object(CONTROL, "run_daemon", side_effect=OSError(errno.ETIMEDOUT, "timeout")), \
             self.assertLogs(CONTROL.LOG, level="ERROR"):
            self.assertEqual(CONTROL.main(["daemon"]), 1)


if __name__ == "__main__":
    unittest.main()
