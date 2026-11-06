# SPDX-License-Identifier: GPL-2.0-only
"""Failure-path checks without touching the host's thermal interfaces."""

import errno
import io
import json
import logging
import math
import os
import subprocess
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

            state = root / "state"
            CONTROL.write_last_manual_state(state, 6, floor=2700, profile="EC 3.5.8")
            with patch.object(CONTROL, "STALL_MARKER_DIR", state), \
                 patch.object(CONTROL, "read_defaults", return_value={}), \
                 patch.object(CONTROL, "write_state", side_effect=AssertionError("wrote")):
                code, stdout, stderr = run_cli([*argv, "daemon"])
            self.assertEqual(code, CONTROL.UNSUPPORTED_FIRMWARE_EXIT)
            self.assertNotIn("held floor", stdout + stderr)

            code, stdout, stderr = run_cli([*argv, "set-state", "1"])
            self.assertEqual(code, 1)
            combined = stdout + stderr
            self.assertEqual(combined.count(CONTROL.CONTROL_UNSUPPORTED), 1)
            self.assertEqual(stderr.count(CONTROL.CONTROL_UNSUPPORTED), 1)
            self.assertNotIn(CONTROL.CONTROL_UNSUPPORTED, stdout)

            acpi = thermal / "cooling_device0"
            acpi.mkdir()
            (acpi / "type").write_text("Fan\n", encoding="ascii")
            cur_state = acpi / "cur_state"
            cur_state.write_text("40\n", encoding="ascii")
            (acpi / "max_state").write_text("100\n", encoding="ascii")
            with patch.object(
                CONTROL, "read_kernel_log",
                return_value=(
                    "dgx_ec_fan_control: EC 3.13.7 floor writes disabled; "
                    "registering hwmon telemetry only\n"
                ),
            ), patch.object(CONTROL, "write_acpi_fan_level") as write_acpi:
                code, stdout, stderr = run_cli([*argv, "acpi-max"])
            self.assertEqual(code, 0)
            combined = stdout + stderr
            self.assertIn(
                "acpi-max: nothing written (EC 3.13.7, telemetry only; "
                "EC runs its own curve)",
                combined,
            )
            self.assertNotIn("ERROR", combined)
            write_acpi.assert_not_called()
            self.assertEqual(cur_state.read_text(encoding="ascii"), "40\n")

    def _floor_device(self, root: Path, state: str = "6\n"):
        thermal = root / "thermal"
        thermal.mkdir()
        device = thermal / "cooling_device0"
        device.mkdir()
        (device / "type").write_text(
            CONTROL.COOLING_DEVICE_TYPE + "\n", encoding="ascii",
        )
        (device / "max_state").write_text("12\n", encoding="ascii")
        cur = device / "cur_state"
        cur.write_text(state, encoding="ascii")
        zone = thermal / "thermal_zone0"
        zone.mkdir()
        (zone / "temp").write_text("40000\n", encoding="ascii")
        return cur

    def test_daemon_refuses_a_held_floor_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cur = self._floor_device(root)
            state = root / "state"
            CONTROL.write_last_manual_state(state, 6, floor=2700, profile="EC 3.5.8")
            writes = []
            real_write = CONTROL.write_state

            def record_write(device, value):
                writes.append(value)
                return real_write(device, value)

            argv = ["--thermal-root", str(root / "thermal"), "--hwmon-root", str(root / "hwmon"), "daemon"]
            with patch.object(CONTROL, "STALL_MARKER_DIR", state), \
                 patch.object(CONTROL, "read_defaults", return_value={}), \
                 patch.object(CONTROL, "write_state", side_effect=record_write):
                code, stdout, stderr = run_cli(argv)
            text = stdout + stderr
            self.assertEqual(code, CONTROL.HELD_FLOOR_EXIT)
            self.assertIn(
                "held floor 6 recorded (last-set); not starting the curve; "
                "run dgx-fan-control automatic first",
                text,
            )
            self.assertEqual(writes, [])
            self.assertEqual(cur.read_text(encoding="ascii"), "6\n")
            self.assertNotIn("0", writes)

            with patch.object(CONTROL, "STALL_MARKER_DIR", state), \
                 patch.object(CONTROL, "read_defaults", return_value={"MANUAL_STATE": "4"}), \
                 patch.object(CONTROL, "write_state", side_effect=record_write):
                code, stdout, stderr = run_cli(argv)
            self.assertEqual(code, CONTROL.HELD_FLOOR_EXIT)
            self.assertIn("held floor 4 recorded (MANUAL_STATE)", stdout + stderr)
            self.assertEqual(writes, [])
            self.assertEqual(cur.read_text(encoding="ascii"), "6\n")

            stop = Event()
            with patch.object(CONTROL, "STALL_MARKER_DIR", root / "empty"), \
                 patch.object(CONTROL, "read_defaults", return_value={}), \
                 patch.object(CONTROL, "Event", return_value=stop), \
                 patch.object(CONTROL.signal, "signal"), \
                 patch.object(stop, "wait", side_effect=lambda delay: stop.set()), \
                 patch.object(CONTROL, "write_state", side_effect=record_write):
                code, stdout, stderr = run_cli(argv)
            self.assertEqual(code, 0, stdout + stderr)
            self.assertNotIn("held floor", stdout + stderr)
            self.assertIn(0, writes)

    def test_health_notes_a_held_floor_exit_without_failing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._healthy_tree(root)
            (root / "thermal" / "cooling_device2" / "cur_state").write_text(
                "6\n", encoding="ascii",
            )
            state = root / "state"
            CONTROL.write_last_manual_state(state, 6, floor=2700, profile="EC 3.5.8")
            snapshot = self._healthy_snapshot(
                service_active="failed",
                service_enabled="enabled",
                exec_status=CONTROL.HELD_FLOOR_EXIT,
                n_restarts=5,
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code, _alerts, _notices = self._health(
                    root, {}, snapshot, record=False,
                )
            text = stdout.getvalue()
            self.assertEqual(code, 0, text)
            self.assertIn(
                "daemon: stopped (held floor 6; automatic clears it)", text,
            )
            self.assertNotIn("restart loop", text)
            self.assertNotIn("needs intervention", text)
            self.assertNotIn("health: fail", text)
            self.assertFalse((state / CONTROL.INTERVENTION_NAME).exists())

            snapshot.pop("n_restarts", None)
            bare = self._healthy_snapshot(
                service_active="inactive",
                service_enabled="enabled",
                exec_status=CONTROL.HELD_FLOOR_EXIT,
                record=None,
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code, _alerts, _notices = self._health(
                    root, {}, bare, record=False,
                )
            text = stdout.getvalue()
            self.assertEqual(code, 0, text)
            self.assertIn("daemon: stopped (exit 79, held floor)", text)

    def test_automatic_on_unsupported_firmware_is_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            thermal = root / "thermal"
            hwmon = root / "hwmon" / "hwmon0"
            state = root / "state"
            etc = root / "etc"
            thermal.mkdir()
            hwmon.mkdir(parents=True)
            state.mkdir()
            (etc / "modprobe.d").mkdir(parents=True)
            (hwmon / "name").write_text(CONTROL.HWMON_DEVICE_NAME + "\n", encoding="ascii")
            acpi = thermal / "cooling_device0"
            acpi.mkdir()
            (acpi / "type").write_text("Fan\n", encoding="ascii")
            cur_state = acpi / "cur_state"
            cur_state.write_text("40\n", encoding="ascii")
            CONTROL.write_last_manual_state(state, 6, floor=2700, profile="EC 3.5.8")
            (etc / "modprobe.d" / CONTROL.OWNED_MODPROBE_NAME).write_text(
                "options dgx_ec_fan_control owned_state=6 owned_floor=2700\n",
                encoding="ascii",
            )
            argv = [
                "--thermal-root", str(thermal),
                "--hwmon-root", str(root / "hwmon"),
            ]
            with patch.object(CONTROL, "STALL_MARKER_DIR", state), \
                 patch.object(CONTROL, "clear_owned_module_option") as clear_owned, \
                 patch.object(CONTROL, "disable_manual_oneshot") as disable_oneshot, \
                 patch.object(
                     CONTROL, "read_kernel_log",
                     return_value=(
                         "dgx_ec_fan_control: EC 3.13.7 floor writes disabled; "
                         "registering hwmon telemetry only\n"
                     ),
                 ), \
                 patch.object(CONTROL, "write_state") as write_state, \
                 patch.object(CONTROL, "write_acpi_fan_level") as write_acpi:
                code, stdout, stderr = run_cli([*argv, "automatic"])
            self.assertEqual(code, 0)
            combined = stdout + stderr
            self.assertIn(
                "automatic: nothing to do (EC 3.13.7, telemetry only; "
                "EC runs its own curve)",
                combined,
            )
            self.assertNotIn("ERROR", combined)
            self.assertNotIn(CONTROL.CONTROL_UNSUPPORTED, combined)
            self.assertFalse((state / CONTROL.MANUAL_STATE_NAME).exists())
            clear_owned.assert_called_once()
            disable_oneshot.assert_called_once()
            write_state.assert_not_called()
            write_acpi.assert_not_called()
            self.assertEqual(cur_state.read_text(encoding="ascii"), "40\n")

    def test_acpi_max_on_unsupported_firmware_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            thermal = root / "thermal"
            hwmon = root / "hwmon" / "hwmon0"
            thermal.mkdir()
            hwmon.mkdir(parents=True)
            (hwmon / "name").write_text(CONTROL.HWMON_DEVICE_NAME + "\n", encoding="ascii")
            acpi = thermal / "cooling_device0"
            acpi.mkdir()
            (acpi / "type").write_text("Fan\n", encoding="ascii")
            cur_state = acpi / "cur_state"
            cur_state.write_text("40\n", encoding="ascii")
            (acpi / "max_state").write_text("100\n", encoding="ascii")
            argv = [
                "--thermal-root", str(thermal),
                "--hwmon-root", str(root / "hwmon"),
            ]
            with patch.object(
                CONTROL, "read_kernel_log",
                return_value=(
                    "dgx_ec_fan_control: EC 3.13.7 floor writes disabled; "
                    "registering hwmon telemetry only\n"
                ),
            ), patch.object(CONTROL, "write_acpi_fan_level") as write_acpi, \
                 patch.object(CONTROL, "set_acpi_fans") as set_acpi:
                code, stdout, stderr = run_cli([*argv, "acpi-max"])
                code40, out40, err40 = run_cli([*argv, "acpi-max", "--level", "40"])
            self.assertEqual(code, 0)
            self.assertEqual(code40, 0)
            for combined in (stdout + stderr, out40 + err40):
                self.assertIn(
                    "acpi-max: nothing written (EC 3.13.7, telemetry only; "
                    "EC runs its own curve)",
                    combined,
                )
                self.assertNotIn("ERROR", combined)
                self.assertNotIn(CONTROL.ACPI_RPM_NOTE, combined)
            write_acpi.assert_not_called()
            set_acpi.assert_not_called()
            self.assertEqual(cur_state.read_text(encoding="ascii"), "40\n")

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

    def test_spinning_fans_near_85c_do_not_power_off_or_fail_health(self):
        foreign = ("inactive", "enabled", CONTROL.FOREIGN_FLOOR_EXIT)
        for celsius in (84.0, 90.0):
            for state in ("active", foreign):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    _status, actions, _alerts, logs, _marker = self._drive_watchdog(
                        root, rpm=(2700, 4050), celsius=celsius,
                        gpu=lambda: False, stop_after=12, unit_state=state,
                    )
                    self.assertEqual(actions, [], (celsius, state))
                    self.assertNotIn("running systemctl poweroff", "\n".join(logs))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._healthy_tree(root)
            for millic, expect in (
                (84000, "temperature: pass (84.0C)"),
                (90000, "temperature: warn (hot under load, 90.0C)"),
            ):
                (root / "thermal" / "thermal_zone0" / "temp").write_text(
                    f"{millic}\n", encoding="ascii",
                )
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code, _alerts, _notices = self._health(
                        root, {}, self._healthy_snapshot(), record=True,
                    )
                self.assertEqual(code, 0, expect)
                self.assertIn(expect, stdout.getvalue())
                self.assertFalse(
                    (root / "state" / CONTROL.INTERVENTION_NAME).exists(),
                    expect,
                )
                self.assertIn("progress: off", stdout.getvalue())

    def test_hot_job_tracking_warns_and_never_stops_the_machine(self):
        state_dir_holder = {}

        def drive(root, *, gpu, defaults, progress, builtin, stop_after, stuck=10):
            status, actions, alerts, logs, marker = self._drive_watchdog(
                root, rpm=(2700, 4050), celsius=90, gpu=gpu, stop_after=stop_after,
                stuck_seconds=stuck, defaults=defaults, progress_reader=progress,
                builtin_reader=builtin, alert_repeat_seconds=1800,
            )
            state_dir_holder["marker"] = marker
            return status, actions, alerts, logs

        def no_action(actions, marker):
            self.assertEqual(actions, [])
            self.assertFalse((marker.parent / CONTROL.INTERVENTION_NAME).exists())
            self.assertNotIn("poweroff", " ".join(actions))
            self.assertNotIn("reboot", " ".join(actions))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = iter([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0])
            _status, actions, alerts, logs = drive(
                root, gpu=lambda: True, defaults={"PROGRESS_CHECK": "tokens"},
                progress=lambda _command: next(values),
                builtin=lambda: (_ for _ in ()).throw(AssertionError("hook should win")),
                stop_after=16,
            )
            no_action(actions, state_dir_holder["marker"])
            self.assertEqual(alerts, [])
            self.assertNotIn("job may be stuck", "\n".join(logs))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _status, actions, alerts, logs = drive(
                root, gpu=lambda: True, defaults={"PROGRESS_CHECK": "tokens"},
                progress=lambda _command: 5.0,
                builtin=lambda: (_ for _ in ()).throw(AssertionError("hook should win")),
                stop_after=16,
            )
            no_action(actions, state_dir_holder["marker"])
            self.assertEqual(sum("job may be stuck" in item for item in alerts), 1)
            self.assertEqual(
                sum("job may be stuck" in line for line in logs), 1,
            )
            text = (state_dir_holder["marker"].parent / CONTROL.JOB_WATCH_NAME).read_text()
            self.assertIn("source=hook\n", text)
            self.assertIn("stuck=yes\n", text)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _status, actions, alerts, logs = drive(
                root, gpu=lambda: True, defaults={"PROGRESS_CHECK": "tokens"},
                progress=lambda _command: None,
                builtin=lambda: (0.0, {42: 100}),
                stop_after=16,
            )
            no_action(actions, state_dir_holder["marker"])
            self.assertEqual(
                sum("PROGRESS_CHECK failed" in line for line in logs), 1,
            )
            self.assertEqual(sum("via built-in" in item for item in alerts), 1)
            text = (state_dir_holder["marker"].parent / CONTROL.JOB_WATCH_NAME).read_text()
            self.assertIn("source=built-in\n", text)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _status, actions, alerts, _logs = drive(
                root, gpu=lambda: True, defaults={},
                progress=lambda _command: (_ for _ in ()).throw(AssertionError("no hook")),
                builtin=lambda: (0.0, {42: 100}),
                stop_after=16,
            )
            no_action(actions, state_dir_holder["marker"])
            self.assertEqual(sum("via built-in" in item for item in alerts), 1)

        calls = {"n": 0}

        def ending():
            calls["n"] += 1
            return calls["n"] < 3

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _status, actions, alerts, logs = drive(
                root, gpu=ending, defaults={},
                progress=lambda _command: None,
                builtin=lambda: (80.0, {42: calls["n"] * 10}),
                stop_after=12, stuck=600,
            )
            no_action(actions, state_dir_holder["marker"])
            self.assertEqual(alerts, [])
            finished = [line for line in logs if "job finished" in line]
            self.assertEqual(len(finished), 1)
            self.assertIn("hot for", finished[0])
            self.assertIn("peak 90.0C", finished[0])

        lines = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._healthy_tree(root)
            problems = CONTROL.collect_health_checks(
                root / "thermal", root / "hwmon",
                module_loaded=True, service_active="inactive",
                service_enabled="disabled", exec_status=None,
                watchdog_active="active", soc_celsius=85, log_text="",
                ready="not configured", emit=lines.append,
                job_watch={
                    "source": "hook", "stuck": "yes", "no_progress_seconds": "600",
                },
            )
            self.assertEqual(problems, [])
            self.assertIn("progress: hook (PROGRESS_CHECK)", lines)
            self.assertIn(
                "job: warn (job may be stuck: no progress 600s, via hook)", lines,
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code, _alerts, _notices = self._health(
                    root, {},
                    self._healthy_snapshot(job_watch={
                        "source": "built-in", "stuck": "yes",
                        "no_progress_seconds": "600",
                    }),
                    record=True,
                )
            self.assertEqual(code, 0)
            self.assertIn("via built-in", stdout.getvalue())
            self.assertFalse((root / "state" / CONTROL.INTERVENTION_NAME).exists())

        cool = {}
        walls = []
        for moment in (0.0, 700.0):
            CONTROL.observe_hot_job(
                now=moment, fan_rpms=[2700, 4050], hottest=70.0, busy=True,
                defaults={"PROGRESS_CHECK": "tokens"}, state_dir=Path("/nonexistent"),
                notifier=walls.append, alert_repeat=1800, zero_fan_celsius=85,
                stuck_seconds=600, state=cool, progress_reader=lambda _command: 1.0,
                builtin_reader=lambda: (_ for _ in ()).throw(AssertionError("cool")),
            )
        self.assertEqual(walls, [])
        self.assertFalse(cool.get("stuck"))

    def test_true_zero_powers_off_at_85c_with_no_work_in_progress(self):
        self.assertEqual(CONTROL.WATCHDOG_SOC_CELSIUS, 85.0)
        self.assertEqual(CONTROL.WATCHDOG_DEFAULT_ACTION, "systemctl poweroff")
        self.assertFalse(hasattr(CONTROL, "fans_stalled"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _status, actions, _alerts, logs, _marker = self._drive_watchdog(
                root, rpm=(0, 0), celsius=85, gpu=lambda: False, stop_after=10,
                soc=85, reboot_seconds=60,
            )
            self.assertEqual(actions, [CONTROL.WATCHDOG_DEFAULT_ACTION])
            self.assertIn(
                "not sampled now; no earlier work-state record", "\n".join(logs),
            )
            self.assertIn("85.0C", "\n".join(logs))
            self.assertNotIn(CONTROL.WATCHDOG_REBOOT_ACTION, actions)

    def test_true_zero_poweroff_does_not_sample_the_gpu(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._stopped_acpi_fans(root, (0, 0), 40.0)
            order = []

            def read_temp(_thermal):
                (root / "thermal" / "thermal_zone0" / "temp").write_text(
                    "91000\n", encoding="ascii",
                )
                value = 91.0
                order.append(("temp", value))
                return value

            def hanging_smi(*_args, **_kwargs):
                order.append("nvidia-smi")
                return None, ("timeout", "")

            def runner(action):
                order.append(("action", action))
                return 0

            slept = []
            with patch.object(CONTROL, "read_hottest_temperature_c", side_effect=read_temp), \
                 patch.object(CONTROL, "run_nvidia_smi", side_effect=hanging_smi), \
                 patch.object(CONTROL.time, "sleep", side_effect=slept.append), \
                 patch.object(CONTROL, "Event", return_value=Event()), \
                 patch.object(CONTROL.signal, "signal"), \
                 self.assertLogs(CONTROL.LOG, level="CRITICAL") as logs:
                status = CONTROL.run_watchdog(
                    root / "thermal", root / "hwmon",
                    poll_seconds=2, stall_seconds=15, reboot_seconds=60,
                    soc_celsius=85, monotonic=lambda: 0.0,
                    runner=runner, unit_state=lambda: "active",
                    notifier=lambda _message: None,
                    marker_path=root / "marker" / "true-zero-reboot",
                )
            self.assertEqual(status, 0)
            self.assertEqual(order[0][0], "temp")
            self.assertEqual(order[0][1], 91.0)
            self.assertEqual(order[1], ("action", CONTROL.WATCHDOG_DEFAULT_ACTION))
            self.assertNotIn("nvidia-smi", order)
            self.assertEqual(slept, [])
            text = "\n".join(logs.output)
            self.assertIn(
                "fan stall: hottest zone is 91.0C; running "
                "systemctl poweroff (not sampled now; no earlier work-state record)",
                text,
            )

            stamp = 1793949945.0
            shown = CONTROL.format_utc_stamp(stamp)
            state = root / "state"
            CONTROL.write_state_text(
                state / CONTROL.JOB_WATCH_NAME,
                "source=hook\ndetail=PROGRESS_CHECK\nstuck=no\n"
                "no_progress_seconds=0\n"
                f"recorded={stamp:.3f}\n",
            )
            order.clear()
            with patch.object(CONTROL, "read_hottest_temperature_c", side_effect=read_temp), \
                 patch.object(CONTROL, "run_nvidia_smi", side_effect=hanging_smi), \
                 patch.object(CONTROL, "Event", return_value=Event()), \
                 patch.object(CONTROL.signal, "signal"), \
                 self.assertLogs(CONTROL.LOG, level="CRITICAL") as logs:
                status = CONTROL.run_watchdog(
                    root / "thermal", root / "hwmon",
                    poll_seconds=2, stall_seconds=15, reboot_seconds=60,
                    soc_celsius=85, monotonic=lambda: 0.0,
                    runner=runner, unit_state=lambda: "active",
                    notifier=lambda _message: None,
                    marker_path=state / "true-zero-reboot",
                )
            self.assertEqual(status, 0)
            self.assertEqual(order[0], ("temp", 91.0))
            self.assertEqual(order[1], ("action", CONTROL.WATCHDOG_DEFAULT_ACTION))
            self.assertNotIn("nvidia-smi", order)
            text = "\n".join(logs.output)
            self.assertIn(
                "fan stall: hottest zone is 91.0C; running systemctl poweroff "
                f"(not sampled now; last recorded work state at {shown}: "
                "GPU job running, progress moving via PROGRESS_CHECK)",
                text,
            )

            gap = (
                "could not sample GPU: nvidia-smi exited 9; "
                "transient after a restart is normal"
            )
            CONTROL.write_state_text(
                state / CONTROL.JOB_WATCH_NAME,
                "source=unknown\n"
                f"detail={gap}\n"
                "stuck=no\nno_progress_seconds=0\n"
                f"recorded={stamp:.3f}\n",
            )
            order.clear()
            with patch.object(CONTROL, "read_hottest_temperature_c", side_effect=read_temp), \
                 patch.object(CONTROL, "run_nvidia_smi", side_effect=hanging_smi), \
                 patch.object(CONTROL, "Event", return_value=Event()), \
                 patch.object(CONTROL.signal, "signal"), \
                 self.assertLogs(CONTROL.LOG, level="CRITICAL") as logs:
                status = CONTROL.run_watchdog(
                    root / "thermal", root / "hwmon",
                    poll_seconds=2, soc_celsius=85, monotonic=lambda: 0.0,
                    runner=runner, unit_state=lambda: "active",
                    notifier=lambda _message: None,
                    marker_path=state / "true-zero-reboot",
                )
            self.assertEqual(status, 0)
            self.assertNotIn("nvidia-smi", order)
            self.assertIn(gap, "\n".join(logs.output))
            self.assertIn(f"last recorded work state at {shown}", "\n".join(logs.output))

            order.clear()
            with patch.object(CONTROL, "read_hottest_temperature_c", side_effect=read_temp), \
                 patch.object(CONTROL, "run_nvidia_smi", side_effect=hanging_smi), \
                 patch.object(CONTROL, "read_job_watch", side_effect=OSError("unreadable")), \
                 patch.object(CONTROL, "Event", return_value=Event()), \
                 patch.object(CONTROL.signal, "signal"), \
                 self.assertLogs(CONTROL.LOG, level="CRITICAL") as logs:
                status = CONTROL.run_watchdog(
                    root / "thermal", root / "hwmon",
                    poll_seconds=2, soc_celsius=85, monotonic=lambda: 0.0,
                    runner=runner, unit_state=lambda: "active",
                    notifier=lambda _message: None,
                    marker_path=state / "true-zero-reboot",
                )
            self.assertEqual(status, 0)
            self.assertEqual(order[1], ("action", CONTROL.WATCHDOG_DEFAULT_ACTION))
            self.assertNotIn("nvidia-smi", order)
            self.assertIn(
                "(not sampled now; no earlier work-state record)",
                "\n".join(logs.output),
            )
            self.assertEqual(
                CONTROL.format_progress({
                    "source": "off",
                    "detail": "no job running",
                    "recorded": f"{stamp:.3f}",
                }),
                f"off (no job running; recorded {shown})",
            )
            memory = {
                "recorded_at": stamp,
                "source": "hook",
                "detail": "PROGRESS_CHECK",
                "stuck": False,
            }
            with patch.object(
                CONTROL, "read_job_watch", side_effect=OSError("do not read"),
            ):
                note = CONTROL.last_recorded_work_note(memory, root / "missing")
            self.assertEqual(
                note,
                f"not sampled now; last recorded work state at {shown}: "
                "GPU job running, progress moving via PROGRESS_CHECK",
            )

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
                        marker=None, module=False, soc=85.0, reboot_seconds=60,
                        max_defer_seconds=None, defaults=None,
                        alert_repeat_seconds=1800, unit_state="active",
                        cooling_state=None, stuck_seconds=None,
                        progress_reader=None, builtin_reader=None):
        self._stopped_acpi_fans(root, rpm, celsius)
        if module:
            device = root / "thermal" / "cooling_device2"
            device.mkdir()
            (device / "type").write_text(
                CONTROL.COOLING_DEVICE_TYPE + "\n", encoding="ascii",
            )
            initial = 1 if cooling_state is None else cooling_state
            (device / "cur_state").write_text(f"{initial}\n", encoding="ascii")
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
                unit_state=lambda: unit_state,
                notifier=alerts.append,
                gpu_jobs=gpu,
                marker_path=marker,
                boot_id=boot,
                nonzero_clear_seconds=30,
                defaults={} if defaults is None else defaults,
                alert_repeat_seconds=alert_repeat_seconds,
                **({} if max_defer_seconds is None else {
                    "max_defer_seconds": max_defer_seconds,
                }),
                **({} if stuck_seconds is None else {"stuck_seconds": stuck_seconds}),
                **({} if progress_reader is None else {"progress_reader": progress_reader}),
                **({} if builtin_reader is None else {"builtin_reader": builtin_reader}),
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
                    defaults={},
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
            logged = "\n".join(logs)
            self.assertIn(
                "could not sample GPU: nvidia-smi exited unknown; "
                "transient after a restart is normal",
                logged,
            )
            self.assertIn("treating a GPU job as running", logged)
            self.assertIn("deferring reboot", logged)

    def _gpu_failure(self):
        with patch.object(CONTROL.subprocess, "run", side_effect=OSError("missing")), \
             patch.object(CONTROL.time, "sleep"):
            return CONTROL.gpu_job_running()

    def test_nvidia_smi_retries_then_a_good_sample_replaces_the_gap(self):
        calls = {"n": 0}

        def run(args, **_kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                return CONTROL.subprocess.CompletedProcess(args, 9, "", "not ready")
            return CONTROL.subprocess.CompletedProcess(args, 0, "", "")

        sleeps = []
        with patch.object(CONTROL.subprocess, "run", side_effect=run), \
             patch.object(CONTROL.time, "sleep", side_effect=sleeps.append):
            self.assertFalse(CONTROL.gpu_job_running())
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sleeps, [0.5, 1.0])
        self.assertIsNone(getattr(CONTROL.run_nvidia_smi, "last_failure", None))

        def always_fail(args, **_kwargs):
            return CONTROL.subprocess.CompletedProcess(
                args, 9, "", "NVML error\nmore",
            )

        with patch.object(CONTROL.subprocess, "run", side_effect=always_fail), \
             patch.object(CONTROL.time, "sleep"):
            self.assertIsNone(CONTROL.gpu_job_running())
        gap = CONTROL.gpu_sample_gap_detail()
        self.assertEqual(
            gap,
            "could not sample GPU: nvidia-smi exited 9; "
            "transient after a restart is normal; NVML error",
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._healthy_tree(root)
            state = root / "state"
            CONTROL.observe_hot_job(
                now=0.0, fan_rpms=[2700, 4050], hottest=40.0, busy=True,
                defaults={}, state_dir=state, notifier=lambda _message: None,
                alert_repeat=1800, zero_fan_celsius=85, stuck_seconds=600,
                state={}, busy_how="nvidia-smi-failed",
                builtin_reader=lambda: (None, None),
            )
            stored = (state / CONTROL.JOB_WATCH_NAME).read_text(encoding="ascii")
            self.assertIn("source=unknown\n", stored)
            self.assertIn(gap, stored)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code, _alerts, _notices = self._health(
                    root, {},
                    self._healthy_snapshot(job_watch=CONTROL.read_job_watch(state)),
                    record=False,
                )
            text = stdout.getvalue()
            self.assertEqual(code, 0, text)
            self.assertIn("health: healthy", text)
            self.assertIn(f"progress: unknown ({gap}; recorded ", text)
            self.assertIn(" UTC)", text)
            self.assertNotIn("NEEDS INTERVENTION", text)
            self.assertFalse((state / CONTROL.INTERVENTION_NAME).exists())

            CONTROL.clear_nvidia_smi_failure()
            CONTROL.observe_hot_job(
                now=2.0, fan_rpms=[2700, 4050], hottest=40.0, busy=False,
                defaults={}, state_dir=state, notifier=lambda _message: None,
                alert_repeat=1800, zero_fan_celsius=85, stuck_seconds=600,
                state={"busy": True}, busy_how="nvidia-smi",
            )
            replaced = (state / CONTROL.JOB_WATCH_NAME).read_text(encoding="ascii")
            self.assertIn("source=off\n", replaced)
            self.assertIn("detail=no job running\n", replaced)
            self.assertNotIn("could not sample GPU", replaced)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code, _alerts, _notices = self._health(
                    root, {},
                    self._healthy_snapshot(job_watch=CONTROL.read_job_watch(state)),
                    record=False,
                )
            text = stdout.getvalue()
            self.assertEqual(code, 0, text)
            self.assertIn("progress: off (no job running; recorded ", text)
            self.assertIn(" UTC)", text)
            self.assertNotIn("could not sample GPU", text)

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
                unit_state=lambda: ("inactive", "enabled", 1),
                notifier=alerts.append,
                alert_repeat_seconds=1800,
                gpu_jobs=lambda: False,
            )
        self.assertEqual(len(alerts), 2)
        warnings = [line for line in logs.output if "is not active" in line]
        self.assertGreaterEqual(len(warnings), 6)

    def test_foreign_floor_alerts_once_before_the_reboot_hold(self):
        clock = [0.0]
        stop = Event()
        alerts = []
        actions = []

        def wait(delay):
            clock[0] += 2
            if clock[0] > 10:
                stop.set()

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(CONTROL, "Event", return_value=stop), \
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
                marker_path=Path(directory) / "true-zero-reboot",
                boot_id="boot-a",
                defaults={},
            )
        self.assertEqual(status, 0)
        self.assertEqual(actions, [])
        self.assertEqual(len(alerts), 1)
        text = "\n".join(logs.output)
        self.assertGreater(text.count("a reboot clears the ACPI floor"), 1)
        self.assertNotIn("not rebooting again", text)

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

    def test_foreign_floor_reboots_once_then_needs_intervention(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = ("inactive", "enabled", CONTROL.FOREIGN_FLOOR_EXIT)
            _status, actions, _alerts, logs, marker = self._drive_watchdog(
                root, rpm=(2700, 4050), celsius=40, gpu=lambda: False,
                stop_after=80, unit_state=state,
            )
            self.assertEqual(actions, [CONTROL.WATCHDOG_REBOOT_ACTION])
            self.assertIn("reason=foreign-floor", marker.read_text(encoding="ascii"))
            self.assertIn("no GPU job", "\n".join(logs))
            self.assertEqual(
                (root / "thermal" / "cooling_device0" / "cur_state").read_text(),
                "40\n",
            )

            root2 = root / "next"
            root2.mkdir()
            _status, actions, _alerts, logs, _marker = self._drive_watchdog(
                root2, rpm=(2700, 4050), celsius=40, gpu=lambda: False,
                stop_after=80, boot="boot-b", marker=marker, unit_state=state,
            )
            self.assertEqual(actions, [])
            text = "\n".join(logs)
            self.assertIn("not rebooting again", text)
            self.assertNotIn("running systemctl poweroff", text)
            flag = marker.parent / CONTROL.INTERVENTION_NAME
            self.assertIn("not rebooting again", flag.read_text(encoding="ascii"))
            self.assertEqual(flag.stat().st_mode & 0o777, 0o644)

    def test_foreign_floor_defers_for_work_and_powers_off_when_hot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = ("inactive", "enabled", CONTROL.FOREIGN_FLOOR_EXIT)
            _status, actions, _alerts, logs, _marker = self._drive_watchdog(
                root, rpm=(2700, 4050), celsius=40, gpu=lambda: True,
                stop_after=80, unit_state=state,
            )
            self.assertEqual(actions, [])
            self.assertIn("deferring reboot", "\n".join(logs))

            root2 = root / "hot"
            root2.mkdir()
            _status, actions, _alerts, logs, _marker = self._drive_watchdog(
                root2, rpm=(2700, 4050), celsius=90, gpu=lambda: True,
                stop_after=20, unit_state=state,
            )
            self.assertEqual(actions, [])
            self.assertNotIn("running systemctl poweroff", "\n".join(logs))

    def test_busy_check_defers_reboots_and_treats_failure_as_busy(self):
        def refuse_nvidia():
            raise AssertionError("nvidia-smi must not run when BUSY_CHECK is set")

        def drive(result, stop_after=80, max_defer_seconds=None, defaults=None):
            seen = []

            def succeeded(command, timeout=CONTROL.CHECK_TIMEOUT_SECONDS):
                seen.append(command)
                return result

            with tempfile.TemporaryDirectory() as directory, \
                 patch.object(CONTROL, "command_succeeded", side_effect=succeeded):
                root = Path(directory)
                settings = {"BUSY_CHECK": "in-flight"}
                if defaults:
                    settings.update(defaults)
                status, actions, alerts, logs, _marker = self._drive_watchdog(
                    root, rpm=(0, 0), celsius=40, gpu=refuse_nvidia,
                    stop_after=stop_after, max_defer_seconds=max_defer_seconds,
                    defaults=settings,
                )
            return status, actions, alerts, logs, seen

        _status, actions, _alerts, logs, seen = drive(True)
        self.assertEqual(actions, [])
        self.assertIn("BUSY_CHECK reports work in progress", "\n".join(logs))
        self.assertEqual(seen[0], "in-flight")

        _status, actions, _alerts, logs, _seen = drive(False)
        self.assertEqual(actions, [CONTROL.WATCHDOG_REBOOT_ACTION])
        self.assertIn("no work in progress", "\n".join(logs))

        _status, actions, _alerts, logs, _seen = drive(None)
        self.assertEqual(actions, [])
        self.assertIn("BUSY_CHECK failed; treating work as in progress", "\n".join(logs))

        _status, actions, alerts, logs, _seen = drive(True, max_defer_seconds=10)
        self.assertEqual(actions, [])
        self.assertTrue(any("not rebooting through work" in item for item in alerts))
        self.assertIn("not rebooting through work", "\n".join(logs))

    def test_max_defer_seconds_in_defaults_overrides_the_flag(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(CONTROL, "command_succeeded", return_value=True):
            root = Path(directory)
            _status, actions, alerts, logs, _marker = self._drive_watchdog(
                root, rpm=(0, 0), celsius=40, gpu=lambda: False, stop_after=80,
                defaults={"BUSY_CHECK": "in-flight", "MAX_DEFER_SECONDS": "10"},
            )
        self.assertEqual(actions, [])
        self.assertTrue(any("deferral limit of 10s" in item for item in alerts))
        self.assertIn("not rebooting through work", "\n".join(logs))

    def _healthy_tree(self, root: Path) -> None:
        self._stopped_acpi_fans(root, (2700, 4050), 40.0)
        device = root / "thermal" / "cooling_device2"
        device.mkdir()
        (device / "type").write_text(CONTROL.COOLING_DEVICE_TYPE + "\n", encoding="ascii")
        (device / "cur_state").write_text("0\n", encoding="ascii")
        (device / "max_state").write_text("12\n", encoding="ascii")

    def _health(self, root: Path, defaults, snapshot, *, settle=0.0, boot="boot-b",
                clock=None, sleeper=None, record=True):
        alerts = []
        notices = []
        if clock is None:
            monotonic = lambda: 0.0
        else:
            monotonic = lambda: clock[0]
        code = CONTROL.run_health(
            root / "thermal",
            root / "hwmon",
            settle_seconds=settle,
            poll_seconds=2,
            state_dir=root / "state",
            defaults=defaults,
            monotonic=monotonic,
            wall_time=lambda: 5.0,
            sleeper=sleeper or (lambda delay: None),
            observer=lambda: snapshot,
            notifier=alerts.append,
            boot_id=boot,
            notify=lambda _defaults, summary: notices.append(summary),
            record=record,
        )
        return code, alerts, notices

    def _healthy_snapshot(self, **overrides):
        snapshot = {
            "module_loaded": True,
            "service_active": "inactive",
            "service_enabled": "disabled",
            "exec_status": None,
            "watchdog_active": "active",
            "log_text": "",
        }
        snapshot.update(overrides)
        return snapshot

    def test_ready_check_pass_fail_and_unset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._healthy_tree(root)
            snapshot = self._healthy_snapshot()
            with patch.object(CONTROL, "command_succeeded", side_effect=AssertionError("unset")):
                code, _alerts, notices = self._health(root, {}, snapshot)
            self.assertEqual(code, 0)
            self.assertEqual(notices, [])
            health = (root / "state" / CONTROL.HEALTH_NAME).read_text(encoding="ascii")
            self.assertIn("result=healthy", health)
            self.assertIn("ready=not configured", health)
            self.assertFalse((root / "state" / CONTROL.INTERVENTION_NAME).exists())

            with patch.object(CONTROL, "command_succeeded", return_value=True):
                code, _alerts, notices = self._health(
                    root, {"READY_CHECK": "probe"}, snapshot,
                )
            self.assertEqual(code, 0)
            self.assertIn(
                "ready=pass",
                (root / "state" / CONTROL.HEALTH_NAME).read_text(encoding="ascii"),
            )
            self.assertEqual(notices, [])

            CONTROL.write_stall_marker(
                root / "state" / CONTROL.STALL_MARKER_NAME, "boot-a", 1.0,
            )
            CONTROL.write_intervention(
                root / "state" / CONTROL.INTERVENTION_NAME, "old", "boot-a", 1.0,
            )
            with patch.object(CONTROL, "command_succeeded", return_value=False):
                code, _alerts, notices = self._health(
                    root, {"READY_CHECK": "probe", "DGX_FAN_NOTIFY": "hook"}, snapshot,
                )
            self.assertEqual(code, 1)
            flag = (root / "state" / CONTROL.INTERVENTION_NAME).read_text(encoding="ascii")
            self.assertIn("reason=workload not ready\n", flag)
            self.assertIn(
                "ready=fail",
                (root / "state" / CONTROL.HEALTH_NAME).read_text(encoding="ascii"),
            )
            self.assertTrue((root / "state" / CONTROL.STALL_MARKER_NAME).exists())
            self.assertEqual(notices, ["dgx-fan-control: needs intervention: workload not ready\n"])

    def test_ready_check_is_retried_and_a_url_is_a_get(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._healthy_tree(root)
            calls = {"n": 0}

            def succeeded(command, timeout=CONTROL.CHECK_TIMEOUT_SECONDS):
                calls["n"] += 1
                return calls["n"] >= 2

            clock = [0.0]

            def sleep(delay):
                clock[0] += delay

            with patch.object(CONTROL, "command_succeeded", side_effect=succeeded):
                code, _alerts, _notices = self._health(
                    root, {"READY_CHECK": "probe"}, self._healthy_snapshot(),
                    settle=10, clock=clock, sleeper=sleep,
                )
            self.assertEqual(code, 0)
            self.assertEqual(calls["n"], 2)

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with patch.object(CONTROL.urllib.request, "urlopen", return_value=Response()) as opened:
            self.assertEqual(
                CONTROL.ready_for_work({"READY_CHECK": "https://example.invalid/ready"}),
                "pass",
            )
        opened.assert_called_once()
        with patch.object(
            CONTROL.urllib.request, "urlopen",
            side_effect=CONTROL.urllib.error.URLError("down"),
        ):
            self.assertEqual(
                CONTROL.ready_for_work({"READY_CHECK": "https://example.invalid/ready"}),
                "fail",
            )

    def test_health_pass_clears_markers_and_announces_recovery_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._healthy_tree(root)
            state = root / "state"
            CONTROL.write_stall_marker(
                state / CONTROL.STALL_MARKER_NAME, "boot-a", 1.0, reason="foreign-floor",
            )
            CONTROL.write_intervention(
                state / CONTROL.INTERVENTION_NAME, "old", "boot-a", 1.0,
            )
            code, alerts, notices = self._health(root, {}, self._healthy_snapshot())
            self.assertEqual(code, 0)
            self.assertEqual(alerts, ["dgx-fan-control: recovered after watchdog reboot"])
            self.assertEqual(notices, ["dgx-fan-control: recovered after watchdog reboot\n"])
            self.assertFalse((state / CONTROL.STALL_MARKER_NAME).exists())
            self.assertFalse((state / CONTROL.INTERVENTION_NAME).exists())
            code, alerts, notices = self._health(root, {}, self._healthy_snapshot())
            self.assertEqual(code, 0)
            self.assertEqual(alerts, [])
            self.assertEqual(notices, [])

    def test_health_fail_keeps_the_reboot_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._healthy_tree(root)
            state = root / "state"
            CONTROL.write_stall_marker(state / CONTROL.STALL_MARKER_NAME, "boot-a", 1.0)
            code, alerts, notices = self._health(
                root, {}, self._healthy_snapshot(module_loaded=False, boot_load="absent"),
            )
            self.assertEqual(code, 1)
            self.assertEqual(alerts, [])
            text = (state / CONTROL.INTERVENTION_NAME).read_text(encoding="ascii")
            self.assertIn(
                "not loaded; boot load is disabled. Run sudo dgx-fan-control module enable",
                text,
            )
            self.assertTrue((state / CONTROL.STALL_MARKER_NAME).exists())
            self.assertTrue(notices and "needs intervention" in notices[0])

    def test_health_clear_removes_the_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            flag = state / CONTROL.INTERVENTION_NAME
            CONTROL.write_intervention(flag, "workload not ready", "boot-b", 1.0)
            with patch.object(CONTROL, "STALL_MARKER_DIR", state), \
                 patch.object(CONTROL.os, "geteuid", return_value=1000):
                code, stdout, stderr = run_cli(["health", "--clear"])
            self.assertEqual(code, 1)
            self.assertIn("health --clear requires root", stdout)
            self.assertNotIn("Traceback", stdout + stderr)
            self.assertNotIn("Permission denied", stdout + stderr)
            self.assertTrue(flag.exists())
            with patch.object(CONTROL, "STALL_MARKER_DIR", state), \
                 patch.object(CONTROL.os, "geteuid", return_value=0):
                code, stdout, _stderr = run_cli(["health", "--clear"])
            self.assertEqual(code, 0)
            self.assertIn("cleared the needs-intervention flag", stdout)
            self.assertFalse(flag.exists())

    def test_interactive_health_is_one_pass_and_does_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._healthy_tree(root)
            state = root / "state"
            CONTROL.write_stall_marker(state / CONTROL.STALL_MARKER_NAME, "boot-a", 1.0)
            CONTROL.write_intervention(
                state / CONTROL.INTERVENTION_NAME, "old reason", "boot-a", 1.0,
            )

            def refuse_wait(delay):
                raise AssertionError(f"interactive health slept {delay}")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code, alerts, notices = self._health(
                    root, {}, self._healthy_snapshot(module_loaded=False, boot_load="absent"),
                    sleeper=refuse_wait, record=False,
                )
            text = stdout.getvalue()
            self.assertEqual(code, 1)
            self.assertEqual(alerts, [])
            self.assertEqual(notices, [])
            self.assertIn(
                "module: fail (not loaded; boot load is disabled. Run sudo dgx-fan-control module enable)",
                text,
            )
            self.assertLess(text.index("module:"), text.index("health: fail"))
            self.assertIn("recorded: no", text)
            self.assertIn("stored needs-intervention: old reason", text)
            self.assertTrue((state / CONTROL.STALL_MARKER_NAME).exists())
            self.assertIn("old reason", (state / CONTROL.INTERVENTION_NAME).read_text())

    def test_unreadable_sysfs_is_unknown_needs_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._healthy_tree(root)
            thermal = root / "thermal"
            hwmon = root / "hwmon"
            thermal.chmod(0)
            hwmon.chmod(0)
            try:
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code, _alerts, _notices = self._health(
                        root, {}, self._healthy_snapshot(), record=False,
                    )
            finally:
                thermal.chmod(0o755)
                hwmon.chmod(0o755)
            text = stdout.getvalue()
            self.assertEqual(code, 1)
            self.assertIn("cooling-device: unknown (needs root)", text)
            self.assertIn("fans: unknown (needs root)", text)
            self.assertIn("temperature: unknown (needs root)", text)
            self.assertNotIn("Permission denied", text)
            self.assertIn("recorded: no", text)

    def test_settle_logs_each_failed_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._healthy_tree(root)
            calls = {"n": 0}

            def observer():
                calls["n"] += 1
                if calls["n"] == 1:
                    return self._healthy_snapshot(module_loaded=False, boot_load="absent")
                return self._healthy_snapshot()

            clock = [0.0]

            def sleep(delay):
                clock[0] += delay

            with self.assertLogs(CONTROL.LOG, level="INFO") as logs:
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code = CONTROL.run_health(
                        root / "thermal", root / "hwmon",
                        settle_seconds=10, poll_seconds=2,
                        state_dir=root / "state", defaults={},
                        monotonic=lambda: clock[0], wall_time=lambda: 1.0,
                        sleeper=sleep, observer=observer, boot_id="boot-b",
                        notifier=lambda message: None,
                        notify=lambda _defaults, summary: None,
                        record=False,
                    )
            self.assertEqual(code, 0)
            self.assertEqual(calls["n"], 2)
            self.assertEqual(clock[0], 2.0)
            text = "\n".join(logs.output)
            self.assertIn("health attempt 1:", text)
            self.assertIn(
                "module: fail (not loaded; boot load is disabled. Run sudo dgx-fan-control module enable)",
                text,
            )
            self.assertIn("health: healthy", stdout.getvalue())
            self.assertFalse((root / "state").exists())

    def test_external_checks_time_out_instead_of_hanging(self):
        def timed_out(*_args, **_kwargs):
            raise CONTROL.subprocess.TimeoutExpired("cmd", CONTROL.CHECK_TIMEOUT_SECONDS)

        with patch.object(CONTROL.subprocess, "run", side_effect=timed_out), \
             patch.object(CONTROL.time, "sleep"):
            self.assertEqual(CONTROL.systemctl_query("is-active"), "timeout")
            self.assertEqual(CONTROL.read_kernel_log(), "")
            self.assertIsNone(CONTROL.gpu_job_running())
            self.assertEqual(CONTROL.command_succeeded("in-flight"), "timeout")
        with patch.object(CONTROL, "command_succeeded", return_value="timeout"):
            self.assertEqual(
                CONTROL.ready_for_work({"READY_CHECK": "in-flight"}), "timeout",
            )
            busy, how = CONTROL.work_is_in_progress(
                {"BUSY_CHECK": "in-flight"},
                lambda: (_ for _ in ()).throw(AssertionError("smi")),
            )
            self.assertTrue(busy)
            self.assertEqual(how, "busy-check-failed")

    def test_status_reports_unreadable_sysfs_without_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            thermal = root / "thermal"
            hwmon = root / "hwmon"
            thermal.mkdir()
            hwmon.mkdir()
            thermal.chmod(0)
            try:
                code, stdout, stderr = run_cli([
                    "--thermal-root", str(thermal),
                    "--hwmon-root", str(hwmon),
                    "status",
                ])
            finally:
                thermal.chmod(0o755)
            combined = stdout + stderr
            self.assertEqual(code, 0)
            self.assertIn("unknown (needs root)", stdout)
            self.assertNotIn("Permission denied", combined)
            self.assertNotIn("Traceback", combined)

    def test_record_flag_requires_root_and_still_prints(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._healthy_tree(root)
            state = root / "absent-state"
            with patch.object(CONTROL, "STALL_MARKER_DIR", state), \
                 patch.object(CONTROL.os, "geteuid", return_value=1000), \
                 patch.object(
                     CONTROL, "read_daemon_facts",
                     return_value=("inactive", "disabled", None, None, None),
                 ), \
                 patch.object(CONTROL, "watchdog_unit_state", return_value=("active", "enabled")), \
                 patch.object(CONTROL, "_module_loaded_now", return_value=True):
                code, stdout, stderr = run_cli([
                    "--thermal-root", str(root / "thermal"),
                    "--hwmon-root", str(root / "hwmon"),
                    "health", "--record",
                ])
            combined = stdout + stderr
            self.assertEqual(code, 1)
            self.assertIn("health --record requires root", stdout)
            self.assertIn("health: healthy", stdout)
            self.assertIn("module: pass", stdout)
            self.assertIn("recorded: no", stdout)
            self.assertNotIn("Traceback", combined)
            self.assertNotIn("Permission denied", combined)
            self.assertFalse(state.exists())

    def test_plain_health_as_root_does_not_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._healthy_tree(root)
            state = root / "state"
            health = state / CONTROL.HEALTH_NAME
            old = (
                "result=needs-intervention\nready=not configured\n"
                "boot_id=boot-old\ntime=not-a-time\n"
                "reason=workload not ready\n"
            )
            CONTROL.write_state_text(health, old)

            def run(argv):
                with patch.object(CONTROL, "STALL_MARKER_DIR", state), \
                     patch.object(CONTROL.os, "geteuid", return_value=0), \
                     patch.object(CONTROL, "read_boot_id", return_value="boot-now"), \
                     patch.object(CONTROL, "read_kernel_log", return_value=""), \
                     patch.object(
                         CONTROL, "read_daemon_facts",
                         return_value=("inactive", "disabled", None, None, None),
                     ), \
                     patch.object(
                         CONTROL, "watchdog_unit_state",
                         return_value=("active", "enabled"),
                     ), \
                     patch.object(CONTROL, "_module_loaded_now", return_value=True):
                    return run_cli([
                        "--thermal-root", str(root / "thermal"),
                        "--hwmon-root", str(root / "hwmon"),
                        *argv,
                    ])

            code, stdout, stderr = run(["health"])
            self.assertEqual(code, 0, stdout + stderr)
            self.assertIn("health: healthy", stdout)
            self.assertIn("recorded: no", stdout)
            self.assertNotIn("recorded: yes", stdout)
            self.assertEqual(health.read_text(encoding="ascii"), old)

            code, stdout, stderr = run(["health", "--record"])
            self.assertEqual(code, 0, stdout + stderr)
            self.assertIn("recorded: yes", stdout)
            self.assertIn("result=healthy", health.read_text(encoding="ascii"))

    def test_owned_floor_match_mismatch_and_missing_record(self):
        self.assertEqual(CONTROL.floor_matches_last_set(13500, (12, 13500)), "ours")
        self.assertEqual(
            CONTROL.floor_report(13500, (12, 13500)),
            "ours (matches last-set 12)",
        )
        self.assertEqual(CONTROL.floor_matches_last_set(0x10FE, (12, 13500)), "foreign")
        foreign = CONTROL.floor_report(0x10FE, (12, 13500))
        self.assertEqual(foreign, "foreign (live 0x10fe, last-set 0x34bc)")
        self.assertEqual(CONTROL.floor_matches_last_set(0x10FE, None), "foreign")
        self.assertEqual(
            CONTROL.floor_report(0x10FE, None),
            "foreign (live 0x10fe, last-set none)",
        )
        refused = "refusing to replace existing lower floor 0x34bc (13500 RPM)\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            thermal = root / "thermal"
            hwmon = root / "hwmon"
            thermal.mkdir()
            hwmon.mkdir()
            with self.assertRaises(CONTROL.ForeignFloor):
                CONTROL.require_control(
                    thermal, hwmon, module_loaded=True, log_text=refused, record=None,
                )
            with self.assertRaises(CONTROL.ForeignFloor):
                CONTROL.require_control(
                    thermal, hwmon, module_loaded=True, log_text=refused,
                    record=(6, 7200),
                )
            with self.assertRaises(RuntimeError) as caught:
                CONTROL.require_control(
                    thermal, hwmon, module_loaded=True, log_text=refused,
                    record=(12, 13500),
                )
            self.assertNotIsInstance(caught.exception, CONTROL.ForeignFloor)
            self.assertIn("last-set 12", str(caught.exception))
            self.assertNotIn("probe refused a foreign lower floor", str(caught.exception))

    def test_module_disable_is_healthy_and_enabled_but_unloaded_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._healthy_tree(root)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code, _alerts, _notices = self._health(
                    root, {},
                    self._healthy_snapshot(module_loaded=False, boot_load="managed-off"),
                    record=False,
                )
            text = stdout.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("module: off (disabled by module disable)", text)
            self.assertIn("cooling-device: skipped (module disabled by module disable)", text)
            self.assertIn("daemon: stopped (manual)", text)
            self.assertIn("health: healthy", text)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code, _alerts, _notices = self._health(
                    root, {},
                    self._healthy_snapshot(module_loaded=False, boot_load="enabled"),
                    record=False,
                )
            self.assertEqual(code, 1)
            self.assertIn(
                "module: fail (enabled but not loaded: check dmesg | grep dgx_ec)",
                stdout.getvalue(),
            )

    def test_set_state_records_the_floor_and_automatic_clears_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._healthy_tree(root)
            etc = root / "etc"
            calls = []
            with patch.object(CONTROL, "enable_manual_oneshot", side_effect=lambda: calls.append("enable")), \
                 patch.object(CONTROL, "disable_manual_oneshot", side_effect=lambda: calls.append("disable")):
                CONTROL.remember_manual_floor(
                    6, "inactive", root / "state",
                    profile="EC 3.5.8", etc=etc,
                )
            text = (root / "state" / CONTROL.MANUAL_STATE_NAME).read_text(encoding="ascii")
            self.assertIn("state=6\n", text)
            self.assertIn("floor=7200\n", text)
            option = etc / "modprobe.d" / CONTROL.OWNED_MODPROBE_NAME
            self.assertIn("owned_state=6 owned_floor=7200", option.read_text(encoding="ascii"))
            self.assertEqual(calls, ["enable"])
            acpi = root / "thermal" / "cooling_device0" / "cur_state"
            self.assertEqual(acpi.read_text(encoding="ascii"), "40\n")
            with patch.object(CONTROL, "disable_manual_oneshot"):
                CONTROL.clear_persisted_manual_floor(root / "state", {}, etc)
            self.assertFalse((root / "state" / CONTROL.MANUAL_STATE_NAME).exists())
            self.assertFalse(option.exists())

            device = root / "thermal" / "cooling_device2"
            CONTROL.write_last_manual_state(root / "state", 6, 7200, "EC 3.5.8")
            (device / "cur_state").write_text("1\n", encoding="ascii")
            code = CONTROL.apply_manual_floor(
                root / "thermal", root / "hwmon",
                defaults={"MANUAL_STATE": "4"},
                state_dir=root / "state",
                wait_seconds=0,
            )
            self.assertEqual(code, 0)
            self.assertEqual((device / "cur_state").read_text(encoding="ascii"), "4\n")
            self.assertEqual(acpi.read_text(encoding="ascii"), "40\n")
            self.assertEqual(
                CONTROL.apply_manual_floor(
                    root / "thermal", root / "hwmon",
                    defaults={"MANUAL_STATE": "0"},
                    state_dir=root / "state",
                    wait_seconds=0,
                ),
                1,
            )
            self.assertEqual((device / "cur_state").read_text(encoding="ascii"), "4\n")

    def test_true_zero_still_runs_in_every_mode_and_state_zero_does_not(self):
        manual = ("inactive", "enabled", 0, "success", 0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _status, actions, alerts, logs, _marker = self._drive_watchdog(
                root, rpm=(0, 0), celsius=40, gpu=lambda: False, stop_after=80,
                module=False,
            )
            self.assertEqual(actions, [CONTROL.WATCHDOG_REBOOT_ACTION])
            self.assertIn("wrote ACPI Fan level 100", "\n".join(logs))
            self.assertEqual(
                (root / "thermal" / "cooling_device0" / "cur_state").read_text(),
                "100\n",
            )

            root = Path(directory) / "stopped"
            root.mkdir()
            _status, actions, alerts, logs, _marker = self._drive_watchdog(
                root, rpm=(0, 0), celsius=40, gpu=lambda: False, stop_after=80,
                module=False, unit_state=manual,
            )
            self.assertEqual(actions, [CONTROL.WATCHDOG_REBOOT_ACTION])
            self.assertTrue(any("fans are stopped" in item for item in alerts))
            self.assertFalse(any("not active" in item for item in alerts))
            self.assertIn("stopped (manual)", "\n".join(logs))

            root = Path(directory) / "held"
            root.mkdir()
            held_marker = root / "marker"
            held_marker.mkdir()
            CONTROL.write_last_manual_state(held_marker, 12, 13500, "EC 3.5.8")
            _status, actions, alerts, _logs, marker = self._drive_watchdog(
                root, rpm=(0, 0), celsius=40, gpu=lambda: False, stop_after=80,
                module=True, cooling_state=12, marker=held_marker / "true-zero-reboot",
            )
            self.assertTrue((held_marker / CONTROL.MANUAL_STATE_NAME).exists())
            self.assertEqual(actions, [CONTROL.WATCHDOG_REBOOT_ACTION])
            self.assertEqual(
                (root / "thermal" / "cooling_device2" / "cur_state").read_text(),
                "12\n",
            )
            self.assertEqual(
                (root / "thermal" / "cooling_device0" / "cur_state").read_text(),
                "40\n",
            )

            root = Path(directory) / "spinning-zero"
            root.mkdir()
            _status, actions, alerts, _logs, _marker = self._drive_watchdog(
                root, rpm=(2700, 4050), celsius=40, gpu=lambda: False, stop_after=80,
                module=True, cooling_state=0,
            )
            self.assertEqual(actions, [])
            self.assertEqual(alerts, [])
            self.assertEqual(
                (root / "thermal" / "cooling_device2" / "cur_state").read_text(),
                "0\n",
            )

            root = Path(directory) / "automatic"
            root.mkdir()
            _status, actions, alerts, _logs, marker = self._drive_watchdog(
                root, rpm=(2700, 4050), celsius=40, gpu=lambda: False, stop_after=80,
                module=True, cooling_state=0,
            )
            self.assertFalse((marker.parent / CONTROL.MANUAL_STATE_NAME).exists())
            self.assertEqual(actions, [])
            self.assertEqual(alerts, [])

            root = Path(directory) / "stopped-fans-state-zero"
            root.mkdir()
            _status, actions, _alerts, _logs, _marker = self._drive_watchdog(
                root, rpm=(0, 0), celsius=40, gpu=lambda: False, stop_after=80,
                module=True, cooling_state=0,
            )
            self.assertEqual(actions, [CONTROL.WATCHDOG_REBOOT_ACTION])

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

    def test_two_acpi_fan_devices_are_the_two_tachometers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._healthy_tree(root)
            first = root / "hwmon" / "hwmon0"
            (first / "fan2_input").unlink()
            (first / "fan1_input").write_text("2066\n", encoding="ascii")
            second = root / "hwmon" / "hwmon1"
            second.mkdir()
            (second / "name").write_text("acpi_fan\n", encoding="ascii")
            (second / "fan1_input").write_text("2152\n", encoding="ascii")

            self.assertEqual(
                CONTROL.read_named_fan_rpms(root / "hwmon"), [2066, 2152],
            )
            status, detail = CONTROL._fan_status(root / "hwmon", root / "thermal")
            self.assertEqual(status, "pass", detail)
            self.assertIn("hwmon0:fan1=2066", detail)
            self.assertIn("hwmon1:fan1=2152", detail)
            self.assertNotIn("fan2=", detail)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code, _alerts, _notices = self._health(
                    root, {}, self._healthy_snapshot(), record=False,
                )
            text = stdout.getvalue()
            self.assertEqual(code, 0, text)
            self.assertIn(
                "fans: pass (hwmon0:fan1=2066 hwmon1:fan1=2152 RPM", text,
            )

            (second / "fan1_input").write_text("0\n", encoding="ascii")
            status, detail = CONTROL._fan_status(root / "hwmon", root / "thermal")
            self.assertEqual(status, "fail", detail)
            self.assertIn("hwmon1:fan1=0", detail)

            (second / "fan1_input").unlink()
            status, detail = CONTROL._fan_status(root / "hwmon", root / "thermal")
            self.assertEqual(status, "fail", detail)
            self.assertIn("hwmon0:fan1=2066", detail)
            self.assertNotIn("hwmon1:fan1", detail)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._stopped_acpi_fans(root, (2066,), 40.0)
            second = root / "hwmon" / "hwmon1"
            second.mkdir()
            (second / "name").write_text("acpi_fan\n", encoding="ascii")
            (second / "fan1_input").write_text("2152\n", encoding="ascii")
            clock = [0.0]
            stop = Event()
            alerts = []
            actions = []

            def wait(delay):
                clock[0] += delay
                if clock[0] > 4:
                    stop.set()

            with patch.object(CONTROL, "Event", return_value=stop), \
                 patch.object(CONTROL.signal, "signal"), \
                 patch.object(CONTROL, "module_is_loaded", return_value=False), \
                 patch.object(stop, "wait", side_effect=wait), \
                 self.assertLogs(CONTROL.LOG, level="INFO") as logs:
                status = CONTROL.run_watchdog(
                    root / "thermal", root / "hwmon",
                    poll_seconds=2, stall_seconds=15,
                    monotonic=lambda: clock[0],
                    runner=lambda action: actions.append(action),
                    unit_state=lambda: "active",
                    notifier=alerts.append,
                )
            self.assertEqual(status, 0)
            self.assertEqual(alerts, [])
            self.assertEqual(actions, [])
            self.assertFalse(any("will not trip" in line for line in logs.output))

            (root / "hwmon" / "hwmon0" / "fan1_input").write_text(
                "0\n", encoding="ascii",
            )
            (second / "fan1_input").write_text("0\n", encoding="ascii")
            clock[0] = 0.0
            stop.clear()

            def wait_stall(delay):
                clock[0] += delay
                if clock[0] > 30:
                    stop.set()

            with patch.object(CONTROL, "Event", return_value=stop), \
                 patch.object(CONTROL.signal, "signal"), \
                 patch.object(CONTROL, "module_is_loaded", return_value=False), \
                 patch.object(stop, "wait", side_effect=wait_stall), \
                 self.assertLogs(CONTROL.LOG, level="INFO") as logs:
                status = CONTROL.run_watchdog(
                    root / "thermal", root / "hwmon",
                    poll_seconds=2, stall_seconds=15,
                    monotonic=lambda: clock[0],
                    runner=lambda action: actions.append(action),
                    unit_state=lambda: "active",
                    notifier=alerts.append,
                )
            self.assertEqual(status, 0)
            self.assertTrue(alerts)
            self.assertIn("fans are stopped", alerts[0])
            self.assertFalse(any("will not trip" in line for line in logs.output))
            self.assertTrue(any("wrote ACPI Fan level 100" in line for line in logs.output))

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

    def test_acpi_level_refuses_below_floor_and_fail_hot_rewrites_one_hundred(self):
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

    def test_acpi_level_floor_is_thirty_five(self):
        self.assertEqual(CONTROL.ACPI_MIN_LEVEL, 35)
        with tempfile.TemporaryDirectory() as directory:
            thermal, first, second = self._acpi_tree(Path(directory))
            code, stdout, stderr = run_cli([
                "--thermal-root", str(thermal), "acpi-max", "--level", "34",
            ])
            self.assertEqual(code, 1)
            self.assertEqual((stdout + stderr).count("refusing ACPI fan level 34"), 1)
            self.assertIn("Level 35 is the lowest level measured to hold", stderr)
            self.assertEqual((first / "cur_state").read_text(), "40\n")
            self.assertEqual((second / "cur_state").read_text(), "40\n")
            code, stdout, stderr = run_cli([
                "--thermal-root", str(thermal), "acpi-max", "--level", "35",
            ])
            self.assertEqual(code, 0)
            self.assertEqual((first / "cur_state").read_text(), "35\n")
            self.assertEqual((second / "cur_state").read_text(), "35\n")

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
                stdout.startswith("state=0/12 (daemon not setting a floor; EC's own curve) hottest=50.0C\n")
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

    def test_telemetry_only_firmware_is_healthy_and_keeps_last_set(self):
        log = (
            "pinned EC 3.13.7 capabilities fan0=975..4350 fan1=975..4350\n"
            "EC 3.13.7 floor writes disabled; registering hwmon telemetry only, "
            "no cooling device\n"
        )

        def tree(root: Path) -> None:
            self._stopped_acpi_fans(root, (2073, 2154), 40.0)
            hwmon = root / "hwmon" / "hwmon1"
            hwmon.mkdir()
            (hwmon / "name").write_text(
                CONTROL.HWMON_DEVICE_NAME + "\n", encoding="ascii",
            )

        def snapshot(**overrides):
            base = self._healthy_snapshot(
                module_loaded=True,
                service_active="inactive",
                service_enabled="disabled",
                exec_status=0,
                service_result="success",
                log_text=log,
                ec_updates=("pass", "not blocked (EC 3.13.7)"),
            )
            base.update(overrides)
            return base

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tree(root)
            state = root / "state"
            CONTROL.write_intervention(
                state / CONTROL.INTERVENTION_NAME,
                "unsupported firmware; telemetry only",
                "boot-a", 1.0,
            )
            CONTROL.write_last_manual_state(
                state, 6, floor=2700, profile="EC 3.5.8",
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code, _alerts, _notices = self._health(
                    root, {},
                    snapshot(
                        service_active="inactive",
                        service_enabled="disabled",
                        exec_status=0,
                        service_result="success",
                    ),
                    record=True,
                )
            text = stdout.getvalue()
            self.assertEqual(code, 0, text)
            self.assertIn("health: healthy", text)
            self.assertIn(
                "firmware: control unsupported (EC 3.13.7, telemetry only)",
                text,
            )
            self.assertIn(
                "cooling-device: n/a (EC 3.13.7: telemetry only, control unsupported)",
                text,
            )
            self.assertIn(f"clock-cap: {CONTROL.CLOCK_CAP_HINT}", text)
            self.assertIn("MiaAI-Lab sparkLimiter", CONTROL.CLOCK_CAP_HINT)
            self.assertIn("daemon: n/a/stopped (unsupported firmware)", text)
            self.assertIn("mode: n/a (last-set 6 kept)", text)
            self.assertIn("ready: n/a", text)
            self.assertIn("fans: pass", text)
            self.assertIn("temperature: pass", text)
            self.assertIn("watchdog: pass", text)
            self.assertFalse((state / CONTROL.INTERVENTION_NAME).exists())
            self.assertIn("state=6", (state / CONTROL.MANUAL_STATE_NAME).read_text())
            stored = (state / CONTROL.HEALTH_NAME).read_text(encoding="ascii")
            self.assertIn("result=healthy", stored)
            self.assertIn("ready=n/a\n", stored)
            self.assertNotIn("ready=not configured", stored)

            with patch.object(CONTROL, "command_succeeded", return_value=True):
                code, _alerts, _notices = self._health(
                    root, {"READY_CHECK": "probe"}, snapshot(), record=True,
                )
            stored = (state / CONTROL.HEALTH_NAME).read_text(encoding="ascii")
            self.assertEqual(code, 0)
            self.assertIn("ready=n/a\n", stored)
            self.assertNotIn("ready=pass", stored)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tree(root)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code, _alerts, _notices = self._health(
                    root, {},
                    snapshot(
                        service_active="inactive",
                        service_enabled="enabled",
                        exec_status=CONTROL.UNSUPPORTED_FIRMWARE_EXIT,
                        service_result="exit-code",
                        log_text="",
                        ec_updates=(
                            "warn",
                            "not blocked; EC 3.5.8 can be replaced by 3.13.7",
                        ),
                    ),
                    record=True,
                )
            text = stdout.getvalue()
            self.assertEqual(code, 0, text)
            self.assertIn("health: healthy", text)
            self.assertIn(
                "daemon: n/a (unsupported firmware; service enabled but idle)",
                text,
            )
            self.assertIn("mode: n/a", text)
            self.assertIn("ec-updates: warn", text)
            self.assertIn("stored needs-intervention: none", text)
            self.assertNotIn("NEEDS INTERVENTION:", text)
            self.assertFalse((root / "state" / CONTROL.INTERVENTION_NAME).exists())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._stopped_acpi_fans(root, (2700, 4050), 40.0)
            lines = []
            problems = CONTROL.collect_health_checks(
                root / "thermal", root / "hwmon",
                module_loaded=True, service_active="inactive",
                service_enabled="disabled", exec_status=None,
                watchdog_active="active", soc_celsius=85,
                log_text="pinned EC 3.5.8 capabilities fan0=1260..9000 fan1=1890..13500\n",
                ready="not configured", emit=lines.append,
                ec_block=("note", "unknown (fwupd not installed)"),
            )
            self.assertTrue(any("cooling-device: fail" in line for line in lines))
            self.assertTrue(problems)
            self.assertFalse(any(line.startswith("firmware:") for line in lines))

            lines = []
            problems = CONTROL.collect_health_checks(
                root / "thermal", root / "hwmon",
                module_loaded=True, service_active="inactive",
                service_enabled="disabled", exec_status=None,
                watchdog_active="active", soc_celsius=85, log_text="",
                ready="not configured", emit=lines.append,
                ec_block=("note", "unknown (fwupd not installed)"),
            )
            self.assertIn("cooling-device: fail (telemetry missing)", lines)
            self.assertTrue(problems)

        stop = Event()
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "true-zero-reboot"
            with patch.object(CONTROL, "Event", return_value=stop), \
                 patch.object(CONTROL.signal, "signal"), \
                 patch.object(stop, "wait", side_effect=lambda delay: stop.set()):
                status = CONTROL.run_watchdog(
                    Path("/tmp/dgx-fan-missing"),
                    Path("/tmp/dgx-fan-missing"),
                    unit_state=lambda: (
                        "inactive", "enabled", CONTROL.UNSUPPORTED_FIRMWARE_EXIT,
                    ),
                    notifier=lambda _message: None,
                    runner=lambda action: 0,
                    monotonic=lambda: 0.0,
                    marker_path=marker,
                    boot_id="boot-a",
                    defaults={},
                )
            self.assertEqual(status, 0)
            self.assertFalse((Path(directory) / CONTROL.INTERVENTION_NAME).exists())

        alerts = []
        stop = Event()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._stopped_acpi_fans(root, (2073, 2154), 40.0)
            hwmon = root / "hwmon" / "hwmon1"
            hwmon.mkdir()
            (hwmon / "name").write_text(
                CONTROL.HWMON_DEVICE_NAME + "\n", encoding="ascii",
            )
            with patch.object(CONTROL, "Event", return_value=stop), \
                 patch.object(CONTROL.signal, "signal"), \
                 patch.object(stop, "wait", side_effect=lambda delay: stop.set()), \
                 self.assertLogs(CONTROL.LOG, level="WARNING") as logs:
                status = CONTROL.run_watchdog(
                    root / "thermal", root / "hwmon",
                    unit_state=lambda: ("failed", "enabled", 1),
                    notifier=alerts.append,
                    runner=lambda action: 0,
                    monotonic=lambda: 0.0,
                    marker_path=root / "marker" / "true-zero-reboot",
                    boot_id="boot-a",
                    defaults={},
                )
            self.assertEqual(status, 0)
            self.assertEqual(alerts, [])
            self.assertFalse(any("is not active" in line for line in logs.output))
            self.assertTrue(any("not alerting" in line for line in logs.output))

        alerts = []
        stop = Event()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(CONTROL, "Event", return_value=stop), \
             patch.object(CONTROL.signal, "signal"), \
             patch.object(stop, "wait", side_effect=lambda delay: stop.set()), \
             self.assertLogs(CONTROL.LOG, level="WARNING") as logs:
            status = CONTROL.run_watchdog(
                Path("/tmp/dgx-fan-missing"),
                Path("/tmp/dgx-fan-missing"),
                unit_state=lambda: ("failed", "enabled", CONTROL.UNSUPPORTED_FIRMWARE_EXIT),
                notifier=alerts.append,
                runner=lambda action: 0,
                monotonic=lambda: 0.0,
                marker_path=Path(directory) / "true-zero-reboot",
                boot_id="boot-a",
                defaults={},
            )
        self.assertEqual(status, 0)
        self.assertEqual(alerts, [])
        self.assertFalse(any("is not active" in line for line in logs.output))

    def test_fan_line_names_rpm_and_stale_health_is_marked_or_cleared(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._stopped_acpi_fans(root, (0, 2150), 40.0)
            (root / "thermal" / "cooling_device1" / "cur_state").write_text(
                "100\n", encoding="ascii",
            )
            hwmon = root / "hwmon" / "hwmon1"
            hwmon.mkdir()
            (hwmon / "name").write_text(
                CONTROL.HWMON_DEVICE_NAME + "\n", encoding="ascii",
            )
            (hwmon / "fan1_input").write_text("5000\n", encoding="ascii")
            (hwmon / "fan2_input").write_text("5000\n", encoding="ascii")
            status, detail = CONTROL._fan_status(root / "hwmon", root / "thermal")
            self.assertEqual(status, "fail")
            self.assertIn("fan1=0", detail)
            self.assertIn("fan2=2150 RPM", detail)
            self.assertIn("acpi cur_state=cooling_device0:40", detail)
            self.assertIn("cooling_device1:100", detail)
            self.assertNotIn("5000", detail)

            (root / "hwmon" / "hwmon0" / "fan1_input").write_text("2073\n", encoding="ascii")
            status, detail = CONTROL._fan_status(root / "hwmon", root / "thermal")
            self.assertEqual(status, "pass", detail)
            self.assertIn("fan1=2073", detail)
            self.assertIn("fan2=2150 RPM", detail)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._healthy_tree(root)
            state = root / "state"
            health = state / CONTROL.HEALTH_NAME
            CONTROL.write_state_text(
                health,
                "result=needs-intervention\nready=not configured\n"
                "boot_id=boot-old\ntime=1.000\n"
                "reason=workload not ready\n",
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code, _alerts, _notices = self._health(
                    root, {}, self._healthy_snapshot(log_text="", ec_updates=(
                        "note", "unknown (fwupd not installed)",
                    )),
                    record=False, boot="boot-b",
                )
            text = stdout.getvalue()
            self.assertEqual(code, 0, text)
            self.assertIn("health: healthy", text)
            self.assertIn(
                "time=" + CONTROL.time.strftime(
                    "%Y-%m-%d %H:%M:%S %Z", CONTROL.time.localtime(1.0),
                ),
                text,
            )
            self.assertIn(
                "stale (previous boot, firmware not recorded, older record format)",
                text,
            )
            self.assertIn(
                "`sudo dgx-fan-control health --record` saves the current result",
                text,
            )
            self.assertIn("recorded: no", text)
            self.assertEqual(
                health.read_text(encoding="ascii"),
                "result=needs-intervention\nready=not configured\n"
                "boot_id=boot-old\ntime=1.000\n"
                "reason=workload not ready\n",
            )

            CONTROL.write_state_text(
                health,
                "result=needs-intervention\nready=not configured\n"
                "boot_id=boot-b\ntime=2.000\n"
                "reason=unsupported firmware; telemetry only\n",
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code, _alerts, _notices = self._health(
                    root, {}, self._healthy_snapshot(
                        module_loaded=True,
                        log_text=(
                            "pinned EC 3.13.7 capabilities fan0=975..4350 "
                            "fan1=975..4350\n"
                            "EC 3.13.7 floor writes disabled; registering hwmon "
                            "telemetry only, no cooling device\n"
                        ),
                        ec_updates=("pass", "blocked"),
                    ),
                    record=False, boot="boot-b",
                )
            text = stdout.getvalue()
            self.assertEqual(code, 0, text)
            self.assertIn("stored health: none", text)
            self.assertFalse(health.exists())

            CONTROL.write_state_text(
                health,
                "result=needs-intervention\nready=not configured\n"
                "boot_id=boot-b\ntime=3.000\nfirmware=EC 3.5.8\n"
                "reason=workload not ready\n",
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code, _alerts, _notices = self._health(
                    root, {}, self._healthy_snapshot(
                        module_loaded=True,
                        log_text=(
                            "pinned EC 3.13.7 capabilities fan0=975..4350 "
                            "fan1=975..4350\n"
                        ),
                        ec_updates=("pass", "blocked"),
                    ),
                    record=False, boot="boot-b",
                )
            text = stdout.getvalue()
            self.assertEqual(code, 0, text)
            self.assertIn(
                "time=" + CONTROL.time.strftime(
                    "%Y-%m-%d %H:%M:%S %Z", CONTROL.time.localtime(3.0),
                ),
                text,
            )
            self.assertIn("stale (firmware changed, older record format)", text)
            self.assertIn(
                "`sudo dgx-fan-control health --record` saves the current result",
                text,
            )
            self.assertEqual(
                health.read_text(encoding="ascii"),
                "result=needs-intervention\nready=not configured\n"
                "boot_id=boot-b\ntime=3.000\nfirmware=EC 3.5.8\n"
                "reason=workload not ready\n",
            )

    def test_stored_health_uses_local_time_and_marks_missing_schema(self):
        stamp = 1793949945.325
        shown_time = CONTROL.time.strftime(
            "%Y-%m-%d %H:%M:%S %Z", CONTROL.time.localtime(stamp),
        )
        clears = (
            "on a healthy run it also clears the stored needs-intervention flag "
            "and the stall and watchdog-reboot markers"
        )
        current = (
            "result=needs-intervention\nready=not configured\n"
            f"boot_id=boot-b\ntime={stamp:.3f}\n"
            f"version={CONTROL.HEALTH_RECORD_VERSION}\n"
            f"code={CONTROL.HEALTH_CODE}\n"
            "firmware=EC 3.13.7\n"
            "reason=fans: fail\n"
        )

        def fmt(text, healthy=True, boot="boot-b"):
            return CONTROL.format_stored_health(
                text, boot, "EC 3.13.7", current_healthy=healthy,
            )

        # Same boot, same install, a real earlier failure: not stale, but the
        # reason and a note are shown so it is not mistaken for the current run.
        line, hint = fmt(current)
        self.assertIn(f"time={shown_time}", line)
        self.assertNotIn(f"time={stamp:.3f}", line)
        self.assertIn("reason=fans: fail", line)
        self.assertNotIn("stale", line)
        self.assertIn("an earlier run this boot failed", hint)
        self.assertIn(clears, hint)
        self.assertIn("logs and broadcasts that recovery once", hint)

        # Do not claim the failure was this boot when either boot id is missing.
        # The --record sentence still says "this boot" about a watchdog reboot.
        line, hint = fmt(current, boot=None)
        self.assertNotIn("stale", line)
        self.assertNotIn("an earlier run this boot failed", hint)
        self.assertIn("an earlier recorded run failed", hint)
        line, hint = fmt(current.replace("boot_id=boot-b\n", "boot_id=\n"))
        self.assertNotIn("stale", line)
        self.assertNotIn("an earlier run this boot failed", hint)

        # The same record during a failing run gets no note.
        line, hint = fmt(current, healthy=False)
        self.assertNotIn("stale", line)
        self.assertIsNone(hint)

        line, _hint = fmt(current.replace(f"time={stamp:.3f}", "time=not-a-time"))
        self.assertIn("time=not-a-time", line)
        self.assertNotIn("stale", line)
        line, _hint = fmt(current.replace(f"time={stamp:.3f}", "time=nan"))
        self.assertIn("time=nan", line)
        line, _hint = fmt(current.replace(f"time={stamp:.3f}", "time=1e309"))
        self.assertIn("time=1e309", line)

        line, hint = fmt(current.replace("firmware=EC 3.13.7\n", ""))
        self.assertIn("stale (firmware not recorded)", line)
        self.assertIn("superseded by the current healthy run", hint)
        self.assertIn(clears, hint)

        line, hint = fmt(current.replace(
            f"version={CONTROL.HEALTH_RECORD_VERSION}\n", "",
        ))
        self.assertIn("stale (older record format)", line)
        self.assertIn("superseded", hint)

        line, _hint = fmt(current.replace(
            f"version={CONTROL.HEALTH_RECORD_VERSION}", "version=1",
        ))
        self.assertIn("stale (older record format)", line)
        self.assertNotIn("different install", line)

        # Any reinstall of different code marks the record stale without a
        # hand-bumped format number.
        line, hint = fmt(current.replace(
            f"code={CONTROL.HEALTH_CODE}", "code=000000000000",
        ))
        self.assertIn("stale (written by a different install)", line)
        self.assertIn("superseded", hint)

        line, hint = fmt(current.replace(f"code={CONTROL.HEALTH_CODE}\n", ""))
        self.assertIn("stale (written by a different install)", line)

        line, hint = fmt(current, boot="boot-c")
        self.assertIn("stale (previous boot)", line)
        self.assertIn("superseded", hint)

        line, hint = fmt(
            current.replace("result=needs-intervention", "result=healthy")
            .replace("reason=fans: fail\n", "")
            .replace(f"code={CONTROL.HEALTH_CODE}", "code=000000000000"),
        )
        self.assertIn("stale (written by a different install)", line)
        self.assertNotIn("reason=", line)
        self.assertIsNone(hint)

        self.assertEqual(fmt("none"), ("none", None))
        self.assertEqual(fmt(CONTROL.NEEDS_ROOT), (CONTROL.NEEDS_ROOT, None))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._healthy_tree(root)
            state = root / "state"
            health = state / CONTROL.HEALTH_NAME
            original = (
                "result=needs-intervention\nready=not configured\n"
                "boot_id=boot-b\ntime=1793949945.325\n"
                "reason=workload not ready\n"
            )
            CONTROL.write_state_text(health, original)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code, _alerts, _notices = self._health(
                    root, {}, self._healthy_snapshot(
                        module_loaded=True,
                        log_text=(
                            "pinned EC 3.13.7 capabilities fan0=975..4350 "
                            "fan1=975..4350\n"
                            "EC 3.13.7 floor writes disabled; registering hwmon "
                            "telemetry only, no cooling device\n"
                        ),
                        ec_updates=("pass", "blocked"),
                    ),
                    record=False, boot="boot-b",
                )
            text = stdout.getvalue()
            self.assertEqual(code, 0, text)
            self.assertIn("health: healthy", text)
            self.assertIn(f"time={shown_time}", text)
            self.assertIn("reason=workload not ready", text)
            self.assertIn(
                "stale (firmware not recorded, older record format)", text,
            )
            self.assertIn(
                "stored health note: superseded by the current healthy run.",
                text,
            )
            self.assertIn(clears, text)
            self.assertIn("recorded: no", text)
            self.assertEqual(health.read_text(encoding="ascii"), original)
            self.assertFalse((state / CONTROL.INTERVENTION_NAME).exists())

            CONTROL.write_health_record(
                health, "healthy", "n/a", [], "boot-b", stamp, "EC 3.13.7",
            )
            written = health.read_text(encoding="ascii")
            self.assertIn(f"version={CONTROL.HEALTH_RECORD_VERSION}\n", written)
            self.assertIn(f"code={CONTROL.HEALTH_CODE}\n", written)
            self.assertIn("firmware=EC 3.13.7\n", written)
            self.assertNotIn("needs-intervention", written)
            line, hint = fmt(written)
            self.assertIn(f"time={shown_time}", line)
            self.assertNotIn("stale", line)
            self.assertIsNone(hint)

    def test_health_cli_prints_stored_record_and_does_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._healthy_tree(root)
            state = root / "state"
            health = state / CONTROL.HEALTH_NAME
            shown = CONTROL.time.strftime(
                "%Y-%m-%d %H:%M:%S %Z", CONTROL.time.localtime(1793949945.325),
            )

            def run(argv, *, loaded=True):
                with patch.object(CONTROL, "STALL_MARKER_DIR", state), \
                     patch.object(CONTROL.os, "geteuid", return_value=1000), \
                     patch.object(CONTROL, "read_boot_id", return_value="boot-now"), \
                     patch.object(CONTROL, "read_kernel_log", return_value=""), \
                     patch.object(
                         CONTROL, "read_daemon_facts",
                         return_value=("inactive", "disabled", None, None, None),
                     ), \
                     patch.object(
                         CONTROL, "watchdog_unit_state",
                         return_value=("active", "enabled"),
                     ), \
                     patch.object(CONTROL, "_module_loaded_now", return_value=loaded):
                    return run_cli([
                        "--thermal-root", str(root / "thermal"),
                        "--hwmon-root", str(root / "hwmon"),
                        *argv,
                    ])

            old = (
                "result=needs-intervention\nready=not configured\n"
                "boot_id=boot-old\ntime=not-a-time\n"
                "reason=workload not ready\n"
            )
            CONTROL.write_state_text(health, old)
            code, stdout, stderr = run(["health"])
            combined = stdout + stderr
            self.assertEqual(code, 0, combined)
            self.assertIn("health: healthy", stdout)
            self.assertIn("time=not-a-time", stdout)
            self.assertIn("reason=workload not ready", stdout)
            self.assertIn(
                "stale (previous boot, firmware not recorded, older record format)",
                stdout,
            )
            self.assertIn(
                "stored health note: superseded by the current healthy run.",
                stdout,
            )
            self.assertIn("recorded: no", stdout)
            self.assertNotIn("Traceback", combined)
            self.assertEqual(health.read_text(encoding="ascii"), old)

            same = (
                "result=needs-intervention\nready=fail\n"
                "boot_id=boot-now\ntime=1793949945.325\n"
                f"version={CONTROL.HEALTH_RECORD_VERSION}\n"
                f"code={CONTROL.HEALTH_CODE}\n"
                "firmware=unknown\n"
                "reason=fans: fail\n"
            )
            CONTROL.write_state_text(health, same)
            code, stdout, stderr = run(["health", "--record"])
            combined = stdout + stderr
            self.assertEqual(code, 1, combined)
            self.assertIn("health --record requires root", stdout)
            self.assertIn("health: healthy", stdout)
            self.assertIn(f"time={shown}", stdout)
            self.assertIn("reason=fans: fail", stdout)
            self.assertIn(
                "stored health note: an earlier run this boot failed; "
                "the current run is healthy.",
                stdout,
            )
            self.assertNotIn("stale (", stdout)
            self.assertIn("recorded: no", stdout)
            self.assertNotIn("Traceback", combined)
            self.assertEqual(health.read_text(encoding="ascii"), same)
            self.assertFalse((state / CONTROL.INTERVENTION_NAME).exists())

            code, stdout, stderr = run(["health"], loaded=False)
            combined = stdout + stderr
            self.assertEqual(code, 1, combined)
            self.assertIn("health: fail", stdout)
            self.assertIn("reason=fans: fail", stdout)
            self.assertNotIn("stored health note:", stdout)
            self.assertEqual(health.read_text(encoding="ascii"), same)

            health.unlink()
            code, stdout, stderr = run(["health"])
            combined = stdout + stderr
            self.assertEqual(code, 0, combined)
            self.assertIn("stored health: none", stdout)
            self.assertNotIn("stored health note:", stdout)
            self.assertIn("recorded: no", stdout)
            self.assertFalse(health.exists())

            CONTROL.write_state_text(health, same)
            real_read = Path.read_text

            def deny_health(self, *args, **kwargs):
                if self.name == CONTROL.HEALTH_NAME:
                    raise PermissionError(errno.EACCES, "Permission denied")
                return real_read(self, *args, **kwargs)

            with patch.object(Path, "read_text", deny_health):
                code, stdout, stderr = run(["health"])
            combined = stdout + stderr
            self.assertEqual(code, 0, combined)
            self.assertIn("stored health: unknown (needs root)", stdout)
            self.assertNotIn("stored health note:", stdout)
            self.assertIn("recorded: no", stdout)
            self.assertNotIn("Traceback", combined)
            self.assertEqual(health.read_text(encoding="ascii"), same)

    def test_unsupported_record_stays_but_is_not_shown_when_unlink_is_denied(self):
        reason = "unsupported firmware; telemetry only"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._healthy_tree(root)
            state = root / "state"
            health = state / CONTROL.HEALTH_NAME
            flag = state / CONTROL.INTERVENTION_NAME
            health_text = (
                "result=needs-intervention\nready=not configured\n"
                "boot_id=boot-old\ntime=1793949945.325\n"
                f"reason={reason}\n"
            )
            flag_text = f"reason={reason}\nboot_id=boot-old\ntime=1.000\n"
            CONTROL.write_state_text(health, health_text)
            CONTROL.write_state_text(flag, flag_text)
            real_unlink = Path.unlink

            def deny_unlink(self, *args, **kwargs):
                if self.name in {CONTROL.HEALTH_NAME, CONTROL.INTERVENTION_NAME}:
                    raise PermissionError(errno.EACCES, "Permission denied")
                return real_unlink(self, *args, **kwargs)

            def run(argv):
                with patch.object(CONTROL, "STALL_MARKER_DIR", state), \
                     patch.object(CONTROL.os, "geteuid", return_value=1000), \
                     patch.object(CONTROL, "read_boot_id", return_value="boot-now"), \
                     patch.object(CONTROL, "read_kernel_log", return_value=(
                         "pinned EC 3.13.7 capabilities fan0=975..4350 "
                         "fan1=975..4350\n"
                         "EC 3.13.7 floor writes disabled; registering hwmon "
                         "telemetry only, no cooling device\n"
                     )), \
                     patch.object(
                         CONTROL, "read_daemon_facts",
                         return_value=("inactive", "disabled", 0, "success", 0),
                     ), \
                     patch.object(
                         CONTROL, "watchdog_unit_state",
                         return_value=("active", "enabled"),
                     ), \
                     patch.object(CONTROL, "_module_loaded_now", return_value=True), \
                     patch.object(Path, "unlink", deny_unlink):
                    return run_cli([
                        "--thermal-root", str(root / "thermal"),
                        "--hwmon-root", str(root / "hwmon"),
                        *argv,
                    ])

            code, stdout, stderr = run(["health"])
            combined = stdout + stderr
            self.assertEqual(code, 0, combined)
            self.assertIn("health: healthy", stdout)
            self.assertIn(f"stored needs-intervention: {CONTROL.DISMISSED_UNSUPPORTED}", stdout)
            self.assertIn(f"stored health: {CONTROL.DISMISSED_UNSUPPORTED}", stdout)
            self.assertNotIn("stored health note:", stdout)
            self.assertNotIn("stale (", stdout)
            self.assertIn("recorded: no", stdout)
            self.assertNotIn("Traceback", combined)
            self.assertEqual(health.read_text(encoding="ascii"), health_text)
            self.assertEqual(flag.read_text(encoding="ascii"), flag_text)

            code, stdout, stderr = run(["status"])
            self.assertEqual(code, 0, stdout + stderr)
            self.assertNotIn("NEEDS INTERVENTION:", stdout)
            self.assertEqual(flag.read_text(encoding="ascii"), flag_text)

            kept = health_text.replace(reason, "workload not ready")
            CONTROL.write_state_text(health, kept)
            CONTROL.write_state_text(
                flag, "reason=workload not ready\nboot_id=boot-old\ntime=1.000\n",
            )
            code, stdout, stderr = run(["health"])
            self.assertEqual(code, 0, stdout + stderr)
            self.assertIn("reason=workload not ready", stdout)
            self.assertIn("stale (", stdout)
            self.assertNotIn("dismissed:", stdout)
            self.assertEqual(health.read_text(encoding="ascii"), kept)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._healthy_tree(root)
            state = root / "state"
            health = state / CONTROL.HEALTH_NAME
            flag = state / CONTROL.INTERVENTION_NAME
            CONTROL.write_state_text(
                health,
                "result=needs-intervention\nready=not configured\n"
                f"boot_id=boot-old\ntime=1.000\nreason={reason}\n",
            )
            CONTROL.write_state_text(flag, f"reason={reason}\nboot_id=old\ntime=1.000\n")
            removed = CONTROL.dismiss_unsupported_health_record(health)
            self.assertTrue(removed)
            self.assertFalse(health.exists())
            self.assertTrue(CONTROL.dismiss_unsupported_firmware_flag(flag))
            self.assertFalse(flag.exists())

    def test_progress_line_names_the_reason_for_every_uid(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            CONTROL.observe_hot_job(
                now=0.0, fan_rpms=[2700, 4050], hottest=40.0, busy=False,
                defaults={}, state_dir=state, notifier=lambda _message: None,
                alert_repeat=1800, zero_fan_celsius=85, stuck_seconds=600,
                state={}, busy_how="nvidia-smi",
            )
            idle = (state / CONTROL.JOB_WATCH_NAME).read_text(encoding="ascii")
            self.assertIn("source=off\n", idle)
            self.assertIn("detail=no job running\n", idle)

            CONTROL.observe_hot_job(
                now=1.0, fan_rpms=[2700, 4050], hottest=40.0, busy=True,
                defaults={}, state_dir=state, notifier=lambda _message: None,
                alert_repeat=1800, zero_fan_celsius=85, stuck_seconds=600,
                state={}, busy_how="nvidia-smi",
                builtin_reader=lambda: (50.0, {}),
            )
            busy = (state / CONTROL.JOB_WATCH_NAME).read_text(encoding="ascii")
            self.assertIn("source=built-in\n", busy)
            self.assertIn("detail=GPU utilization\n", busy)

            CONTROL.observe_hot_job(
                now=2.0, fan_rpms=[2700, 4050], hottest=40.0, busy=True,
                defaults={}, state_dir=state, notifier=lambda _message: None,
                alert_repeat=1800, zero_fan_celsius=85, stuck_seconds=600,
                state={}, busy_how="nvidia-smi-failed",
                builtin_reader=lambda: (None, None),
            )
            failed = (state / CONTROL.JOB_WATCH_NAME).read_text(encoding="ascii")
            self.assertIn("source=unknown\n", failed)
            self.assertIn("could not sample GPU: nvidia-smi exited", failed)
            self.assertIn("transient after a restart is normal", failed)
            self.assertNotIn("GPU utilization", failed)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._healthy_tree(root)
            state = root / "state"
            watch = state / CONTROL.JOB_WATCH_NAME
            CONTROL.write_state_text(
                watch,
                "source=built-in\ndetail=GPU utilization\nstuck=no\n"
                "no_progress_seconds=0\n",
            )

            def run(uid):
                with patch.object(CONTROL, "STALL_MARKER_DIR", state), \
                     patch.object(CONTROL.os, "geteuid", return_value=uid), \
                     patch.object(CONTROL, "read_boot_id", return_value="boot-now"), \
                     patch.object(CONTROL, "read_kernel_log", return_value=""), \
                     patch.object(
                         CONTROL, "read_daemon_facts",
                         return_value=("inactive", "disabled", None, None, None),
                     ), \
                     patch.object(
                         CONTROL, "watchdog_unit_state",
                         return_value=("active", "enabled"),
                     ), \
                     patch.object(CONTROL, "_module_loaded_now", return_value=True):
                    return run_cli([
                        "--thermal-root", str(root / "thermal"),
                        "--hwmon-root", str(root / "hwmon"),
                        "health",
                    ])

            user_code, user_out, user_err = run(1000)
            root_code, root_out, root_err = run(0)
            self.assertEqual(user_code, 0, user_out + user_err)
            self.assertEqual(root_code, 0, root_out + root_err)
            self.assertIn("progress: built-in (GPU utilization)", user_out)
            self.assertIn("progress: built-in (GPU utilization)", root_out)
            self.assertNotIn("progress: built-in (GPU utilization; recorded", user_out)
            self.assertNotIn("progress: built-in (GPU utilization; recorded", root_out)
            self.assertNotIn("job: warn", user_out)
            self.assertNotIn("job: warn", root_out)
            self.assertEqual(
                watch.read_text(encoding="ascii"),
                "source=built-in\ndetail=GPU utilization\nstuck=no\n"
                "no_progress_seconds=0\n",
            )

            CONTROL.write_state_text(
                watch,
                "source=off\ndetail=no job running\nstuck=no\n"
                "no_progress_seconds=0\n",
            )
            user_code, user_out, user_err = run(1000)
            root_code, root_out, root_err = run(0)
            self.assertEqual(user_code, 0, user_out + user_err)
            self.assertEqual(root_code, 0, root_out + root_err)
            self.assertIn("progress: off (no job running)", user_out)
            self.assertIn("progress: off (no job running)", root_out)
            self.assertNotIn("progress: off (no job running; recorded", user_out)
            self.assertNotIn("progress: off (no job running; recorded", root_out)

            real_read = Path.read_text

            def deny_watch(self, *args, **kwargs):
                if self.name == CONTROL.JOB_WATCH_NAME:
                    raise PermissionError(errno.EACCES, "Permission denied")
                return real_read(self, *args, **kwargs)

            with patch.object(Path, "read_text", deny_watch):
                code, stdout, stderr = run(1000)
            self.assertEqual(code, 0, stdout + stderr)
            self.assertIn("progress: unknown (needs root)", stdout)
            self.assertNotIn("progress: off", stdout)
            self.assertNotIn("progress: built-in", stdout)
            self.assertIn("health: healthy", stdout)

    def _two_acpi_fan_devices(self, root: Path, second: str | None):
        self._healthy_tree(root)
        first = root / "hwmon" / "hwmon0"
        (first / "fan2_input").unlink()
        (first / "fan1_input").write_text("2066\n", encoding="ascii")
        other = root / "hwmon" / "hwmon1"
        other.mkdir()
        (other / "name").write_text("acpi_fan\n", encoding="ascii")
        if second is not None:
            (other / "fan1_input").write_text(second, encoding="ascii")
        return other

    def test_health_rechecks_a_transient_zero_tachometer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            other = self._two_acpi_fan_devices(root, "0\n")
            sleeps = []

            def sleeper(delay):
                sleeps.append(delay)
                (other / "fan1_input").write_text("2148\n", encoding="ascii")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code, _alerts, _notices = self._health(
                    root, {}, self._healthy_snapshot(), record=False, sleeper=sleeper,
                )
            text = stdout.getvalue()
            self.assertEqual(code, 0, text)
            self.assertIn("health: healthy", text)
            self.assertIn("fans: pass (", text)
            self.assertIn("hwmon1:fan1=2148", text)
            self.assertIn(
                "hwmon1:fan1 read 0 RPM once, recovered to 2148 RPM "
                "on recheck (transient)",
                text,
            )
            self.assertEqual(sleeps, [CONTROL.HEALTH_FAN_RECHECK_SECONDS])
            self.assertFalse((root / "state" / CONTROL.HEALTH_NAME).exists())

    def test_health_fails_a_zero_tachometer_that_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._two_acpi_fan_devices(root, "0\n")
            sleeps = []
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code, _alerts, _notices = self._health(
                    root, {}, self._healthy_snapshot(), record=False,
                    sleeper=sleeps.append,
                )
            text = stdout.getvalue()
            self.assertNotEqual(code, 0, text)
            self.assertIn("health: fail", text)
            self.assertIn("fans: fail (", text)
            self.assertIn("hwmon1:fan1=0", text)
            self.assertNotIn("recovered", text)
            self.assertNotIn("transient", text)
            self.assertEqual(
                sleeps, [CONTROL.HEALTH_FAN_RECHECK_SECONDS] * CONTROL.HEALTH_FAN_RECHECK_LIMIT,
            )
            self.assertLessEqual(sum(sleeps), 3.0)
            self.assertFalse((root / "state" / CONTROL.HEALTH_NAME).exists())

    def test_health_rechecks_a_missing_tachometer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            other = self._two_acpi_fan_devices(root, None)
            sleeps = []

            def sleeper(delay):
                sleeps.append(delay)
                (other / "fan1_input").write_text("2148\n", encoding="ascii")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code, _alerts, _notices = self._health(
                    root, {}, self._healthy_snapshot(), record=False, sleeper=sleeper,
                )
            text = stdout.getvalue()
            self.assertEqual(code, 0, text)
            self.assertIn(
                "hwmon1:fan1 missing once, recovered to 2148 RPM "
                "on recheck (transient)",
                text,
            )
            self.assertIn("fans: pass (", text)
            self.assertEqual(sleeps, [1.0])

            (other / "fan1_input").unlink()
            sleeps.clear()
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code, _alerts, _notices = self._health(
                    root, {}, self._healthy_snapshot(), record=False,
                    sleeper=sleeps.append,
                )
            text = stdout.getvalue()
            self.assertNotEqual(code, 0, text)
            self.assertIn("fans: fail (", text)
            self.assertIn("hwmon0:fan1=2066", text)
            self.assertNotIn("hwmon1:fan1=", text)
            self.assertNotIn("recovered", text)
            self.assertEqual(sleeps, [1.0, 1.0, 1.0])

    def test_legacy_nvidia_smi_failed_after_restart_uses_the_gap_wording(self):
        stamp = 1793949945.0
        shown = CONTROL.format_utc_stamp(stamp)
        gap = CONTROL.gpu_gap_sentence()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._healthy_tree(root)
            state = root / "state"
            watch = state / CONTROL.JOB_WATCH_NAME
            mtime = stamp + 10000.0
            CONTROL.write_state_text(
                watch,
                "source=off\ndetail=nvidia-smi failed\nstuck=no\n"
                "no_progress_seconds=0\n"
                f"recorded={stamp:.3f}\n",
            )
            os.utime(watch, (mtime, mtime))
            CONTROL.write_state_text(
                state / CONTROL.WATCHDOG_STARTED_NAME,
                f"time={stamp + 20000:.3f}\n",
            )

            def run():
                with patch.object(CONTROL, "STALL_MARKER_DIR", state), \
                     patch.object(CONTROL.os, "geteuid", return_value=1000), \
                     patch.object(CONTROL, "read_boot_id", return_value="boot-now"), \
                     patch.object(CONTROL, "read_kernel_log", return_value=""), \
                     patch.object(
                         CONTROL, "read_daemon_facts",
                         return_value=("inactive", "disabled", None, None, None),
                     ), \
                     patch.object(
                         CONTROL, "watchdog_unit_state",
                         return_value=("active", "enabled"),
                     ), \
                     patch.object(CONTROL, "_module_loaded_now", return_value=True):
                    return run_cli([
                        "--thermal-root", str(root / "thermal"),
                        "--hwmon-root", str(root / "hwmon"),
                        "health",
                    ])

            code, stdout, stderr = run()
            self.assertEqual(code, 0, stdout + stderr)
            self.assertNotIn("nvidia-smi failed", stdout)
            self.assertIn("health: healthy", stdout)
            self.assertIn("progress: waiting for first watchdog sample ", stdout)
            self.assertIn(f"previous sample at {shown}: {gap}", stdout)
            self.assertNotIn(CONTROL.format_utc_stamp(mtime), stdout)
            self.assertEqual(
                watch.read_text(encoding="ascii"),
                "source=off\ndetail=nvidia-smi failed\nstuck=no\n"
                "no_progress_seconds=0\n"
                f"recorded={stamp:.3f}\n",
            )

            CONTROL.write_state_text(
                watch,
                "source=off\ndetail=nvidia-smi failed\nstuck=no\n"
                "no_progress_seconds=0\n",
            )
            os.utime(watch, (mtime, mtime))
            code, stdout, stderr = run()
            self.assertEqual(code, 0, stdout + stderr)
            self.assertNotIn("nvidia-smi failed", stdout)
            self.assertIn(
                "previous sample, time unknown: " + gap, stdout,
            )
            self.assertNotIn(CONTROL.format_utc_stamp(mtime), stdout)
            self.assertNotIn(shown, stdout)

    def test_first_gpu_sample_waits_until_the_startup_grace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._stopped_acpi_fans(root, (2700, 4050), 40.0)
            state = root / "state"
            watch = state / CONTROL.JOB_WATCH_NAME
            CONTROL.write_state_text(
                watch,
                "source=off\ndetail=nvidia-smi failed\nstuck=no\n"
                "no_progress_seconds=0\n",
            )
            clock = [0.0]
            stop = Event()
            calls = []

            def gpu():
                calls.append(clock[0])
                if len(calls) == 1:
                    self.assertIn(
                        "detail=nvidia-smi failed\n",
                        watch.read_text(encoding="ascii"),
                    )
                return False

            def wait(_delay):
                clock[0] += 2
                if clock[0] > 6:
                    stop.set()

            with patch.object(CONTROL, "Event", return_value=stop), \
                 patch.object(CONTROL.signal, "signal"), \
                 patch.object(stop, "wait", side_effect=wait), \
                 self.assertLogs(CONTROL.LOG, level="INFO"):
                status = CONTROL.run_watchdog(
                    root / "thermal", root / "hwmon",
                    poll_seconds=2, monotonic=lambda: clock[0],
                    wall_time=lambda: 1000.0,
                    runner=lambda action: 0,
                    unit_state=lambda: "active",
                    notifier=lambda _message: None,
                    gpu_jobs=gpu,
                    marker_path=state / "true-zero-reboot",
                    defaults={},
                )
            self.assertEqual(status, 0)
            self.assertEqual(calls, [6.0])
            self.assertNotIn("nvidia-smi failed", watch.read_text(encoding="ascii"))

    def test_unsupported_firmware_flag_clears_on_read_and_is_never_written(self):
        reason = "unsupported firmware; telemetry only"
        held = reason + "; manual floor state 6 is not held"

        with tempfile.TemporaryDirectory() as directory:
            flag = Path(directory) / CONTROL.INTERVENTION_NAME
            CONTROL.write_intervention(flag, reason, "boot-a", 1.0)
            self.assertFalse(flag.exists())
            CONTROL.write_intervention(flag, held, "boot-a", 1.0)
            self.assertFalse(flag.exists())
            CONTROL.write_intervention(flag, "foreign floor remains", "boot-a", 1.0)
            self.assertIn("foreign floor remains", flag.read_text(encoding="ascii"))

        def telemetry_tree(root: Path) -> None:
            self._stopped_acpi_fans(root, (2073, 2154), 40.0)
            hwmon = root / "hwmon" / "hwmon1"
            hwmon.mkdir()
            (hwmon / "name").write_text(
                CONTROL.HWMON_DEVICE_NAME + "\n", encoding="ascii",
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            telemetry_tree(root)
            state = root / "state"
            flag = state / CONTROL.INTERVENTION_NAME
            CONTROL.write_state_text(flag, f"reason={reason}\nboot_id=old\ntime=1.000\n")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code, _alerts, _notices = self._health(
                    root, {},
                    self._healthy_snapshot(
                        module_loaded=True,
                        log_text=(
                            "pinned EC 3.13.7 capabilities fan0=975..4350 "
                            "fan1=975..4350\n"
                            "EC 3.13.7 floor writes disabled; registering hwmon "
                            "telemetry only, no cooling device\n"
                        ),
                        ec_updates=("pass", "blocked"),
                    ),
                    record=False,
                )
            text = stdout.getvalue()
            self.assertEqual(code, 0, text)
            self.assertNotIn("NEEDS INTERVENTION:", text)
            self.assertIn("stored needs-intervention: none", text)
            self.assertIn("recorded: no", text)
            self.assertFalse(flag.exists())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            telemetry_tree(root)
            state = root / "state"
            flag = state / CONTROL.INTERVENTION_NAME
            CONTROL.write_state_text(flag, f"reason={reason}\nboot_id=old\ntime=1.000\n")
            CONTROL.write_last_manual_state(state, 6, floor=2700, profile="EC 3.5.8")
            with patch.object(CONTROL, "STALL_MARKER_DIR", state):
                code, stdout, stderr = run_cli([
                    "--thermal-root", str(root / "thermal"),
                    "--hwmon-root", str(root / "hwmon"),
                    "status",
                ])
            combined = stdout + stderr
            self.assertEqual(code, 0, combined)
            self.assertIn(CONTROL.TELEMETRY_ONLY_STATUS, stdout)
            self.assertIn(f"clock-cap: {CONTROL.CLOCK_CAP_HINT}\n", stdout)
            self.assertNotIn("NEEDS INTERVENTION:", combined)
            self.assertFalse(flag.exists())
            self.assertIn("state=6", (state / CONTROL.MANUAL_STATE_NAME).read_text())

            CONTROL.write_state_text(
                flag, "reason=foreign floor remains\nboot_id=old\ntime=1.000\n",
            )
            with patch.object(CONTROL, "STALL_MARKER_DIR", state):
                code, stdout, stderr = run_cli([
                    "--thermal-root", str(root / "thermal"),
                    "--hwmon-root", str(root / "hwmon"),
                    "status",
                ])
            self.assertIn("NEEDS INTERVENTION: foreign floor remains", stdout)
            self.assertTrue(flag.exists())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            telemetry_tree(root)
            state = root / "state"
            flag = state / CONTROL.INTERVENTION_NAME
            CONTROL.write_state_text(flag, f"reason={held}\nboot_id=old\ntime=1.000\n")
            CONTROL.write_last_manual_state(state, 6, floor=2700, profile="EC 3.5.8")
            with patch.object(CONTROL, "STALL_MARKER_DIR", state):
                code = CONTROL.apply_manual_floor(
                    root / "thermal", root / "hwmon", defaults={}, state_dir=state,
                    wait_seconds=0, monotonic=lambda: 0.0, sleeper=lambda delay: None,
                )
            self.assertEqual(code, 1)
            self.assertFalse(flag.exists())
            self.assertTrue((state / CONTROL.MANUAL_STATE_NAME).exists())
            self.assertFalse(flag.exists())

            CONTROL.write_state_text(flag, f"reason={reason}\nboot_id=old\ntime=1.000\n")
            with patch.object(CONTROL, "STALL_MARKER_DIR", state):
                code, stdout, stderr = run_cli([
                    "--thermal-root", str(root / "thermal"),
                    "--hwmon-root", str(root / "hwmon"),
                    "daemon",
                ])
            self.assertEqual(code, CONTROL.UNSUPPORTED_FIRMWARE_EXIT)
            self.assertNotIn("NEEDS INTERVENTION:", stdout + stderr)
            self.assertFalse(flag.exists())

        stop = Event()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            flag = root / CONTROL.INTERVENTION_NAME
            CONTROL.write_state_text(flag, f"reason={reason}\nboot_id=old\ntime=1.000\n")
            with patch.object(CONTROL, "Event", return_value=stop), \
                 patch.object(CONTROL.signal, "signal"), \
                 patch.object(stop, "wait", side_effect=lambda delay: stop.set()):
                status = CONTROL.run_watchdog(
                    Path("/tmp/dgx-fan-missing"),
                    Path("/tmp/dgx-fan-missing"),
                    unit_state=lambda: (
                        "inactive", "enabled", CONTROL.UNSUPPORTED_FIRMWARE_EXIT,
                    ),
                    notifier=lambda _message: None,
                    runner=lambda action: 0,
                    monotonic=lambda: 0.0,
                    marker_path=root / "true-zero-reboot",
                    boot_id="boot-a",
                    defaults={},
                )
            self.assertEqual(status, 0)
            self.assertFalse(flag.exists())

    def test_ec_update_block_is_informational_and_never_fails(self):
        guid = CONTROL.EC_FWUPD_GUID
        soc = "b488217b-3895-4fc0-b1bf-ab7005a2d45a"
        other = "dd1a238a-5f8e-46bd-9401-a88da99c5a96"
        devices = {
            "Devices": [
                {
                    "Name": "UEFI Device Firmware",
                    "Guid": [soc],
                    "Version": "0x02009e0f",
                },
                {
                    "Name": "UEFI Device Firmware",
                    "Guid": [guid],
                    "Version": "0x03000508",
                    "Summary": (
                        "UEFI System Resource Table device "
                        "(Updated via capsule-on-disk)"
                    ),
                },
                {
                    "Name": "UEFI Device Firmware",
                    "Guid": [other],
                    "Version": "0x00000519",
                },
            ]
        }
        text = json.dumps(devices)
        self.assertEqual(CONTROL.ec_release_name("0x03000508"), "3.5.8")
        self.assertEqual(CONTROL.ec_release_name("0x03000d07"), "3.13.7")
        self.assertEqual(CONTROL.ec_version_in_devices(text), "3.5.8")
        status, detail = CONTROL.classify_ec_update_block(
            devices_text=text, devices_ok=True, verbose_text=None, log_text="",
        )
        self.assertEqual(status, "warn")
        self.assertIn("not blocked", detail)
        self.assertIn("3.5.8", detail)
        self.assertNotEqual(status, "fail")

        devices["Devices"][1]["Version"] = "0x03000d07"
        status, detail = CONTROL.classify_ec_update_block(
            devices_text=json.dumps(devices), devices_ok=True,
            verbose_text=None, log_text="pinned EC 3.5.8 capabilities\n",
        )
        self.assertEqual((status, detail), ("pass", "not blocked (EC 3.13.7)"))

        soc_only = json.dumps({"Devices": [devices["Devices"][0], devices["Devices"][2]]})
        verbose = (
            "FuEngine device 8c948e1db381648c8893897e4d09b7b153309991 "
            "[UEFI Device Firmware] is disabled "
            f"[{guid}], ignoring from uefi_capsule\n"
        )
        status, detail = CONTROL.classify_ec_update_block(
            devices_text=soc_only, devices_ok=True, verbose_text=verbose,
            log_text="",
        )
        self.assertEqual((status, detail), ("pass", "blocked"))
        status, detail = CONTROL.classify_ec_update_block(
            devices_text=soc_only, devices_ok=True, verbose_text="",
            log_text="pinned EC 3.5.8 capabilities fan0=1260..9000\n",
        )
        self.assertEqual(status, "pass")
        self.assertIn("likely blocked", detail)
        status, detail = CONTROL.classify_ec_update_block(
            devices_text=soc_only, devices_ok=True, verbose_text="", log_text="",
        )
        self.assertEqual(status, "note")
        self.assertTrue(detail.startswith("unknown"))
        self.assertEqual(detail, "unknown (EC not in get-devices)")
        status, detail = CONTROL.classify_ec_update_block(
            devices_text=soc_only, devices_ok=True, verbose_text="", log_text="",
            as_root=False,
        )
        self.assertEqual(
            (status, detail), ("note", "unknown (needs root to confirm the block)"),
        )
        status, detail = CONTROL.classify_ec_update_block(
            devices_text=soc_only, devices_ok=True, verbose_text=verbose,
            log_text="", as_root=False,
        )
        self.assertEqual((status, detail), ("pass", "blocked"))

        def absent_as_user(argv):
            if argv[0] == "fwupdmgr":
                return subprocess.CompletedProcess(argv, 0, soc_only, "")
            return subprocess.CompletedProcess(argv, 1, "", "Permission denied")

        self.assertEqual(
            CONTROL.read_ec_update_block("", run=absent_as_user, as_root=False),
            ("note", "unknown (needs root to confirm the block)"),
        )
        self.assertEqual(
            CONTROL.read_ec_update_block("", run=absent_as_user, as_root=True),
            ("note", "unknown (EC not in get-devices)"),
        )
        status, detail = CONTROL.classify_ec_update_block(
            devices_text=None, devices_ok=False, verbose_text=None, log_text="",
            timed_out=True,
        )
        self.assertEqual(detail, "unknown (fwupd timed out)")
        status, detail = CONTROL.classify_ec_update_block(
            devices_text=None, devices_ok=False, verbose_text=None, log_text="",
            fwupd_missing=True,
        )
        self.assertEqual(detail, "unknown (fwupd not installed)")

        calls = []

        def timed_out(argv):
            calls.append(argv)
            raise subprocess.TimeoutExpired(argv, 5)

        status, detail = CONTROL.read_ec_update_block("", run=timed_out)
        self.assertEqual(calls, [["fwupdmgr", "get-devices", "--json"]])
        self.assertEqual((status, detail), ("note", "unknown (fwupd timed out)"))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._healthy_tree(root)
            lines = []
            problems = CONTROL.collect_health_checks(
                root / "thermal", root / "hwmon",
                module_loaded=True, service_active="inactive",
                service_enabled="disabled", exec_status=None,
                watchdog_active="active", soc_celsius=85, log_text="",
                ready="not configured", emit=lines.append,
                ec_block=("warn", "not blocked; EC 3.5.8 can be replaced by 3.13.7"),
            )
            self.assertEqual(problems, [])
            self.assertIn(
                "ec-updates: warn (not blocked; EC 3.5.8 can be replaced by 3.13.7)",
                lines,
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code, _alerts, _notices = self._health(
                    root, {},
                    self._healthy_snapshot(ec_updates=(
                        "warn",
                        "not blocked; EC 3.5.8 can be replaced by 3.13.7",
                    )),
                    record=True,
                )
            self.assertEqual(code, 0)
            self.assertIn("ec-updates: warn", stdout.getvalue())
            self.assertFalse((root / "state" / CONTROL.INTERVENTION_NAME).exists())

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
