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
        states = iter((OSError(errno.EAGAIN, "reply guard"), 1))

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
        original = OSError(errno.EAGAIN, "reply guard")
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
                self.assertEqual(CONTROL.main([*argv, "daemon"]), 0)
                self.assertEqual(CONTROL.main([*argv, "set-state", "1"]), 1)
                self.assertEqual(CONTROL.main([*argv, "automatic"]), 1)

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
                )
            self.assertEqual(status, 0)
            self.assertEqual(actions, [])

    def test_expected_daemon_error_uses_nonrestart_exit_status(self):
        with patch.object(CONTROL, "run_daemon", side_effect=OSError(errno.ETIMEDOUT, "timeout")), \
             self.assertLogs(CONTROL.LOG, level="ERROR"):
            self.assertEqual(CONTROL.main(["daemon"]), 69)


if __name__ == "__main__":
    unittest.main()
