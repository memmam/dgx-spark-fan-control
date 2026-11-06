#!/usr/bin/env python3
"""Smooth userland policy for the DGX Spark additive fan-floor device."""

from __future__ import annotations

import argparse
import errno
import logging
import math
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from threading import Event
from pathlib import Path

COOLING_DEVICE_TYPE = "dgx_ec_fan_floor"
ACPI_COOLING_TYPE = "Fan"
HWMON_DEVICE_NAME = "dgx_ec_fan"
ACPI_FAN_NAME = "acpi_fan"
ACPI_MIN_LEVEL = 40
ACPI_MAX_LEVEL = 100
CONTROL_UNSUPPORTED = "control unsupported on this firmware"
WATCHDOG_POLL_SECONDS = 2.0
WATCHDOG_ZERO_SECONDS = 10.0
WATCHDOG_SOC_CELSIUS = 70.0
WATCHDOG_DEFAULT_ACTION = "systemctl poweroff"
DAEMON_UNIT = "dgx-fan-control.service"
DAEMON_ALERT_INTERVAL_SECONDS = 60.0
MODULE_NAME = "dgx_ec_fan_control"
MANAGED_BLACKLIST_NAME = "dgx-spark-fan-control-disable.conf"
MODULES_LOAD_NAME = "dgx_ec_fan_control.conf"
MANAGED_BLACKLIST_TEXT = (
    "# Managed by dgx-fan-control module disable. Do not edit.\n"
    "# Remove with: dgx-fan-control module enable\n"
    "blacklist dgx_ec_fan_control\n"
    "install dgx_ec_fan_control /bin/false\n"
)
MODULES_LOAD_TEXT = (
    "# Load the exact-kernel, locally signed additive fan-floor driver at boot.\n"
    "dgx_ec_fan_control\n"
)
MODULES_LOAD_DISABLED_TEXT = (
    "# Disabled by dgx-fan-control module disable.\n"
    "# Restore with: dgx-fan-control module enable\n"
    "# dgx_ec_fan_control\n"
)
MAX_STATE = 12
MIN_CURVE_STATE = 1
POLL_SECONDS = 2.0
FILTER_ALPHA = 0.35
DOWN_HYSTERESIS_C = 4.0
UP_STATES_PER_POLL = 2
DOWN_STATES_PER_POLL = 1
TRANSPORT_FAILURE_LIMIT = 3
RETRY_INITIAL_SECONDS = 2.0
# Reply-guard refusal is EPROTO. The driver has already started its 30s
# recovery cooldown, so a shorter retry cannot succeed. Idle-guard refusal
# stays EAGAIN and keeps the 2s/4s backoff.
RECOVERY_COOLDOWN_SECONDS = 31.0
CONTROLLER_UNAVAILABLE_EXIT = 69
UNSUPPORTED_FIRMWARE_EXIT = 69
RETRYABLE_ERRNOS = frozenset((
    errno.EBUSY, errno.EAGAIN, errno.EINTR, errno.EIO,
    errno.ETIMEDOUT, errno.EREMOTEIO, errno.EPROTO,
))

# Temperatures are unchanged. State indexes are shared by every capability
# profile: the kernel maps them to that profile's floor table. State 0 is
# NVIDIA's curve and is never selected while this process is running. A lower
# clamp of 0 has been observed to latch with the fans stopped. State 1 is the
# first additive floor of the active profile. State 12 is that profile's
# reported maximum. Stopping the daemon still writes state 0.
PERFORMANCE_CURVE = (
    (0.0, 1),
    (50.0, 3),
    (55.0, 5),
    (60.0, 8),
    (65.0, 10),
    (70.0, 12),
)

# Opt-in quieter curve from tenhkspark, upstream PR #2 and
# tenhkspark/dgx-spark-fan-tuning patch/0001-performance-curve.patch.
# For EC 3.5.8 with the pre-October SoC (validation image 2.155.11) when
# less noise is worth higher temperatures. State 1 is still the floor.
QUIET_CURVE = (
    (0.0, 1),
    (52.0, 2),
    (57.0, 3),
    (62.0, 4),
    (66.0, 5),
    (70.0, 6),
    (73.0, 8),
    (76.0, 10),
    (78.0, 12),
)
CURVES = {
    "performance": PERFORMANCE_CURVE,
    "quiet": QUIET_CURVE,
}

LOG = logging.getLogger("dgx-fan-control")


def mark_reported(exc: BaseException) -> None:
    """Remember that this failure is already in the process log.

    main logs unreported exceptions once. Repeating one that a command
    already logged puts the same event on the terminal and in the journal
    a second time.
    """
    setattr(exc, "_dgx_reported", True)


def was_reported(exc: BaseException) -> bool:
    return bool(getattr(exc, "_dgx_reported", False))


class ControlUnsupported(RuntimeError):
    """Hwmon telemetry is bound and the EC is not accepting a floor."""


def cooling_devices(thermal_root: Path) -> list[Path]:
    matches = []
    for candidate in sorted(thermal_root.glob("cooling_device*")):
        try:
            device_type = (candidate / "type").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            continue
        if device_type == COOLING_DEVICE_TYPE:
            matches.append(candidate)
    return matches


def find_cooling_device(thermal_root: Path) -> Path:
    matches = cooling_devices(thermal_root)
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one {COOLING_DEVICE_TYPE!r} cooling device, found {len(matches)}"
        )
    return matches[0]


def find_fan_hwmon(hwmon_root: Path) -> Path | None:
    matches = []
    for candidate in sorted(hwmon_root.glob("hwmon*")):
        try:
            name = (candidate / "name").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            continue
        if name == HWMON_DEVICE_NAME:
            matches.append(candidate)
    if len(matches) > 1:
        raise RuntimeError(
            f"expected at most one {HWMON_DEVICE_NAME!r} hwmon device, found {len(matches)}"
        )
    return matches[0] if matches else None


def read_thermal_zone_c(thermal_root: Path, zone: str = "thermal_zone0") -> float:
    text = (thermal_root / zone / "temp").read_text(encoding="ascii").strip()
    millidegrees = int(text)
    if not 0 <= millidegrees <= 200_000:
        raise RuntimeError(f"{zone} temperature is outside the accepted range")
    return millidegrees / 1000.0


def read_named_fan_rpms(hwmon_root: Path, device_name: str = ACPI_FAN_NAME) -> list[int]:
    """RPM inputs for one hwmon name. Does not require this project's module."""
    readings = []
    for device in sorted(hwmon_root.glob("hwmon*")):
        try:
            name = (device / "name").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            continue
        if name != device_name:
            continue
        for path in sorted(device.glob("fan*_input")):
            readings.append(int(path.read_text(encoding="ascii").strip()))
    return readings


def fans_stalled(fan_rpms: list[int], soc_c: float, held_seconds: float, *,
                 soc_limit_c: float = WATCHDOG_SOC_CELSIUS,
                 zero_seconds: float = WATCHDOG_ZERO_SECONDS) -> bool:
    """True only after every fan has read 0 RPM for longer than zero_seconds."""
    if len(fan_rpms) < 2 or any(rpm != 0 for rpm in fan_rpms):
        return False
    if not math.isfinite(soc_c) or soc_c < soc_limit_c:
        return False
    if not math.isfinite(held_seconds) or not math.isfinite(zero_seconds):
        return False
    return held_seconds > zero_seconds


def systemctl_query(verb: str, unit: str = DAEMON_UNIT) -> str:
    try:
        completed = subprocess.run(
            ["systemctl", verb, unit],
            check=False, capture_output=True, text=True,
        )
    except OSError:
        return "unknown"
    lines = (completed.stdout or "").strip().splitlines()
    return lines[0] if lines else "unknown"


def systemctl_is_active(unit: str = DAEMON_UNIT) -> str:
    return systemctl_query("is-active", unit)


def read_daemon_unit(unit: str = DAEMON_UNIT) -> tuple[str, str, int | None]:
    return (
        systemctl_query("is-active", unit),
        systemctl_query("is-enabled", unit),
        systemctl_exec_main_status(unit),
    )


def systemctl_show_properties(unit: str, names: tuple[str, ...]) -> dict[str, str]:
    command = ["systemctl", "show"]
    for name in names:
        command.extend(["-p", name])
    command.append(unit)
    try:
        completed = subprocess.run(
            command, check=False, capture_output=True, text=True,
        )
    except OSError:
        return {}
    found: dict[str, str] = {}
    for line in (completed.stdout or "").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            found[key] = value.strip()
    return found


def _show_int(properties: dict[str, str], name: str) -> int | None:
    text = properties.get(name, "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def read_exec_main(unit: str = DAEMON_UNIT) -> tuple[int | None, str | None, int | None, int | None]:
    """Exit code, wall-clock exit time, and start/exit monotonic microseconds.

    A monotonic value of 0 means systemd has no such timestamp. ExecMainStatus
    is also 0 when the main process has never exited, so callers must not
    treat a bare 0 as a completed run.
    """
    properties = systemctl_show_properties(unit, (
        "ExecMainStatus",
        "ExecMainExitTimestamp",
        "ExecMainStartTimestampMonotonic",
        "ExecMainExitTimestampMonotonic",
    ))
    timestamp = properties.get("ExecMainExitTimestamp", "").strip()
    if timestamp in {"", "n/a", "0"}:
        timestamp = None
    return (
        _show_int(properties, "ExecMainStatus"),
        timestamp,
        _show_int(properties, "ExecMainStartTimestampMonotonic"),
        _show_int(properties, "ExecMainExitTimestampMonotonic"),
    )


def systemctl_exec_main_status(unit: str = DAEMON_UNIT) -> int | None:
    status, _timestamp, _start_us, _exit_us = read_exec_main(unit)
    return status


def service_exit_is_current(active: str, enabled: str, exec_status: int | None = None,
                            exit_timestamp: str | None = None,
                            start_monotonic_us: int | None = None,
                            exit_monotonic_us: int | None = None) -> bool:
    """True when this inactive or failed state is the main process's own exit.

    A disabled unit that is not failed has not run since it was disabled, so
    a leftover ExecMainStatus is not current. An active unit is still running.
    Enabled and inactive counts only after the main process has exited since
    it last started.
    """
    if exec_status is None:
        return False
    if active == "failed":
        return True
    if active == "active" or enabled != "enabled":
        return False
    if not exit_timestamp or exit_monotonic_us == 0:
        return False
    if (
        start_monotonic_us not in (None, 0)
        and exit_monotonic_us not in (None, 0)
        and exit_monotonic_us < start_monotonic_us
    ):
        return False
    return True


def format_service_exit(active: str, enabled: str, exec_status: int | None = None,
                        exit_timestamp: str | None = None,
                        start_monotonic_us: int | None = None,
                        exit_monotonic_us: int | None = None) -> str:
    """n/a, a bare code, or '<code> at <ExecMainExitTimestamp>'."""
    if not service_exit_is_current(
        active, enabled, exec_status, exit_timestamp,
        start_monotonic_us, exit_monotonic_us,
    ):
        return "n/a"
    if exit_timestamp:
        return f"{exec_status} at {exit_timestamp}"
    return str(exec_status)


def daemon_is_down(state: str) -> bool:
    return state != "active"


def daemon_needs_alarm(active: str, enabled: str = "enabled",
                       exec_status: int | None = None) -> bool:
    """Alarm unless the unit is running, deliberately stopped, or unsupported.

    A failed unit always alarms. Enabled but inactive alarms when the last
    exit is not the startup unsupported-firmware code. Disabled and inactive
    is ACPI-only mode and does not alarm. Exit 69 is only that startup check.
    """
    if active == "active":
        return False
    if active == "failed":
        return True
    if exec_status == UNSUPPORTED_FIRMWARE_EXIT:
        return False
    if enabled == "disabled" and active == "inactive":
        return False
    return True


def split_unit_state(reported: str | tuple) -> tuple[str, str, int | None]:
    if isinstance(reported, tuple):
        if len(reported) >= 3:
            return reported[0], reported[1], reported[2]
        if len(reported) == 2:
            return reported[0], reported[1], None
    return reported, "enabled", None


def broadcast_alert(message: str) -> None:
    subprocess.run(["wall", message], check=False, capture_output=True)


def run_watchdog_action(action: str) -> int:
    argv = shlex.split(action)
    if not argv:
        raise ValueError("watchdog action is empty")
    completed = subprocess.run(argv, check=False)
    return completed.returncode


def run_watchdog(thermal_root: Path, hwmon_root: Path, *,
                 poll_seconds: float = WATCHDOG_POLL_SECONDS,
                 zero_seconds: float = WATCHDOG_ZERO_SECONDS,
                 soc_celsius: float = WATCHDOG_SOC_CELSIUS,
                 action: str = WATCHDOG_DEFAULT_ACTION,
                 monotonic=None, runner=None, unit_state=None, notifier=None) -> int:
    """Read ACPI fan tachometers and thermal_zone0. Never writes a fan control."""
    if not math.isfinite(poll_seconds) or poll_seconds <= 0:
        raise ValueError("poll interval must be finite and positive")
    if not math.isfinite(zero_seconds) or zero_seconds < 0:
        raise ValueError("zero interval must be finite and non-negative")
    if not math.isfinite(soc_celsius):
        raise ValueError("SoC threshold must be finite")
    if monotonic is None:
        monotonic = time.monotonic
    if runner is None:
        runner = run_watchdog_action
    if unit_state is None:
        unit_state = read_daemon_unit
    if notifier is None:
        notifier = broadcast_alert
    stop_requested = Event()

    def request_stop(signum: int, frame: object) -> None:
        stop_requested.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    LOG.critical(
        "fan-stall watchdog watching ACPI fans and thermal_zone0; "
        "0 RPM on every fan for more than %.0fs at >=%.0fC runs %s",
        zero_seconds, soc_celsius, action,
    )
    fault_since: float | None = None
    announced_shortage = False
    last_daemon_alert: float | None = None
    reported_unsupported = False
    while not stop_requested.is_set():
        # A dead daemon can leave a low floor while temperatures stay under
        # every stall threshold. Check it before those thresholds.
        active_state, enabled_state, exec_status = split_unit_state(unit_state())
        if daemon_needs_alarm(active_state, enabled_state, exec_status):
            now = monotonic()
            if (last_daemon_alert is None or
                    now - last_daemon_alert >= DAEMON_ALERT_INTERVAL_SECONDS):
                message = (
                    f"dgx-fan-control: {DAEMON_UNIT} is not active "
                    f"({active_state}, {enabled_state})"
                )
                # One journal line. wall is a separate tty broadcast for
                # logged-in users, not a second copy on stdout or stderr.
                LOG.critical(message)
                try:
                    notifier(message)
                except OSError:
                    LOG.warning("could not broadcast the fan-daemon alert")
                last_daemon_alert = now
        else:
            last_daemon_alert = None
            if (exec_status == UNSUPPORTED_FIRMWARE_EXIT and
                    not reported_unsupported):
                LOG.warning(
                    "dgx-fan-control: %s last exited %s "
                    "(fan control unsupported); not alerting",
                    DAEMON_UNIT, exec_status,
                )
                reported_unsupported = True
        try:
            fan_rpms = read_named_fan_rpms(hwmon_root)
            soc_c = read_thermal_zone_c(thermal_root)
        except (OSError, UnicodeError, ValueError, RuntimeError):
            LOG.exception("fan-stall observation failed; not treating it as a stall")
            fault_since = None
        else:
            if len(fan_rpms) < 2:
                if not announced_shortage:
                    LOG.warning(
                        "fan-stall watchdog sees %d ACPI fan input(s); "
                        "it will not trip until two are present",
                        len(fan_rpms),
                    )
                    announced_shortage = True
                fault_since = None
            elif all(rpm == 0 for rpm in fan_rpms) and soc_c >= soc_celsius:
                now = monotonic()
                if fault_since is None:
                    fault_since = now
                held = now - fault_since
                if fans_stalled(fan_rpms, soc_c, held, soc_limit_c=soc_celsius,
                                zero_seconds=zero_seconds):
                    LOG.critical(
                        "fan stall: ACPI fans read %s RPM for %.1fs while "
                        "thermal_zone0 is %.1fC; running %s",
                        fan_rpms, held, soc_c, action,
                    )
                    status = runner(action)
                    if status != 0:
                        LOG.critical("fan-stall action exited %s", status)
                        return status if isinstance(status, int) else 1
                    return 0
            else:
                fault_since = None
                announced_shortage = False
        if stop_requested.is_set():
            break
        stop_requested.wait(poll_seconds)
    LOG.info("fan-stall watchdog stopped")
    return 0


def missing_cooling_reason(module_loaded: bool, log_text: str) -> str:
    """Why status has no dgx_ec_fan_floor device. Never the bare count 'found 0'."""
    if not module_loaded:
        return (
            "no dgx_ec_fan_floor cooling device: kernel module "
            "dgx_ec_fan_control is not loaded"
        )
    if "refusing to replace existing lower floor" in log_text:
        return (
            "no dgx_ec_fan_floor cooling device: probe refused a foreign lower floor. "
            "ACPI cur_state writes the same EC register. This driver will not clear it. "
            "Do not write ACPI level 0; it stops both fans and does not clear the floor. "
            "Unload the module before using ACPI."
        )
    if "EC mailbox did not complete" in log_text:
        return (
            "no dgx_ec_fan_floor cooling device: the EC mailbox did not complete. "
            "On EC 3.5.8 the ACPI Fan device is the fallback when that happens. "
            "Do not write an ACPI level below 30; level 0 stops both fans."
        )
    return "no dgx_ec_fan_floor cooling device: probe did not register one"


def require_control(thermal_root: Path, hwmon_root: Path, *,
                    module_loaded: bool | None = None,
                    log_text: str | None = None) -> Path:
    matches = cooling_devices(thermal_root)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise RuntimeError(
            f"expected one {COOLING_DEVICE_TYPE!r} cooling device, found {len(matches)}"
        )
    if find_fan_hwmon(hwmon_root) is not None:
        raise ControlUnsupported(CONTROL_UNSUPPORTED)
    if module_loaded is None:
        module_loaded = module_is_loaded()
    if log_text is None:
        log_text = read_kernel_log()
    raise RuntimeError(missing_cooling_reason(module_loaded, log_text))


def read_hottest_temperature_c(thermal_root: Path) -> float:
    readings = []
    for path in sorted(thermal_root.glob("thermal_zone*/temp")):
        try:
            millidegrees = int(path.read_text(encoding="ascii").strip())
        except (OSError, UnicodeError, ValueError):
            continue
        if 0 < millidegrees <= 200_000:
            readings.append(millidegrees / 1000.0)
    if not readings:
        raise RuntimeError("no valid kernel thermal-zone readings are available")
    return max(readings)


def curve_name(explicit: str | None = None) -> str:
    raw = explicit if explicit else os.environ.get("DGX_FAN_CURVE", "performance")
    name = raw.strip().lower()
    if name not in CURVES:
        raise ValueError(f"unknown fan curve {raw!r}; choose performance or quiet")
    return name


def curve_state(temperature_c: float, curve: tuple = PERFORMANCE_CURVE) -> int:
    if not math.isfinite(temperature_c) or temperature_c <= 0:
        raise ValueError("temperature must be finite and positive")
    state = 0
    for threshold_c, threshold_state in curve:
        if threshold_state < MIN_CURVE_STATE:
            raise ValueError("performance curve requested state 0")
        if temperature_c < threshold_c:
            break
        state = threshold_state
    return max(state, MIN_CURVE_STATE)


def retry_delay_seconds(exc: OSError, failures: int) -> float:
    if exc.errno == errno.EPROTO:
        return RECOVERY_COOLDOWN_SECONDS
    return RETRY_INITIAL_SECONDS * 2 ** (failures - 1)


def next_state(current: int, filtered_temperature_c: float,
               curve: tuple = PERFORMANCE_CURVE) -> int:
    """Rate-limit the curve, with prompt increases and slow hysteretic falls."""
    if not 0 <= current <= MAX_STATE:
        raise ValueError("current cooling state is outside the driver contract")
    rising_target = curve_state(filtered_temperature_c, curve)
    if rising_target > current:
        chosen = min(rising_target, current + UP_STATES_PER_POLL)
    else:
        falling_target = curve_state(filtered_temperature_c + DOWN_HYSTERESIS_C, curve)
        if falling_target < current:
            chosen = max(falling_target, current - DOWN_STATES_PER_POLL)
        else:
            chosen = current
    return max(chosen, MIN_CURVE_STATE)


def read_kernel_log() -> str:
    try:
        completed = subprocess.run(
            ["dmesg"], check=False, capture_output=True, text=True,
        )
    except OSError:
        return ""
    return completed.stdout or ""


def latest_floor_mismatch(log_text: str) -> tuple[int | None, int | None]:
    """RPM pair from the newest ownership-mismatch line, if the log has one."""
    observed = expected = None
    for match in re.finditer(
        r"floor ownership mismatch: observed=0x[0-9a-fA-F]+ \((\d+) RPM\)"
        r"(?: confirmed=0x[0-9a-fA-F]+ \((\d+) RPM\))?",
        log_text,
    ):
        observed = int(match.group(1))
        expected = int(match.group(2)) if match.group(2) else None
    return observed, expected


def floor_changed_message(observed: int | None = None,
                          expected: int | None = None) -> str:
    """ESTALE is the EC floor moving under this driver, usually from ACPI."""
    if observed is not None and expected is not None:
        numbers = f"observed {observed} RPM, expected {expected}"
    elif observed is not None:
        numbers = f"observed {observed} RPM"
    else:
        numbers = "the EC floor does not match the floor this driver set"
    return (
        f"floor was changed outside this driver ({numbers}); likely ACPI cur_state. "
        "Recovery: stop the daemon and unload the module with "
        "'dgx-fan-control module disable' before any ACPI write. "
        "This driver will not clear a floor it does not own. "
        "Do not write ACPI level 0; it stops both fans and does not clear the floor."
    )


def normalize_cooling_error(exc: OSError) -> Exception:
    """Turn a sysfs ESTALE into one sentence. Other OSErrors pass through."""
    if exc.errno != errno.ESTALE:
        return exc
    observed, expected = latest_floor_mismatch(read_kernel_log())
    return RuntimeError(floor_changed_message(observed, expected))


def read_state(cooling_device: Path) -> int:
    try:
        state = int((cooling_device / "cur_state").read_text(encoding="ascii").strip())
        maximum = int((cooling_device / "max_state").read_text(encoding="ascii").strip())
    except OSError as exc:
        raise normalize_cooling_error(exc) from None
    if maximum != MAX_STATE or not 0 <= state <= maximum:
        raise RuntimeError(
            f"unexpected cooling-device contract: state={state} max_state={maximum}"
        )
    return state


def write_state(cooling_device: Path, state: int) -> None:
    if not 0 <= state <= MAX_STATE:
        raise ValueError(f"state must be between 0 and {MAX_STATE}")
    try:
        (cooling_device / "cur_state").write_text(f"{state}\n", encoding="ascii")
    except OSError as exc:
        raise normalize_cooling_error(exc) from None
    observed = read_state(cooling_device)
    if observed != state:
        raise RuntimeError(f"cooling-state readback mismatch: wrote {state}, read {observed}")


def format_cooling_status(state: int, hottest_c: float) -> str:
    """State 0 is the EC curve. The label says so, so it does not read as fans off."""
    text = f"state={state}/{MAX_STATE}"
    if state == 0:
        text += " (no floor; EC curve)"
    return f"{text} hottest={hottest_c:.1f}C"


def run_daemon(thermal_root: Path, poll_seconds: float,
               hwmon_root: Path | None = None, curve: str | None = None) -> int:
    if not math.isfinite(poll_seconds) or poll_seconds <= 0:
        raise ValueError("poll interval must be finite and positive")
    if hwmon_root is None:
        hwmon_root = Path("/sys/class/hwmon")
    selected = CURVES[curve_name(curve)]
    cooling_device = require_control(thermal_root, hwmon_root)
    filtered_temperature_c: float | None = None
    stop_requested = Event()
    failures = 0
    primary_failure = False

    def request_stop(signum: int, frame: object) -> None:
        stop_requested.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    LOG.info(
        "controller starting curve=%s; authenticating the current cooling state",
        curve_name(curve),
    )

    try:
        while not stop_requested.is_set():
            try:
                # Suspend, an EC reset, or an uncertain write may change state.
                # Read it on every pass before deciding that no update is needed.
                current = read_state(cooling_device)
                try:
                    hottest = read_hottest_temperature_c(thermal_root)
                    if filtered_temperature_c is None:
                        filtered_temperature_c = hottest
                    else:
                        filtered_temperature_c += FILTER_ALPHA * (
                            hottest - filtered_temperature_c
                        )
                    desired = next_state(current, filtered_temperature_c, selected)
                except (OSError, RuntimeError, ValueError) as exc:
                    LOG.error(
                        "temperature observation failed; requesting maximum cooling: %s",
                        exc,
                    )
                    hottest = math.nan
                    filtered_temperature_c = None
                    desired = MAX_STATE

                if desired != current:
                    write_state(cooling_device, desired)
                    LOG.info(
                        "cooling state %d -> %d (hottest=%.1fC filtered=%s)",
                        current,
                        desired,
                        hottest,
                        "unavailable"
                        if filtered_temperature_c is None
                        else f"{filtered_temperature_c:.1f}C",
                    )
                if failures:
                    LOG.info("fan communication recovered after %d failed attempts", failures)
                failures = 0
            except RuntimeError as exc:
                if "floor was changed outside this driver" not in str(exc):
                    raise
                LOG.error("%s", exc)
                mark_reported(exc)
                raise
            except OSError as exc:
                failures += 1
                filtered_temperature_c = None
                if exc.errno not in RETRYABLE_ERRNOS or failures >= TRANSPORT_FAILURE_LIMIT:
                    LOG.error(
                        "fan control unavailable after %d failed attempt(s): %s; "
                        "stopping for operator recovery, current floor is unverified",
                        failures, exc,
                    )
                    mark_reported(exc)
                    raise
                delay = retry_delay_seconds(exc, failures)
                LOG.warning(
                    "fan communication failed (%d/%d): %s; retrying in %.1fs",
                    failures, TRANSPORT_FAILURE_LIMIT, exc, delay,
                )
                stop_requested.wait(delay)
                continue
            stop_requested.wait(poll_seconds)
    except BaseException:
        primary_failure = True
        raise
    finally:
        try:
            write_state(cooling_device, 0)
        except Exception as exc:
            # The ownership failure is already logged. A second copy, or a
            # traceback, is the line operators saw as a bare ESTALE.
            duplicate = (
                primary_failure and "floor was changed outside this driver" in str(exc)
            )
            if not duplicate:
                LOG.error(
                    "automatic restoration failed; current fan floor is unverified: %s",
                    exc,
                )
            mark_reported(exc)
            if not primary_failure:
                raise
        else:
            LOG.info("automatic NVIDIA fan policy restored")
    return 0


def acpi_fan_devices(thermal_root: Path) -> list[Path]:
    matches = []
    for candidate in sorted(thermal_root.glob("cooling_device*")):
        try:
            device_type = (candidate / "type").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            continue
        if device_type == ACPI_COOLING_TYPE:
            matches.append(candidate)
    return matches


ACPI_RPM_NOTE = (
    "firmware may accept this write without changing RPM "
    "(EC 3.13.7 does); check fan RPM afterward"
)


def acpi_fan_result_line(device: Path) -> str:
    """cur_state/max_state after a write, for example 'cooling_device0 (Fan): 100/100'."""
    current = (device / "cur_state").read_text(encoding="ascii").strip()
    maximum = (device / "max_state").read_text(encoding="ascii").strip()
    kind = (device / "type").read_text(encoding="ascii").strip()
    return f"{device.name} ({kind}): {current}/{maximum}"


def write_acpi_fan_level(device: Path, level: int) -> None:
    maximum = int((device / "max_state").read_text(encoding="ascii").strip())
    if not 0 <= level <= maximum:
        raise RuntimeError(f"ACPI fan level {level} is outside max_state {maximum}")
    (device / "cur_state").write_text(f"{level}\n", encoding="ascii")
    observed = int((device / "cur_state").read_text(encoding="ascii").strip())
    if observed != level:
        raise RuntimeError(
            f"ACPI fan readback mismatch: wrote {level}, read {observed}"
        )


def pinned_ec_name(log_text: str) -> str | None:
    match = re.search(r"pinned (EC \d+\.\d+\.\d+) capabilities", log_text)
    return match.group(1) if match else None


def classify_fan_binding(thermal_root: Path, hwmon_root: Path) -> str:
    """supported, unsupported (telemetry only), or absent."""
    if cooling_devices(thermal_root):
        return "supported"
    if find_fan_hwmon(hwmon_root) is not None:
        return "unsupported"
    return "absent"


def describe_control(kind: str, profile: str | None = None) -> str:
    if kind == "supported":
        return "supported"
    if kind == "unsupported":
        if profile:
            return f"unsupported ({profile}, telemetry only)"
        return "unsupported (telemetry only)"
    return "unknown"


def module_is_loaded(module: str = MODULE_NAME, module_root: Path | None = None) -> bool:
    root = module_root if module_root is not None else Path("/sys/module")
    return (root / module).is_dir()


def uncommented_lines(text: str) -> list[str]:
    lines = []
    for raw in text.splitlines():
        stripped = raw.split("#", 1)[0].strip()
        if stripped:
            lines.append(stripped)
    return lines


def conf_blocks_module(text: str, module: str = MODULE_NAME) -> bool:
    """True for a blacklist line or an install override of this module."""
    for line in uncommented_lines(text):
        parts = line.split()
        if len(parts) < 2:
            continue
        if parts[0] == "blacklist" and module in parts[1:]:
            return True
        if parts[0] == "install" and parts[1] == module:
            return True
    return False


def text_selects_module(text: str, module: str = MODULE_NAME) -> bool:
    for line in uncommented_lines(text):
        if module in line.split():
            return True
    return False


def modules_load_is_project_file(text: str, module: str = MODULE_NAME) -> bool:
    """True when the file only loads this module, or only comments that name."""
    active = uncommented_lines(text)
    if active:
        return active == [module]
    return module in text


def foreign_blacklist_files(modprobe_dir: Path, module: str = MODULE_NAME) -> list[Path]:
    if not modprobe_dir.is_dir():
        return []
    found = []
    for path in sorted(modprobe_dir.glob("*.conf")):
        if path.name == MANAGED_BLACKLIST_NAME:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if conf_blocks_module(text, module):
            found.append(path)
    return found


def initramfs_module_files(etc: Path, module: str = MODULE_NAME) -> list[Path]:
    """Files under etc that would put this module in an early boot image."""
    found = []
    singles = (
        etc / "initramfs-tools" / "modules",
        etc / "dracut.conf",
    )
    directories = (
        etc / "initramfs-tools" / "modules.d",
        etc / "dracut.conf.d",
    )
    for path in singles:
        if path.is_file() and text_selects_module(_read_text(path), module):
            found.append(path)
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if path.is_file() and text_selects_module(_read_text(path), module):
                found.append(path)
    return found


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def write_text_if_changed(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        try:
            if path.read_text(encoding="utf-8") == text:
                return
        except (OSError, UnicodeError):
            pass
    path.write_text(text, encoding="utf-8")


def warn_about_foreign_blacklists(modprobe_dir: Path, *, on_enable: bool = False) -> list[Path]:
    foreign = foreign_blacklist_files(modprobe_dir)
    for path in foreign:
        if on_enable:
            LOG.warning(
                "%s blacklists or install-overrides %s. module enable will not "
                "edit or delete it, so it can still block loading",
                path, MODULE_NAME,
            )
        else:
            LOG.warning(
                "leaving %s in place; it blacklists or install-overrides %s "
                "and was not created by this command",
                path, MODULE_NAME,
            )
    return foreign


def note_initramfs(etc: Path) -> list[Path]:
    listed = initramfs_module_files(etc)
    if not listed:
        return []
    names = ", ".join(str(path) for path in listed)
    LOG.warning(
        "%s is listed for the initramfs in %s. This project does not install "
        "it there and does not run update-initramfs or dracut. After changing "
        "mode, rebuild that image (sudo update-initramfs -u, or sudo dracut -f) "
        "or early boot can still load the module.",
        MODULE_NAME, names,
    )
    return listed


def module_paths(etc: Path) -> tuple[Path, Path, Path]:
    modprobe_dir = etc / "modprobe.d"
    modules_load = etc / "modules-load.d" / MODULES_LOAD_NAME
    managed = modprobe_dir / MANAGED_BLACKLIST_NAME
    return modprobe_dir, modules_load, managed


def neutralize_modules_load(modules_load: Path) -> None:
    if not modules_load.exists():
        return
    text = _read_text(modules_load)
    if not modules_load_is_project_file(text):
        LOG.warning(
            "not editing %s; it is not the modules-load.d file this project installs",
            modules_load,
        )
        return
    write_text_if_changed(modules_load, MODULES_LOAD_DISABLED_TEXT)


def restore_modules_load(modules_load: Path) -> None:
    if modules_load.exists():
        text = _read_text(modules_load)
        if not modules_load_is_project_file(text):
            LOG.warning(
                "not editing %s; it is not the modules-load.d file this project installs",
                modules_load,
            )
            return
    write_text_if_changed(modules_load, MODULES_LOAD_TEXT)


def run_module_command(argv: list[str], runner) -> None:
    if runner is None:
        completed = subprocess.run(argv, check=False, capture_output=True, text=True)
        if completed.returncode == 0:
            return
        detail = (completed.stderr or completed.stdout or "").strip()
        lowered = detail.lower()
        if any(token in lowered for token in ("not currently loaded", "does not exist", "not found", "not loaded")):
            return
        raise RuntimeError(f"{' '.join(argv)} failed: {detail or completed.returncode}")
    runner(argv)


def disable_kernel_module(etc: Path, *, runner, loaded: bool) -> None:
    """Stop autoload and the service. Idempotent. Does not delete foreign files."""
    run_module_command(["systemctl", "disable", "--now", DAEMON_UNIT], runner)
    if loaded:
        run_module_command(["modprobe", "-r", MODULE_NAME], runner)
    _modprobe_dir, modules_load, managed = module_paths(etc)
    write_text_if_changed(managed, MANAGED_BLACKLIST_TEXT)
    neutralize_modules_load(modules_load)
    warn_about_foreign_blacklists(_modprobe_dir)
    note_initramfs(etc)


def enable_kernel_module(etc: Path, *, runner, loaded: bool,
                         control_supported: bool | None = None,
                         force_service: bool = False,
                         start_service: bool = False,
                         profile: str | None = None) -> None:
    """Undo a managed disable and load the module.

    The performance service is not enabled unless start_service or
    force_service is set. On a telemetry-only bind the service stays
    disabled unless force_service is set. Exit 69 is reserved for that
    startup check, not for later crashes.
    """
    modprobe_dir, modules_load, managed = module_paths(etc)
    if managed.is_file():
        managed.unlink()
    warn_about_foreign_blacklists(modprobe_dir, on_enable=True)
    restore_modules_load(modules_load)
    if not loaded:
        run_module_command(["modprobe", MODULE_NAME], runner)
    if control_supported is None:
        kind = classify_fan_binding(Path("/sys/class/thermal"), Path("/sys/class/hwmon"))
        supported = kind != "unsupported"
    else:
        supported = control_supported
    if not supported and profile is None:
        try:
            logged = subprocess.run(
                ["dmesg"], check=False, capture_output=True, text=True,
            )
            profile = pinned_ec_name(logged.stdout or "")
        except OSError:
            profile = None
    if not supported and not force_service:
        run_module_command(["systemctl", "disable", "--now", DAEMON_UNIT], runner)
        label = profile or "this firmware"
        # Printed once. Logging the same sentence as well showed it twice
        # on the EC 3.13.7 enable path.
        print(
            f"fan control unsupported on {label}; telemetry only, "
            "service left disabled; use acpi-max on EC 3.5.8"
        )
        note_initramfs(etc)
        return
    if not (start_service or force_service):
        print(
            "module loaded; dgx-fan-control.service was not enabled. "
            "Enable the performance curve only if you want it: "
            "sudo dgx-fan-control module enable --start-service"
        )
        note_initramfs(etc)
        return
    if not supported and force_service:
        LOG.warning("starting the service despite unsupported fan control")
    run_module_command(["systemctl", "enable", "--now", DAEMON_UNIT], runner)
    note_initramfs(etc)


def module_status(etc: Path, *, loaded: bool, service_active: str, service_enabled: str,
                  control: str = "unknown", exec_status: int | None = None,
                  exit_timestamp: str | None = None,
                  start_monotonic_us: int | None = None,
                  exit_monotonic_us: int | None = None) -> str:
    modprobe_dir, _modules_load, managed = module_paths(etc)
    foreign = foreign_blacklist_files(modprobe_dir)
    initramfs = initramfs_module_files(etc)
    managed_on = managed.is_file()
    if managed_on and not loaded and service_enabled == "disabled":
        mode = "ACPI-only"
    elif loaded or service_enabled == "enabled":
        mode = "module"
    else:
        mode = "ACPI-only"
    if managed_on and loaded:
        mode = "inconsistent"
    lines = [
        f"loaded: {'yes' if loaded else 'no'}",
        f"managed-blacklist: {'yes' if managed_on else 'no'}",
        "other-blacklists: " + (", ".join(str(path) for path in foreign) if foreign else "none"),
        f"service-enabled: {service_enabled}",
        f"service-active: {service_active}",
        f"control: {control}",
        f"mode: {mode}",
        "initramfs: " + (", ".join(str(path) for path in initramfs) if initramfs else "none"),
        "service-exit: " + format_service_exit(
            service_active, service_enabled, exec_status, exit_timestamp,
            start_monotonic_us, exit_monotonic_us,
        ),
    ]
    return "\n".join(lines) + "\n"


def acpi_result_note(profile: str | None) -> str:
    """3.13.7 accepts the write and ignores RPM. 3.5.8 does change RPM."""
    if profile == "EC 3.5.8":
        return "check fan RPM"
    return ACPI_RPM_NOTE


def acpi_write_refusal(module_loaded: bool, service_active: str) -> str | None:
    """ACPI cur_state and this module write the same EC lower-floor register."""
    if service_active == "active":
        return (
            "refusing ACPI write: dgx-fan-control.service is active and shares "
            "the EC lower-floor register with ACPI cur_state. "
            "Stop the service and unload the module first, or pass --force. "
            "Do not write ACPI level 0."
        )
    if module_loaded:
        return (
            "refusing ACPI write: kernel module dgx_ec_fan_control is loaded and "
            "shares the EC lower-floor register with ACPI cur_state. "
            "Run 'dgx-fan-control module disable' first, or pass --force. "
            "Do not write ACPI level 0."
        )
    return None


def set_acpi_fans(thermal_root: Path, level: int = ACPI_MAX_LEVEL,
                  module_loaded: bool | None = None,
                  service_active: str | None = None,
                  force: bool = False,
                  profile: str | None = None) -> None:
    """Set every ACPI Fan device. On a failed lower level, rewrite them to 100.

    Refuses while this module is loaded or its daemon is active, unless force
    is set. A level below 40 is refused before any write. Level 0 stops both
    fans. EC 3.13.7 accepts the ACPI write and ignores RPM.
    """
    if level < ACPI_MIN_LEVEL or level > ACPI_MAX_LEVEL:
        raise ValueError(
            f"refusing ACPI fan level {level}: this command only writes "
            f"{ACPI_MIN_LEVEL} through {ACPI_MAX_LEVEL}. "
            "Levels below 30 are unsafe. Level 0 stops both fans immediately "
            "and they stay stopped. Level 20 has stalled one fan. Do not write 0."
        )
    if module_loaded is None:
        module_loaded = module_is_loaded()
    if service_active is None:
        service_active = systemctl_is_active()
    refusal = acpi_write_refusal(module_loaded, service_active)
    if refusal and not force:
        raise RuntimeError(refusal)
    if refusal and force:
        LOG.warning(
            "forcing an ACPI write while the kernel module or the fan service "
            "is active. This overwrites the shared EC lower floor. "
            "Do not write level 0; it stops both fans."
        )
    devices = acpi_fan_devices(thermal_root)
    if not devices:
        raise RuntimeError("no ACPI Fan cooling device found")

    def fail_hot() -> None:
        if level == ACPI_MAX_LEVEL:
            return
        for device in devices:
            try:
                write_acpi_fan_level(device, ACPI_MAX_LEVEL)
            except (OSError, RuntimeError, ValueError) as exc:
                LOG.error(
                    "fail-hot ACPI write of %s failed: %s", ACPI_MAX_LEVEL, exc,
                )

    failures: list[BaseException] = []
    summaries: list[str] = []
    try:
        for device in devices:
            try:
                write_acpi_fan_level(device, level)
                summaries.append(acpi_fan_result_line(device))
            except (OSError, RuntimeError, ValueError) as exc:
                failures.append(exc)
        if failures:
            raise RuntimeError(
                f"ACPI fan update to {level} failed on {len(failures)} device(s)"
            )
        for line in summaries:
            print(line)
        print(acpi_result_note(profile))
    except BaseException:
        fail_hot()
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--thermal-root",
        type=Path,
        default=Path("/sys/class/thermal"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--hwmon-root",
        type=Path,
        default=Path("/sys/class/hwmon"),
        help=argparse.SUPPRESS,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    daemon = subparsers.add_parser("daemon", help="run the smooth performance curve")
    daemon.add_argument("--poll-seconds", type=float, default=POLL_SECONDS)
    daemon.add_argument(
        "--curve",
        choices=tuple(CURVES),
        default=None,
        help="performance (default) or quiet; DGX_FAN_CURVE overrides the default",
    )
    watchdog = subparsers.add_parser(
        "watchdog",
        help="read ACPI fans and power off if both stay at 0 RPM while hot",
    )
    watchdog.add_argument("--poll-seconds", type=float, default=WATCHDOG_POLL_SECONDS)
    watchdog.add_argument("--zero-seconds", type=float, default=WATCHDOG_ZERO_SECONDS)
    watchdog.add_argument("--soc-celsius", type=float, default=WATCHDOG_SOC_CELSIUS)
    watchdog.add_argument("--action", default=WATCHDOG_DEFAULT_ACTION)
    acpi_max = subparsers.add_parser(
        "acpi-max",
        help="set ACPI Fan cooling devices without this kernel module",
    )
    acpi_max.add_argument("--level", type=int, default=ACPI_MAX_LEVEL)
    acpi_max.add_argument(
        "--force",
        action="store_true",
        help="write ACPI even if this kernel module or its daemon is active",
    )
    module = subparsers.add_parser(
        "module",
        help="enable or disable the kernel module for ACPI-only fan control",
    )
    module.add_argument("action", choices=("disable", "enable", "status"))
    module.add_argument(
        "--force-service",
        action="store_true",
        help="start the daemon even when fan control is unsupported",
    )
    module.add_argument(
        "--start-service",
        action="store_true",
        help="enable and start the performance service; otherwise leave it disabled",
    )
    set_state = subparsers.add_parser("set-state", help="set one explicit cooling state")
    set_state.add_argument("state", type=int, choices=range(MAX_STATE + 1))
    subparsers.add_parser("automatic", help="restore firmware-automatic policy")
    subparsers.add_parser("status", help="show the cooling state and hottest sensor")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        if args.command == "daemon":
            return run_daemon(
                args.thermal_root, args.poll_seconds, args.hwmon_root, args.curve,
            )
        if args.command == "watchdog":
            return run_watchdog(
                args.thermal_root,
                args.hwmon_root,
                poll_seconds=args.poll_seconds,
                zero_seconds=args.zero_seconds,
                soc_celsius=args.soc_celsius,
                action=args.action,
            )
        if args.command == "acpi-max":
            set_acpi_fans(
                args.thermal_root, args.level, force=args.force,
                profile=pinned_ec_name(read_kernel_log()),
            )
            return 0
        if args.command == "module":
            if os.geteuid() != 0:
                raise PermissionError("dgx-fan-control module requires root")
            etc = Path("/etc")
            loaded = module_is_loaded()
            if args.action == "disable":
                disable_kernel_module(etc, runner=None, loaded=loaded)
                return 0
            if args.action == "enable":
                enable_kernel_module(
                    etc, runner=None, loaded=loaded,
                    force_service=args.force_service,
                    start_service=args.start_service,
                )
                return 0
            kind = classify_fan_binding(
                Path("/sys/class/thermal"), Path("/sys/class/hwmon"),
            )
            profile = None
            try:
                logged = subprocess.run(
                    ["dmesg"], check=False, capture_output=True, text=True,
                )
                profile = pinned_ec_name(logged.stdout or "")
            except OSError:
                profile = None
            exec_status, exit_timestamp, start_us, exit_us = read_exec_main()
            print(module_status(
                etc,
                loaded=loaded,
                service_active=systemctl_query("is-active"),
                service_enabled=systemctl_query("is-enabled"),
                control=describe_control(kind, profile),
                exec_status=exec_status,
                exit_timestamp=exit_timestamp,
                start_monotonic_us=start_us,
                exit_monotonic_us=exit_us,
            ), end="")
            return 0
        cooling_device = require_control(args.thermal_root, args.hwmon_root)
        if args.command == "set-state":
            write_state(cooling_device, args.state)
            return 0
        if args.command == "automatic":
            write_state(cooling_device, 0)
            return 0
        if args.command == "status":
            try:
                state = read_state(cooling_device)
            except RuntimeError as exc:
                if "floor was changed outside this driver" in str(exc):
                    print(exc)
                    return 1
                raise
            print(format_cooling_status(
                state, read_hottest_temperature_c(args.thermal_root),
            ))
            return 0
    except ControlUnsupported as exc:
        # status is interactive: print the reason once. Logging it as well
        # repeated the line (ERROR on stderr, then the same text on stdout).
        # The daemon and the write commands only log, so the journal has one
        # line. Exit 69 is only the daemon's startup check.
        if args.command == "status":
            print(CONTROL_UNSUPPORTED)
            return 0
        LOG.error("%s", exc)
        if args.command == "daemon":
            return UNSUPPORTED_FIRMWARE_EXIT
        return 1
    except (OSError, RuntimeError, ValueError) as exc:
        if args.command == "status" and (
            str(exc).startswith("no dgx_ec_fan_floor")
            or "floor was changed outside this driver" in str(exc)
        ):
            print(exc)
            return 1
        if not was_reported(exc):
            LOG.error("%s", exc)
        return 1
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
