#!/usr/bin/env python3
"""Smooth userland policy for the DGX Spark additive fan-floor device."""

from __future__ import annotations

import argparse
import errno
import logging
import math
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

LOG = logging.getLogger("dgx-fan-control")


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
                 monotonic=None, runner=None) -> int:
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
    while not stop_requested.is_set():
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


def require_control(thermal_root: Path, hwmon_root: Path) -> Path:
    matches = cooling_devices(thermal_root)
    if len(matches) == 1:
        return matches[0]
    if len(matches) == 0 and find_fan_hwmon(hwmon_root) is not None:
        raise ControlUnsupported(CONTROL_UNSUPPORTED)
    raise RuntimeError(
        f"expected one {COOLING_DEVICE_TYPE!r} cooling device, found {len(matches)}"
    )


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


def curve_state(temperature_c: float) -> int:
    if not math.isfinite(temperature_c) or temperature_c <= 0:
        raise ValueError("temperature must be finite and positive")
    state = 0
    for threshold_c, threshold_state in PERFORMANCE_CURVE:
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


def next_state(current: int, filtered_temperature_c: float) -> int:
    """Rate-limit the curve, with prompt increases and slow hysteretic falls."""
    if not 0 <= current <= MAX_STATE:
        raise ValueError("current cooling state is outside the driver contract")
    rising_target = curve_state(filtered_temperature_c)
    if rising_target > current:
        chosen = min(rising_target, current + UP_STATES_PER_POLL)
    else:
        falling_target = curve_state(filtered_temperature_c + DOWN_HYSTERESIS_C)
        if falling_target < current:
            chosen = max(falling_target, current - DOWN_STATES_PER_POLL)
        else:
            chosen = current
    return max(chosen, MIN_CURVE_STATE)


def read_state(cooling_device: Path) -> int:
    state = int((cooling_device / "cur_state").read_text(encoding="ascii").strip())
    maximum = int((cooling_device / "max_state").read_text(encoding="ascii").strip())
    if maximum != MAX_STATE or not 0 <= state <= maximum:
        raise RuntimeError(
            f"unexpected cooling-device contract: state={state} max_state={maximum}"
        )
    return state


def write_state(cooling_device: Path, state: int) -> None:
    if not 0 <= state <= MAX_STATE:
        raise ValueError(f"state must be between 0 and {MAX_STATE}")
    (cooling_device / "cur_state").write_text(f"{state}\n", encoding="ascii")
    observed = read_state(cooling_device)
    if observed != state:
        raise RuntimeError(f"cooling-state readback mismatch: wrote {state}, read {observed}")


def run_daemon(thermal_root: Path, poll_seconds: float,
               hwmon_root: Path | None = None) -> int:
    if not math.isfinite(poll_seconds) or poll_seconds <= 0:
        raise ValueError("poll interval must be finite and positive")
    if hwmon_root is None:
        hwmon_root = Path("/sys/class/hwmon")
    cooling_device = require_control(thermal_root, hwmon_root)
    filtered_temperature_c: float | None = None
    stop_requested = Event()
    failures = 0
    primary_failure = False

    def request_stop(signum: int, frame: object) -> None:
        stop_requested.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    LOG.info("controller starting; authenticating the current cooling state")

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
                    desired = next_state(current, filtered_temperature_c)
                except (OSError, RuntimeError, ValueError):
                    LOG.exception("temperature observation failed; requesting maximum cooling")
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
            except OSError as exc:
                failures += 1
                filtered_temperature_c = None
                if exc.errno not in RETRYABLE_ERRNOS or failures >= TRANSPORT_FAILURE_LIMIT:
                    LOG.error(
                        "fan control unavailable after %d failed attempt(s): %s; "
                        "stopping for operator recovery, current floor is unverified",
                        failures, exc,
                    )
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
        except Exception:
            LOG.exception("automatic restoration failed; current fan floor is unverified")
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


def set_acpi_fans(thermal_root: Path, level: int = ACPI_MAX_LEVEL) -> None:
    """Set every ACPI Fan device. On a failed lower level, rewrite them to 100.

    Does not use this project's kernel module. A level below 40 is refused
    before any write. EC 3.13.7 accepts the ACPI write and ignores it.
    """
    if level < ACPI_MIN_LEVEL or level > ACPI_MAX_LEVEL:
        raise ValueError(
            f"ACPI fan level must be between {ACPI_MIN_LEVEL} and {ACPI_MAX_LEVEL}"
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
            except (OSError, RuntimeError, ValueError):
                LOG.exception("fail-hot ACPI write of %s failed", ACPI_MAX_LEVEL)

    failures: list[BaseException] = []
    try:
        for device in devices:
            try:
                write_acpi_fan_level(device, level)
            except (OSError, RuntimeError, ValueError) as exc:
                failures.append(exc)
        if failures:
            raise RuntimeError(
                f"ACPI fan update to {level} failed on {len(failures)} device(s)"
            )
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
            return run_daemon(args.thermal_root, args.poll_seconds, args.hwmon_root)
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
            set_acpi_fans(args.thermal_root, args.level)
            return 0
        cooling_device = require_control(args.thermal_root, args.hwmon_root)
        if args.command == "set-state":
            write_state(cooling_device, args.state)
            return 0
        if args.command == "automatic":
            write_state(cooling_device, 0)
            return 0
        if args.command == "status":
            print(
                f"state={read_state(cooling_device)}/{MAX_STATE} "
                f"hottest={read_hottest_temperature_c(args.thermal_root):.1f}C"
            )
            return 0
    except ControlUnsupported as exc:
        LOG.error("%s", exc)
        if args.command == "status":
            print(CONTROL_UNSUPPORTED)
        # Permanent firmware limitation. Exit 0 so systemd does not restart the
        # daemon. set-state and automatic still fail because they cannot write.
        return 0 if args.command in ("status", "daemon") else 1
    except (OSError, RuntimeError, ValueError) as exc:
        LOG.error("%s", exc)
        return CONTROLLER_UNAVAILABLE_EXIT if args.command == "daemon" else 1
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
