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
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
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
# Any-temperature alert. Idle fans on EC 3.5.8 with the October SoC run near
# 2700/4050 RPM, so 0/0 is not a normal state. Fifteen seconds is several
# 2-second polls, long enough to ignore one missed tach sample, and short
# enough to announce a stop at 39°C well before a clean shutdown at 85°C.
WATCHDOG_STALL_SECONDS = 15.0
WATCHDOG_REBOOT_SECONDS = 60.0
WATCHDOG_SOC_CELSIUS = 85.0
WATCHDOG_NONZERO_CLEAR_SECONDS = 30.0
WATCHDOG_ALERT_REPEAT_SECONDS = 1800.0
WATCHDOG_MAX_DEFER_SECONDS = 1800.0
CHECK_TIMEOUT_SECONDS = 5.0
NEEDS_ROOT = "unknown (needs root)"
WATCHDOG_DEFAULT_ACTION = "systemctl poweroff"
WATCHDOG_REBOOT_ACTION = "systemctl reboot"
DAEMON_UNIT = "dgx-fan-control.service"
WATCHDOG_UNIT = "dgx-fan-watchdog.service"
DAEMON_ALERT_INTERVAL_SECONDS = 60.0
STALL_MARKER_DIR = Path("/var/lib/dgx-fan-control")
STALL_MARKER_NAME = "true-zero-reboot"
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
# Probe refused a floor this driver does not own. Restarting cannot fix it.
FOREIGN_FLOOR_EXIT = 78
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


class ForeignFloor(RuntimeError):
    """Probe refused a lower floor written from outside this driver."""


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
            timeout=CHECK_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return "timeout"
    except OSError as exc:
        if access_denied(exc):
            return NEEDS_ROOT
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
            timeout=CHECK_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
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
    if exec_status in (UNSUPPORTED_FIRMWARE_EXIT, FOREIGN_FLOOR_EXIT):
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


def read_boot_id(path: Path = Path("/proc/sys/kernel/random/boot_id")) -> str | None:
    try:
        text = path.read_text(encoding="ascii").strip()
    except OSError:
        return None
    return text or None


def read_stall_marker(path: Path) -> tuple[str, str, str] | None:
    """boot id, timestamp, and reason, or None when no reboot was recorded.

    A marker written before reasons existed is treated as a true-zero reboot.
    """
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except OSError:
        return None
    found = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if separator:
            found[key.strip()] = value.strip()
    boot_id = found.get("boot_id")
    timestamp = found.get("time")
    if not boot_id or not timestamp:
        return None
    return boot_id, timestamp, found.get("reason") or "true-zero"


def write_stall_marker(path: Path, boot_id: str, when: float, reason: str = "true-zero") -> None:
    ensure_state_dir(path.parent)
    path.write_text(
        f"boot_id={boot_id}\ntime={when:.3f}\nreason={reason}\n",
        encoding="ascii",
    )


INTERVENTION_NAME = "needs-intervention"
HEALTH_NAME = "health"
NOTIFY_DEFAULT = Path("/etc/default/dgx-fan-control")


def ensure_state_dir(path: Path) -> None:
    """Create the state directory and keep it traversable by later SSH logins."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o755)
    except OSError:
        return


def write_state_text(path: Path, text: str) -> None:
    """Write a state file as mode 0644 so a login can read the flag."""
    ensure_state_dir(path.parent)
    path.write_text(text, encoding="ascii")
    try:
        os.chmod(path, 0o644)
    except OSError:
        return


def write_intervention(path: Path, reason: str, boot_id: str | None, when: float) -> None:
    write_state_text(
        path,
        f"reason={reason}\nboot_id={boot_id or ''}\ntime={when:.3f}\n",
    )


def access_denied(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and exc.errno in (errno.EACCES, errno.EPERM)


def scandir_names(path: Path) -> list[str] | None:
    """Directory entry names, or None when the directory is not readable."""
    try:
        with os.scandir(path) as scanned:
            names = [entry.name for entry in scanned]
    except FileNotFoundError:
        return []
    except OSError as exc:
        if access_denied(exc):
            return None
        return []
    return sorted(names)


def read_intervention_reason(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="ascii")
    except FileNotFoundError:
        return None
    except OSError as exc:
        if access_denied(exc):
            return NEEDS_ROOT
        return None
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "reason" and value.strip():
            return value.strip()
    stripped = text.strip()
    return stripped or None


def intervention_banner(path: Path) -> str:
    reason = read_intervention_reason(path)
    if not reason:
        return ""
    return f"NEEDS INTERVENTION: {reason}\n"


def clear_stall_marker(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        return


PREVIOUS_REBOOT_NAME = "previous-watchdog-reboot"
RECOVERY_ANNOUNCED_NAME = "recovery-announced"
HEALTH_SETTLE_SECONDS = 45.0
HEALTH_POLL_SECONDS = 2.0


def remember_previous_reboot(directory: Path, boot_id: str | None,
                             marker: tuple[str, str, str] | None) -> None:
    """Keep a note when this boot follows a watchdog reboot.

    The stall marker itself is removed once the fans are nonzero. The health
    check still needs to know that this boot came after that reboot.
    """
    if not boot_id or marker is None or marker[0] == boot_id:
        return
    path = directory / PREVIOUS_REBOOT_NAME
    try:
        existing = path.read_text(encoding="ascii")
    except OSError:
        existing = ""
    if f"boot_id={boot_id}\n" in existing:
        return
    write_state_text(path, f"boot_id={boot_id}\nreason={marker[2]}\n")


def pending_watchdog_reboot(state_dir: Path, boot_id: str | None) -> str | None:
    """Reason text when this boot follows a watchdog reboot, else None."""
    marker = read_stall_marker(state_dir / STALL_MARKER_NAME)
    if marker is not None and boot_id and marker[0] != boot_id:
        return marker[2]
    path = state_dir / PREVIOUS_REBOOT_NAME
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except OSError:
        return None
    found = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if separator:
            found[key.strip()] = value.strip()
    if boot_id and found.get("boot_id") == boot_id:
        return found.get("reason") or "true-zero"
    return None


def recovery_already_announced(state_dir: Path, boot_id: str | None) -> bool:
    if not boot_id:
        return False
    path = state_dir / RECOVERY_ANNOUNCED_NAME
    try:
        text = path.read_text(encoding="ascii")
    except OSError:
        return False
    return f"boot_id={boot_id}" in text


def read_defaults(path: Path = NOTIFY_DEFAULT) -> dict[str, str]:
    """KEY=VALUE lines from /etc/default/dgx-fan-control. Missing file is empty.

    Permission denied is raised so a health check can say it needs root
    instead of treating the file as unset.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        if access_denied(exc):
            raise
        return {}
    found = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        found[key.strip()] = value.strip().strip("'\"")
    return found


def command_succeeded(command: str, timeout: float = CHECK_TIMEOUT_SECONDS) -> bool | None | str:
    """True on success, False on a clean non-zero exit.

    An http or https URL is a GET. ``timeout`` and ``unknown (needs root)``
    name those outcomes. None is any other failure.
    """
    command = command.strip()
    if not command:
        return None
    if command.startswith("http://") or command.startswith("https://"):
        try:
            with urllib.request.urlopen(command, timeout=timeout) as response:
                return 200 <= getattr(response, "status", 200) < 300
        except TimeoutError:
            return "timeout"
        except urllib.error.URLError as exc:
            if isinstance(getattr(exc, "reason", None), TimeoutError):
                return "timeout"
            return None
        except OSError as exc:
            if access_denied(exc):
                return NEEDS_ROOT
            return None
        except ValueError:
            return None
    try:
        completed = subprocess.run(
            command, shell=True, check=False, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "timeout"
    except OSError as exc:
        if access_denied(exc):
            return NEEDS_ROOT
        return None
    return completed.returncode == 0


def work_is_in_progress(defaults: dict[str, str], gpu_jobs) -> tuple[bool, str]:
    """Whether a reboot should wait.

    BUSY_CHECK exit 0 means work is in progress. A failure or timeout counts
    as busy. When BUSY_CHECK is unset, nvidia-smi compute apps are the default.
    """
    command = defaults.get("BUSY_CHECK", "").strip()
    if command:
        result = command_succeeded(command)
        if result is True:
            return True, "busy-check"
        if result is False:
            return False, "busy-check"
        return True, "busy-check-failed"
    state = gpu_jobs()
    if state is None:
        return True, "nvidia-smi-failed"
    return bool(state), "nvidia-smi"


def ready_for_work(defaults: dict[str, str]) -> str:
    """pass, fail, timeout, not configured, or unknown (needs root)."""
    command = defaults.get("READY_CHECK", "").strip()
    if not command:
        return "not configured"
    result = command_succeeded(command)
    if result == NEEDS_ROOT:
        return NEEDS_ROOT
    if result == "timeout":
        return "timeout"
    if result is True:
        return "pass"
    return "fail"


def configured_float(defaults: dict[str, str], key: str, fallback: float) -> float:
    raw = defaults.get(key, "").strip()
    if not raw:
        return fallback
    try:
        value = float(raw)
    except ValueError:
        LOG.warning("%s is not a number; using %s", key, fallback)
        return fallback
    if not math.isfinite(value) or value < 0:
        LOG.warning("%s must be finite and non-negative; using %s", key, fallback)
        return fallback
    return value


def _emit_check(name: str, status: str, detail: str, emit) -> str | None:
    """Print one check. Return the flag text when it is not a pass."""
    if status == "pass":
        emit(f"{name}: pass")
        return None
    if status == "unknown":
        emit(f"{name}: {NEEDS_ROOT}")
        return f"{name}: {NEEDS_ROOT}"
    emit(f"{name}: fail ({detail})")
    return detail


def _module_loaded_now() -> bool | None:
    """True, False, or None when /sys/module cannot be read."""
    path = Path("/sys/module") / MODULE_NAME
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return False
    except OSError as exc:
        if access_denied(exc):
            return None
        return False
    return stat.S_ISDIR(mode)


def _count_floor_devices(thermal_root: Path) -> int | None:
    names = scandir_names(thermal_root)
    if names is None:
        return None
    found = 0
    for name in names:
        if not name.startswith("cooling_device"):
            continue
        try:
            kind = (thermal_root / name / "type").read_text(encoding="ascii").strip()
        except OSError as exc:
            if access_denied(exc):
                return None
            continue
        if kind == COOLING_DEVICE_TYPE:
            found += 1
    return found


def _fan_status(hwmon_root: Path) -> tuple[str, str]:
    names = scandir_names(hwmon_root)
    if names is None:
        return "unknown", ""
    rpms: list[int] = []
    for name in names:
        device = hwmon_root / name
        try:
            kind = (device / "name").read_text(encoding="ascii").strip()
        except OSError as exc:
            if access_denied(exc):
                return "unknown", ""
            continue
        if kind != ACPI_FAN_NAME:
            continue
        fan_names = scandir_names(device)
        if fan_names is None:
            return "unknown", ""
        for fan in fan_names:
            if not (fan.startswith("fan") and fan.endswith("_input")):
                continue
            try:
                rpms.append(int((device / fan).read_text(encoding="ascii").strip()))
            except OSError as exc:
                if access_denied(exc):
                    return "unknown", ""
                return "fail", "ACPI fan inputs are unreadable"
            except (UnicodeError, ValueError):
                return "fail", "ACPI fan inputs are unreadable"
    if len(rpms) < 2 or any(rpm == 0 for rpm in rpms):
        return "fail", "ACPI fans are not all nonzero"
    return "pass", ""


def _temperature_status(thermal_root: Path, soc_celsius: float) -> tuple[str, str]:
    try:
        hottest = read_hottest_temperature_c(thermal_root)
    except PermissionError:
        return "unknown", ""
    except (OSError, RuntimeError, ValueError):
        return "fail", "hottest zone is not readable"
    if hottest >= soc_celsius:
        return "fail", f"hottest zone is {hottest:.1f}C"
    return "pass", ""


def collect_health_checks(thermal_root: Path, hwmon_root: Path, *,
                          module_loaded: bool | None, service_active: str,
                          service_enabled: str, exec_status: int | None,
                          watchdog_active: str, soc_celsius: float,
                          log_text: str | None, ready: str, emit) -> list[str]:
    """Run every check, emitting each line before the next external call.

    ``log_text`` None means read the kernel log only if a missing cooling
    device still needs to be classified. The returned list is flag text for
    checks that did not pass. ``ready`` is pass, fail, timeout, not
    configured, or unknown (needs root).
    """
    problems: list[str] = []

    def keep(name: str, status: str, detail: str = "") -> None:
        problem = _emit_check(name, status, detail, emit)
        if problem:
            problems.append(problem)

    if module_loaded is None:
        module_loaded = _module_loaded_now()
    if module_loaded is None:
        keep("module", "unknown")
    elif module_loaded:
        keep("module", "pass")
    else:
        keep("module", "fail", "kernel module dgx_ec_fan_control is not loaded")

    devices = _count_floor_devices(thermal_root)
    foreign = exec_status == FOREIGN_FLOOR_EXIT and service_active != "active"
    if devices is None:
        keep("cooling-device", "unknown")
    elif foreign:
        keep("cooling-device", "fail", "probe refused a foreign lower floor")
    elif devices != 1:
        if log_text is None and module_loaded is True:
            log_text = read_kernel_log()
        if log_text and "refusing to replace existing lower floor" in log_text:
            keep("cooling-device", "fail", "probe refused a foreign lower floor")
        else:
            keep("cooling-device", "fail", "dgx_ec_fan_floor cooling device is missing")
    else:
        keep("cooling-device", "pass")

    if service_active == NEEDS_ROOT or service_enabled == NEEDS_ROOT:
        keep("daemon", "unknown")
    elif service_active == "timeout" or service_enabled == "timeout":
        keep("daemon", "fail", "systemctl timed out")
    elif service_active == "unknown" or service_enabled == "unknown":
        keep("daemon", "fail", "systemctl state unknown")
    elif service_enabled == "enabled" and service_active != "active":
        keep("daemon", "fail", f"{DAEMON_UNIT} is not active ({service_active})")
    else:
        keep("daemon", "pass")

    keep("fans", *_fan_status(hwmon_root))
    keep("temperature", *_temperature_status(thermal_root, soc_celsius))

    if watchdog_active == NEEDS_ROOT:
        keep("watchdog", "unknown")
    elif watchdog_active == "timeout":
        keep("watchdog", "fail", "systemctl timed out")
    elif watchdog_active != "active":
        keep("watchdog", "fail", f"{WATCHDOG_UNIT} is not active ({watchdog_active})")
    else:
        keep("watchdog", "pass")

    if ready == "not configured":
        emit("ready: not configured")
    elif ready == "pass":
        emit("ready: pass")
    elif ready == "timeout":
        keep("ready", "fail", "READY_CHECK timed out")
    elif ready == NEEDS_ROOT:
        keep("ready", "unknown")
    else:
        keep("ready", "fail", "workload not ready")
    return problems


def write_health_record(path: Path, result: str, ready: str, reasons: list[str],
                        boot_id: str | None, when: float) -> None:
    lines = [
        f"result={result}",
        f"ready={ready}",
        f"boot_id={boot_id or ''}",
        f"time={when:.3f}",
    ]
    if reasons:
        lines.append("reason=" + "; ".join(reasons))
    write_state_text(path, "\n".join(lines) + "\n")


def run_notify(defaults: dict[str, str], summary: str) -> None:
    """Run DGX_FAN_NOTIFY with the summary on stdin. Empty means off."""
    command = defaults.get("DGX_FAN_NOTIFY", "").strip()
    if not command:
        return
    try:
        subprocess.run(
            command, shell=True, input=summary, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOG.warning("notify hook failed: %s", exc)


def _stored_text(path: Path) -> str:
    try:
        return path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return "none"
    except OSError as exc:
        if access_denied(exc):
            return NEEDS_ROOT
        return "none"


def run_health(thermal_root: Path, hwmon_root: Path, *,
               settle_seconds: float = 0,
               poll_seconds: float = HEALTH_POLL_SECONDS,
               soc_celsius: float = WATCHDOG_SOC_CELSIUS,
               state_dir: Path | None = None,
               defaults: dict[str, str] | None = None,
               monotonic=None, wall_time=None, sleeper=None,
               observer=None, notifier=None, boot_id: str | None = None,
               notify=None, record: bool | None = None) -> int:
    """Print fan, thermal, and READY_CHECK results.

    An interactive call uses one pass and does not write. ``settle_seconds``
    retries until the checks pass; the boot unit passes that. Lines are
    printed as each check finishes on a single pass. While settling, each
    failed attempt is one journal line. Writing the health file, clearing
    markers, broadcasting, and setting the flag happen only when ``record``
    is true. The default records when the effective uid is 0.
    """
    if state_dir is None:
        state_dir = STALL_MARKER_DIR
    defaults_denied = False
    if defaults is None:
        try:
            defaults = read_defaults()
        except OSError as exc:
            defaults = {}
            defaults_denied = access_denied(exc)
    if not math.isfinite(settle_seconds) or settle_seconds < 0:
        raise ValueError("health settle interval must be finite and non-negative")
    if not math.isfinite(poll_seconds) or poll_seconds <= 0:
        raise ValueError("health poll interval must be finite and positive")
    if monotonic is None:
        monotonic = time.monotonic
    if wall_time is None:
        wall_time = time.time
    if sleeper is None:
        sleeper = time.sleep
    if notifier is None:
        notifier = broadcast_alert
    if notify is None:
        notify = run_notify
    if boot_id is None:
        boot_id = read_boot_id()
    if record is None:
        record = os.geteuid() == 0
    interactive = settle_seconds <= 0
    deadline = monotonic() + settle_seconds
    attempt = 0
    lines: list[str] = []
    problems: list[str] = []
    ready = "not configured"

    def evaluate(emit) -> list[str]:
        nonlocal ready
        if observer is None:
            active, enabled, exec_status = read_daemon_unit()
            watchdog_active, _watchdog_enabled = watchdog_unit_state()
            snapshot = {
                "module_loaded": None,
                "service_active": active,
                "service_enabled": enabled,
                "exec_status": exec_status,
                "watchdog_active": watchdog_active,
            }
            log_text = None
        else:
            snapshot = observer()
            log_text = snapshot.get("log_text", "")
        if defaults_denied:
            ready = NEEDS_ROOT
        else:
            ready = ready_for_work(defaults)
        return collect_health_checks(
            thermal_root, hwmon_root,
            module_loaded=snapshot.get("module_loaded", None),
            service_active=snapshot["service_active"],
            service_enabled=snapshot["service_enabled"],
            exec_status=snapshot.get("exec_status"),
            watchdog_active=snapshot["watchdog_active"],
            soc_celsius=soc_celsius,
            log_text=log_text,
            ready=ready,
            emit=emit,
        )

    while True:
        attempt += 1
        lines = []
        if interactive:
            problems = evaluate(lambda line: (lines.append(line), print(line, flush=True)))
        else:
            problems = evaluate(lines.append)
        if not problems or interactive or monotonic() >= deadline:
            if problems and not interactive:
                LOG.info("health attempt %d: %s", attempt, "; ".join(
                    line for line in lines if not line.endswith(": pass")
                    and line != "ready: not configured" and line != "ready: pass"
                ))
            break
        failing = [
            line for line in lines
            if not line.endswith(": pass")
            and line != "ready: not configured" and line != "ready: pass"
        ]
        LOG.info("health attempt %d: %s", attempt, "; ".join(failing))
        sleeper(poll_seconds)
    if not interactive:
        for line in lines:
            print(line, flush=True)
    print("health: healthy" if not problems else "health: fail", flush=True)
    when = wall_time()
    health_path = state_dir / HEALTH_NAME
    intervention_path = state_dir / INTERVENTION_NAME
    if record:
        try:
            if not problems:
                previous = pending_watchdog_reboot(state_dir, boot_id)
                clear_stall_marker(state_dir / STALL_MARKER_NAME)
                clear_stall_marker(intervention_path)
                clear_stall_marker(state_dir / PREVIOUS_REBOOT_NAME)
                write_health_record(health_path, "healthy", ready, [], boot_id, when)
                if previous and not recovery_already_announced(state_dir, boot_id):
                    message = "dgx-fan-control: recovered after watchdog reboot"
                    LOG.info(message)
                    _wall(notifier, message, "recovery notice")
                    write_state_text(
                        state_dir / RECOVERY_ANNOUNCED_NAME,
                        f"boot_id={boot_id or ''}\n",
                    )
                    notify(defaults, message + "\n")
                else:
                    LOG.info("dgx-fan-control: healthy")
            else:
                reason = "; ".join(problems)
                write_intervention(intervention_path, reason, boot_id, when)
                write_health_record(
                    health_path, "needs-intervention", ready, problems, boot_id, when,
                )
                message = f"dgx-fan-control: needs intervention: {reason}"
                LOG.critical(message)
                notify(defaults, message + "\n")
        except OSError as exc:
            if access_denied(exc):
                print("recorded: no (needs root)", flush=True)
                return 1
            raise
    stored_flag = _stored_text(intervention_path)
    if stored_flag not in {"none", NEEDS_ROOT} and stored_flag.startswith("reason="):
        stored_flag = stored_flag.split("reason=", 1)[1].split("\n", 1)[0]
    stored_health = _stored_text(health_path)
    if stored_health not in {"none", NEEDS_ROOT}:
        kept = [
            line for line in stored_health.splitlines()
            if line.startswith("result=") or line.startswith("ready=")
        ]
        stored_health = " ".join(kept) if kept else stored_health.splitlines()[0]
    print(f"stored needs-intervention: {stored_flag}", flush=True)
    print(f"stored health: {stored_health}", flush=True)
    print("recorded: yes" if record else "recorded: no", flush=True)
    return 0 if not problems else 1


def show_intervention() -> None:
    banner = intervention_banner(STALL_MARKER_DIR / INTERVENTION_NAME)
    if banner:
        print(banner, end="")


def gpu_job_running() -> bool | None:
    """True when nvidia-smi lists a compute pid. None when the query fails.

    None is treated as a running job so a missed query cannot reboot the machine.
    """
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader"],
            check=False, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return any(line.strip() for line in (completed.stdout or "").splitlines())


def recover_fan_stall(thermal_root: Path, *, module_loaded: bool) -> None:
    """One repair after a true-zero stall. Never writes ACPI 0 or a level below 40.

    With the module loaded, request its maximum floor through its cooling
    device. Do not write ACPI: both paths use the same EC register.
    """
    if module_loaded:
        devices = cooling_devices(thermal_root)
        if len(devices) != 1:
            LOG.error(
                "fan stall: %s is loaded but there is no single cooling device; "
                "not writing ACPI",
                MODULE_NAME,
            )
            return
        try:
            write_state(devices[0], MAX_STATE)
        except (OSError, RuntimeError, ValueError) as exc:
            LOG.error(
                "fan stall: module state %d write failed: %s", MAX_STATE, exc,
            )
            return
        LOG.critical(
            "fan stall: requested module state %d because every fan read 0 RPM; "
            "did not write ACPI",
            MAX_STATE,
        )
        return
    try:
        set_acpi_fans(
            thermal_root, ACPI_MAX_LEVEL,
            module_loaded=False, service_active="inactive", report=False,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        LOG.error("fan stall: ACPI level %d write failed: %s", ACPI_MAX_LEVEL, exc)
        return
    LOG.critical(
        "fan stall: wrote ACPI Fan level %d because every fan read 0 RPM",
        ACPI_MAX_LEVEL,
    )


def note_deferred_reboot(kind: str, detail: str, now: float, since: float | None,
                         wall_at: float | None, max_defer: float, repeat: float,
                         notifier, announced: bool) -> tuple[float, float | None, bool]:
    """Journal the deferral. After max_defer, wall as well and keep waiting."""
    if since is None:
        since = now
    if now - since >= max_defer:
        message = (
            f"{kind}: deferral limit of {max_defer:.0f}s reached; "
            "work is still in progress; not rebooting through work"
        )
        due = wall_at is None or now - wall_at >= repeat
        if due:
            LOG.critical(message)
            _wall(notifier, message, "deferral-limit alert")
            wall_at = now
        else:
            LOG.warning(message)
        return since, wall_at, True
    if not announced:
        LOG.warning("%s: deferring reboot; %s", kind, detail)
    return since, wall_at, True


def _wall(notifier, message: str, what: str) -> None:
    try:
        notifier(message)
    except OSError:
        LOG.warning("could not broadcast the %s", what)


def run_watchdog(thermal_root: Path, hwmon_root: Path, *,
                 poll_seconds: float = WATCHDOG_POLL_SECONDS,
                 zero_seconds: float = WATCHDOG_ZERO_SECONDS,
                 stall_seconds: float = WATCHDOG_STALL_SECONDS,
                 reboot_seconds: float = WATCHDOG_REBOOT_SECONDS,
                 soc_celsius: float = WATCHDOG_SOC_CELSIUS,
                 alert_repeat_seconds: float = WATCHDOG_ALERT_REPEAT_SECONDS,
                 nonzero_clear_seconds: float = WATCHDOG_NONZERO_CLEAR_SECONDS,
                 action: str = WATCHDOG_DEFAULT_ACTION,
                 reboot_action: str = WATCHDOG_REBOOT_ACTION,
                 max_defer_seconds: float = WATCHDOG_MAX_DEFER_SECONDS,
                 monotonic=None, wall_time=None, runner=None, unit_state=None,
                 notifier=None, gpu_jobs=None, module_loaded=None,
                 marker_path: Path | None = None,
                 boot_id: str | None = None,
                 defaults: dict[str, str] | None = None) -> int:
    """Watch ACPI fans. A true zero or a foreign floor can reboot once.

    A foreign floor skips the repair because those fans are already high.
    The next boot with the same floor records needs-intervention and does
    not reboot or power off. ``zero_seconds`` remains accepted so older
    invocations still parse. MAX_DEFER_SECONDS in the defaults file, when
    set, overrides ``max_defer_seconds``.
    """
    del zero_seconds  # Hot shutdown now follows the hottest zone, not a second hold.
    if not math.isfinite(poll_seconds) or poll_seconds <= 0:
        raise ValueError("poll interval must be finite and positive")
    if not math.isfinite(stall_seconds) or stall_seconds < 0:
        raise ValueError("stall interval must be finite and non-negative")
    if not math.isfinite(reboot_seconds) or reboot_seconds < 0:
        raise ValueError("reboot interval must be finite and non-negative")
    if not math.isfinite(soc_celsius):
        raise ValueError("SoC threshold must be finite")
    if not math.isfinite(alert_repeat_seconds) or alert_repeat_seconds < 0:
        raise ValueError("alert repeat interval must be finite and non-negative")
    if not math.isfinite(max_defer_seconds) or max_defer_seconds < 0:
        raise ValueError("maximum deferral must be finite and non-negative")
    if monotonic is None:
        monotonic = time.monotonic
    if wall_time is None:
        wall_time = time.time
    if runner is None:
        runner = run_watchdog_action
    if unit_state is None:
        unit_state = read_daemon_unit
    if notifier is None:
        notifier = broadcast_alert
    if gpu_jobs is None:
        gpu_jobs = gpu_job_running
    if defaults is None:
        try:
            defaults = read_defaults()
        except OSError:
            LOG.warning("could not read %s; BUSY_CHECK is unset", NOTIFY_DEFAULT)
            defaults = {}
    max_defer_seconds = configured_float(
        defaults, "MAX_DEFER_SECONDS", max_defer_seconds,
    )
    if marker_path is None:
        marker_path = STALL_MARKER_DIR / STALL_MARKER_NAME
    if boot_id is None:
        boot_id = read_boot_id()
    stop_requested = Event()

    def request_stop(signum: int, frame: object) -> None:
        stop_requested.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    LOG.critical(
        "fan-stall watchdog watching ACPI fans and the hottest thermal zone; "
        "0 RPM on every fan for more than %.0fs alerts and attempts a repair; "
        "still stopped after %.0fs reboots when no GPU job is running; "
        "a running job is powered off if the hottest zone reaches %.0fC",
        stall_seconds, reboot_seconds, soc_celsius,
    )
    zero_since: float | None = None
    nonzero_since: float | None = None
    announced_shortage = False
    last_daemon_wall: float | None = None
    last_daemon_state: tuple | None = None
    last_stall_wall: float | None = None
    stall_repair_done = False
    reboot_requested = False
    deferred_logged = False
    smi_warned = False
    reported_unsupported = False
    foreign_walled = False
    foreign_since: float | None = None
    foreign_deferred = False
    foreign_intervened = False
    foreign_defer_since: float | None = None
    foreign_defer_wall: float | None = None
    zero_defer_since: float | None = None
    zero_defer_wall: float | None = None
    while not stop_requested.is_set():
        active_state, enabled_state, exec_status = split_unit_state(unit_state())
        if exec_status == FOREIGN_FLOOR_EXIT:
            now = monotonic()
            if foreign_since is None:
                foreign_since = now
            held = now - foreign_since
            message = (
                "dgx-fan-control: probe refused a foreign lower floor; "
                "a reboot clears the ACPI floor"
            )
            if not foreign_walled:
                LOG.critical(message)
                _wall(notifier, message, "foreign-floor alert")
                foreign_walled = True
            else:
                LOG.info(message)
            try:
                hottest = read_hottest_temperature_c(thermal_root)
            except (OSError, RuntimeError, ValueError):
                hottest = None
            busy, busy_how = work_is_in_progress(defaults, gpu_jobs)
            if busy_how == "nvidia-smi-failed" and not smi_warned:
                LOG.warning("nvidia-smi failed; treating a GPU job as running")
                smi_warned = True
            if busy_how == "busy-check-failed" and not smi_warned:
                LOG.warning("BUSY_CHECK failed; treating work as in progress")
                smi_warned = True
            hot = hottest is not None and hottest >= soc_celsius
            if hot:
                LOG.critical(
                    "foreign floor: hottest zone is %.1fC; running %s",
                    hottest, action,
                )
                status = runner(action)
                if status != 0:
                    LOG.critical("fan-stall action exited %s", status)
                    return status if isinstance(status, int) else 1
                return 0
            if held >= reboot_seconds:
                marker = read_stall_marker(marker_path)
                previous_boot = (
                    marker is not None and boot_id is not None and marker[0] != boot_id
                )
                if busy:
                    detail = (
                        "a GPU job is running"
                        if busy_how.startswith("nvidia-smi")
                        else "BUSY_CHECK reports work in progress"
                    )
                    foreign_defer_since, foreign_defer_wall, foreign_deferred = (
                        note_deferred_reboot(
                            "foreign floor", detail, now, foreign_defer_since,
                            foreign_defer_wall, max_defer_seconds,
                            alert_repeat_seconds, notifier, foreign_deferred,
                        )
                    )
                elif previous_boot or boot_id is None:
                    reason = (
                        "foreign floor remains after a watchdog reboot; "
                        "fans are held high; not rebooting again"
                    )
                    if previous_boot:
                        remember_previous_reboot(marker_path.parent, boot_id, marker)
                    if not foreign_intervened:
                        write_intervention(
                            marker_path.parent / INTERVENTION_NAME,
                            reason, boot_id, wall_time(),
                        )
                        LOG.critical("dgx-fan-control: needs intervention: %s", reason)
                        foreign_intervened = True
                    else:
                        LOG.info("dgx-fan-control: needs intervention: %s", reason)
                elif reboot_requested or (
                    marker is not None and boot_id is not None and marker[0] == boot_id
                ):
                    LOG.info(
                        "foreign floor: reboot already requested this boot; "
                        "not requesting another",
                    )
                else:
                    write_stall_marker(
                        marker_path, boot_id, wall_time(), reason="foreign-floor",
                    )
                    reboot_requested = True
                    idle = (
                        "no GPU job" if busy_how.startswith("nvidia-smi")
                        else "no work in progress"
                    )
                    LOG.critical(
                        "foreign floor: %s; a reboot clears the ACPI floor; "
                        "running %s",
                        idle, reboot_action,
                    )
                    status = runner(reboot_action)
                    if status != 0:
                        LOG.critical("fan-stall action exited %s", status)
                        return status if isinstance(status, int) else 1
                    return 0
            if stop_requested.is_set():
                break
            stop_requested.wait(poll_seconds)
            continue
        foreign_since = None
        if daemon_needs_alarm(active_state, enabled_state, exec_status):
            now = monotonic()
            state_key = (active_state, enabled_state, exec_status)
            changed = state_key != last_daemon_state
            message = (
                f"dgx-fan-control: {DAEMON_UNIT} is not active "
                f"({active_state}, {enabled_state})"
            )
            LOG.warning(message)
            repeat_due = (
                last_daemon_wall is None
                or now - last_daemon_wall >= alert_repeat_seconds
            )
            if changed or repeat_due:
                _wall(notifier, message, "fan-daemon alert")
                last_daemon_wall = now
            last_daemon_state = state_key
        else:
            last_daemon_wall = None
            last_daemon_state = None
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
        except (OSError, UnicodeError, ValueError, RuntimeError):
            LOG.error("fan-stall observation failed; not treating it as a stall")
            fan_rpms = None
        if fan_rpms is None or len(fan_rpms) < 2:
            if fan_rpms is not None and not announced_shortage:
                LOG.warning(
                    "fan-stall watchdog sees %d ACPI fan input(s); "
                    "it will not trip until two are present",
                    len(fan_rpms),
                )
                announced_shortage = True
            zero_since = None
            nonzero_since = None
            last_stall_wall = None
            stall_repair_done = False
            deferred_logged = False
        elif any(rpm != 0 for rpm in fan_rpms):
            announced_shortage = False
            now = monotonic()
            if nonzero_since is None:
                nonzero_since = now
            elif now - nonzero_since >= nonzero_clear_seconds:
                marker = read_stall_marker(marker_path)
                if marker is not None:
                    remember_previous_reboot(marker_path.parent, boot_id, marker)
                    clear_stall_marker(marker_path)
                    LOG.info(
                        "fan stall: fans have been nonzero for %.0fs; "
                        "cleared the true-zero reboot marker",
                        nonzero_clear_seconds,
                    )
            zero_since = None
            last_stall_wall = None
            stall_repair_done = False
            reboot_requested = False
            deferred_logged = False
        else:
            announced_shortage = False
            nonzero_since = None
            now = monotonic()
            if zero_since is None:
                zero_since = now
            held = now - zero_since
            LOG.warning(
                "fan stall: ACPI fans still read %s RPM for %.1fs",
                fan_rpms, held,
            )
            try:
                hottest = read_hottest_temperature_c(thermal_root)
            except (OSError, RuntimeError, ValueError):
                hottest = None
                LOG.error(
                    "fan stall: hottest zone unreadable; not using temperature",
                )
            if held > stall_seconds:
                repeat_due = (
                    last_stall_wall is None
                    or now - last_stall_wall >= alert_repeat_seconds
                )
                if last_stall_wall is None or repeat_due:
                    message = (
                        f"dgx-fan-control: ACPI fans read {fan_rpms} RPM "
                        f"for {held:.1f}s"
                        + (f" at {hottest:.1f}C" if hottest is not None else "")
                        + "; fans are stopped"
                    )
                    LOG.critical(message)
                    _wall(notifier, message, "fan-stall alert")
                    last_stall_wall = now
                if not stall_repair_done:
                    stall_repair_done = True
                    loaded = module_is_loaded() if module_loaded is None else module_loaded
                    LOG.info(
                        "fan stall: attempting repair; module loaded=%s",
                        loaded,
                    )
                    recover_fan_stall(thermal_root, module_loaded=loaded)
            busy, busy_how = work_is_in_progress(defaults, gpu_jobs)
            if busy_how == "nvidia-smi-failed" and not smi_warned:
                LOG.warning("nvidia-smi failed; treating a GPU job as running")
                smi_warned = True
            if busy_how == "busy-check-failed" and not smi_warned:
                LOG.warning("BUSY_CHECK failed; treating work as in progress")
                smi_warned = True
            hot = hottest is not None and hottest >= soc_celsius
            if busy and hot and held > stall_seconds:
                LOG.critical(
                    "fan stall: GPU job is running and the hottest zone is "
                    "%.1fC; running %s",
                    hottest, action,
                )
                status = runner(action)
                if status != 0:
                    LOG.critical("fan-stall action exited %s", status)
                    return status if isinstance(status, int) else 1
                return 0
            if held >= reboot_seconds:
                if busy:
                    detail = (
                        "a GPU job is running and the fans are still at 0 RPM"
                        if busy_how.startswith("nvidia-smi")
                        else "BUSY_CHECK reports work in progress"
                    )
                    zero_defer_since, zero_defer_wall, deferred_logged = (
                        note_deferred_reboot(
                            "fan stall", detail, now, zero_defer_since,
                            zero_defer_wall, max_defer_seconds,
                            alert_repeat_seconds, notifier, deferred_logged,
                        )
                    )
                else:
                    marker = read_stall_marker(marker_path)
                    if marker is not None and boot_id is not None and marker[0] != boot_id:
                        LOG.critical(
                            "fan stall: already rebooted once for a true-zero "
                            "stall; running %s",
                            action,
                        )
                        status = runner(action)
                        if status != 0:
                            LOG.critical("fan-stall action exited %s", status)
                            return status if isinstance(status, int) else 1
                        return 0
                    if reboot_requested or (
                        marker is not None and boot_id is not None and marker[0] == boot_id
                    ):
                        LOG.info(
                            "fan stall: reboot already requested this boot; "
                            "not requesting another",
                        )
                    else:
                        if boot_id is None:
                            LOG.critical(
                                "fan stall: boot id is unreadable, so a reboot "
                                "cannot be limited to one; running %s",
                                action,
                            )
                            status = runner(action)
                            if status != 0:
                                LOG.critical("fan-stall action exited %s", status)
                                return status if isinstance(status, int) else 1
                            return 0
                        write_stall_marker(
                            marker_path, boot_id, wall_time(), reason="true-zero",
                        )
                        reboot_requested = True
                        idle = (
                            "no GPU job" if busy_how.startswith("nvidia-smi")
                            else "no work in progress"
                        )
                        LOG.critical(
                            "fan stall: %s and fans still at 0 RPM "
                            "after %.1fs; running %s",
                            idle, held, reboot_action,
                        )
                        status = runner(reboot_action)
                        if status != 0:
                            LOG.critical("fan-stall action exited %s", status)
                            return status if isinstance(status, int) else 1
                        return 0
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
            "a reboot clears the ACPI floor. "
            "Fans stay high until then; do not write ACPI level 0."
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
    reason = missing_cooling_reason(module_loaded, log_text)
    if "probe refused a foreign lower floor" in reason:
        raise ForeignFloor(reason)
    raise RuntimeError(reason)


def read_hottest_temperature_c(thermal_root: Path) -> float:
    names = scandir_names(thermal_root)
    if names is None:
        raise PermissionError(errno.EACCES, "thermal zones are not readable")
    readings = []
    denied = False
    for name in names:
        if not name.startswith("thermal_zone"):
            continue
        path = thermal_root / name / "temp"
        try:
            millidegrees = int(path.read_text(encoding="ascii").strip())
        except OSError as exc:
            if access_denied(exc):
                denied = True
            continue
        except (UnicodeError, ValueError):
            continue
        if 0 < millidegrees <= 200_000:
            readings.append(millidegrees / 1000.0)
    if not readings:
        if denied:
            raise PermissionError(errno.EACCES, "thermal zones are not readable")
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
            timeout=CHECK_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
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


WATCHDOG_NOT_FOUND_HINT = (
    "watchdog-hint: unit not installed; see "
    "docs/installation.md#5-choose-your-startup-mode"
)


def format_watchdog_status(active: str, enabled: str) -> str:
    text = f"watchdog-active: {active}\nwatchdog-enabled: {enabled}\n"
    if enabled == "not-found":
        text += WATCHDOG_NOT_FOUND_HINT + "\n"
    return text


def watchdog_unit_state() -> tuple[str, str]:
    return (
        systemctl_query("is-active", WATCHDOG_UNIT),
        systemctl_query("is-enabled", WATCHDOG_UNIT),
    )


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
                  exit_monotonic_us: int | None = None,
                  watchdog_active: str = "unknown",
                  watchdog_enabled: str = "unknown") -> str:
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
        f"watchdog-enabled: {watchdog_enabled}",
        f"watchdog-active: {watchdog_active}",
        *([WATCHDOG_NOT_FOUND_HINT] if watchdog_enabled == "not-found" else []),
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
                  profile: str | None = None,
                  report: bool = True) -> None:
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
        if report:
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
        help="alert if ACPI fans stay at 0 RPM; power off only if that stop is also hot",
    )
    watchdog.add_argument("--poll-seconds", type=float, default=WATCHDOG_POLL_SECONDS)
    watchdog.add_argument("--zero-seconds", type=float, default=WATCHDOG_ZERO_SECONDS)
    watchdog.add_argument(
        "--stall-seconds",
        type=float,
        default=WATCHDOG_STALL_SECONDS,
        help="alert when every fan stays at 0 RPM this long, at any temperature",
    )
    watchdog.add_argument("--soc-celsius", type=float, default=WATCHDOG_SOC_CELSIUS)
    watchdog.add_argument("--action", default=WATCHDOG_DEFAULT_ACTION)
    watchdog.add_argument(
        "--reboot-seconds",
        type=float,
        default=WATCHDOG_REBOOT_SECONDS,
        help="reboot when fans are still at 0 RPM this long after the stall began and no GPU job is running",
    )
    watchdog.add_argument(
        "--reboot-action",
        default=WATCHDOG_REBOOT_ACTION,
        help="command when a true-zero stall has no GPU job; default systemctl reboot",
    )
    watchdog.add_argument(
        "--alert-repeat-seconds",
        type=float,
        default=WATCHDOG_ALERT_REPEAT_SECONDS,
        help="minimum time between repeated wall broadcasts for one ongoing condition",
    )
    watchdog.add_argument(
        "--max-defer-seconds",
        type=float,
        default=WATCHDOG_MAX_DEFER_SECONDS,
        help="after this long deferred, alert and keep waiting; never reboot through work",
    )
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
    health = subparsers.add_parser(
        "health",
        help="check fans and thermals after boot, then the optional READY_CHECK",
    )
    health.add_argument(
        "--clear",
        action="store_true",
        help="remove the needs-intervention flag; requires root",
    )
    health.add_argument(
        "--settle",
        type=float,
        default=0,
        help=(
            "seconds to retry; interactive health does one pass, "
            f"the boot unit passes {HEALTH_SETTLE_SECONDS:.0f}"
        ),
    )
    health.add_argument(
        "--record",
        action="store_true",
        help="write the health file and needs-intervention flag; requires root",
    )
    health.add_argument("--poll-seconds", type=float, default=HEALTH_POLL_SECONDS)
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
                stall_seconds=args.stall_seconds,
                reboot_seconds=args.reboot_seconds,
                soc_celsius=args.soc_celsius,
                alert_repeat_seconds=args.alert_repeat_seconds,
                action=args.action,
                reboot_action=args.reboot_action,
                max_defer_seconds=args.max_defer_seconds,
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
            watchdog_active, watchdog_enabled = watchdog_unit_state()
            show_intervention()
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
                watchdog_active=watchdog_active,
                watchdog_enabled=watchdog_enabled,
            ), end="")
            return 0
        if args.command == "health":
            if args.clear:
                if os.geteuid() != 0:
                    print("dgx-fan-control health --clear requires root")
                    return 1
                clear_stall_marker(STALL_MARKER_DIR / INTERVENTION_NAME)
                print("cleared the needs-intervention flag")
                return 0
            record_denied = args.record and os.geteuid() != 0
            if record_denied:
                print("dgx-fan-control health --record requires root")
            code = run_health(
                args.thermal_root,
                args.hwmon_root,
                settle_seconds=args.settle,
                poll_seconds=args.poll_seconds,
                record=os.geteuid() == 0,
            )
            if record_denied and code == 0:
                return 1
            return code
        if args.command == "status" and scandir_names(args.thermal_root) is None:
            show_intervention()
            print(f"state={NEEDS_ROOT} hottest={NEEDS_ROOT}")
            active, enabled = watchdog_unit_state()
            print(format_watchdog_status(active, enabled), end="")
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
            except OSError as exc:
                if not access_denied(exc):
                    raise
                state = None
            except RuntimeError as exc:
                if "floor was changed outside this driver" in str(exc):
                    show_intervention()
                    print(exc)
                    return 1
                raise
            try:
                hottest = read_hottest_temperature_c(args.thermal_root)
            except OSError as exc:
                if not access_denied(exc):
                    raise
                hottest = None
            active, enabled = watchdog_unit_state()
            show_intervention()
            if state is None or hottest is None:
                state_text = NEEDS_ROOT if state is None else f"{state}/{MAX_STATE}"
                hottest_text = NEEDS_ROOT if hottest is None else f"{hottest:.1f}C"
                print(f"state={state_text} hottest={hottest_text}")
            else:
                print(format_cooling_status(state, hottest))
            print(format_watchdog_status(active, enabled), end="")
            return 0
    except ControlUnsupported as exc:
        # status is interactive: print the reason once. Logging it as well
        # repeated the line (ERROR on stderr, then the same text on stdout).
        # The daemon and the write commands only log, so the journal has one
        # line. Exit 69 is only the daemon's startup check.
        if args.command == "status":
            active, enabled = watchdog_unit_state()
            show_intervention()
            print(CONTROL_UNSUPPORTED)
            print(format_watchdog_status(active, enabled), end="")
            return 0
        LOG.error("%s", exc)
        if args.command == "daemon":
            return UNSUPPORTED_FIRMWARE_EXIT
        return 1
    except ForeignFloor as exc:
        # One journal line. Exit 78 is not restarted: another start cannot
        # clear a floor the probe refused. A reboot clears the ACPI floor.
        if args.command == "status":
            active, enabled = watchdog_unit_state()
            show_intervention()
            print(exc)
            print(format_watchdog_status(active, enabled), end="")
            return 1
        LOG.error("%s", exc)
        if args.command == "daemon":
            return FOREIGN_FLOOR_EXIT
        return 1
    except (OSError, RuntimeError, ValueError) as exc:
        if args.command == "status" and (
            str(exc).startswith("no dgx_ec_fan_floor")
            or "floor was changed outside this driver" in str(exc)
        ):
            active, enabled = watchdog_unit_state()
            show_intervention()
            print(exc)
            print(format_watchdog_status(active, enabled), end="")
            return 1
        if not was_reported(exc):
            LOG.error("%s", exc)
        return 1
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
