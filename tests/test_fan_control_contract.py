import hashlib
import importlib.util
import re
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "kernel" / "dgx_ec_fan_control.c").read_text()
MODULE_PATH = ROOT / "userspace" / "dgx_fan_control.py"
SERVICE = (ROOT / "systemd" / "dgx-fan-control.service").read_text()
WATCHDOG_SERVICE = (ROOT / "systemd" / "dgx-fan-watchdog.service").read_text()
INSTALL_USERLAND = (ROOT / "scripts" / "install-userland").read_text()
UNINSTALL_USERLAND = (ROOT / "scripts" / "uninstall-userland").read_text()
SPEC = importlib.util.spec_from_file_location("dgx_fan_control", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
CONTROL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTROL)


class KernelContractTests(unittest.TestCase):
    def test_only_additive_lower_floor_is_writable(self):
        self.assertIn("DGX_EC_FAN_SET_LOWER_LIMIT", SOURCE)
        self.assertNotIn("SET_UPPER", SOURCE)
        self.assertNotIn("hwmon_fan_target", SOURCE)
        self.assertNotIn("hwmon_pwm", SOURCE)
        self.assertNotIn("debugfs", SOURCE)
        self.assertNotIn("ioctl", SOURCE)

    def test_state_zero_is_automatic_and_states_are_bounded(self):
        table = SOURCE.split("static const struct dgx_ec_fan_profile dgx_ec_profiles[]", 1)[1]
        self.assertIn("DGX_EC_LIMIT_UNSET", table)
        for rpm in ("2700U", "4500U", "9000U", "13500U", "2000U", "4350U"):
            self.assertIn(rpm, table)
        self.assertIn("state >= DGX_EC_FLOOR_STATE_COUNT", SOURCE)
        self.assertIn("EC 3.5.8", table)
        self.assertIn("EC 3.13.7", table)

    def test_platform_and_capabilities_fail_closed(self):
        for requirement in (
            'DMI_SYS_VENDOR, "NVIDIA"',
            'DMI_PRODUCT_NAME, "NVIDIA_DGX_Spark"',
            'DMI_BOARD_NAME, "P4242"',
            "FFA_VERSION_1_2",
            "dgx_ec_match_profile",
            "return -EBADMSG",
            "unrecognized capabilities raw=%*ph",
            "if (floor != DGX_EC_LIMIT_UNSET)",
            "floor == DGX_EC_LIMIT_STOPPED",
            "clearing zero lower clamp",
            "DGX_EC_STATUS_UNSUPPORTED",
            "registering hwmon telemetry only, no cooling device",
            "allow_unverified_writes",
            "module_param(allow_unverified_writes, bool, 0444)",
            "October SoC 2.158.15",
            "Do not write a level below 30",
            "has completed this probe",
            "has also timed out",
            "refusing to replace existing lower floor",
            "taking ownership of existing lower floor",
            "module_param(owned_state, int, 0444)",
            "module_param(owned_floor, uint, 0444)",
            "matches owned state",
            "static int owned_state = -1;",
            "owned_state >= 1",
            "owned_floor != 0",
            "Do not write ACPI level 0",
            "realtime=%lld.%09ld uptime=%lld.%09ld",
            "ret = -EAGAIN",
            "ret = -EPROTO",
            "floor ownership mismatch",
        ):
            self.assertIn(requirement, SOURCE)

    def test_orderly_lifecycle_restores_automatic_policy(self):
        self.assertIn("dgx_ec_restore_automatic(data, \"suspend\")", SOURCE)
        self.assertIn("dgx_ec_restore_automatic(data, \"reboot\")", SOURCE)
        self.assertIn("dgx_ec_restore_automatic(data, \"module removal\")", SOURCE)
        self.assertIn("dgx_ec_restore_automatic(data, \"module load\")", SOURCE)
        self.assertIn("DGX_EC_RESTORE_ATTEMPTS", SOURCE)
        self.assertIn("thermal_cooling_device_register", SOURCE)
        self.assertNotIn("dgx_ec_confirm_floor_control", SOURCE)

    def test_default_load_does_not_refuse_an_unset_floor(self):
        probe = SOURCE.split("dgx_ec_fan_control_probe", 1)[1]
        unset_gate = probe.index("floor != DGX_EC_LIMIT_UNSET")
        adopt = probe.index("taking ownership of existing lower floor")
        refuse = probe.index("refusing to replace existing lower floor")
        self.assertLess(unset_gate, adopt)
        self.assertLess(adopt, refuse)
        self.assertEqual(probe_existing_floor(0xFFFF), "continue")
        self.assertEqual(probe_existing_floor(0), "clear-zero")
        self.assertEqual(probe_existing_floor(13500), "refuse")
        self.assertEqual(
            probe_existing_floor(13500, owned_state=12, owned_floor=13500, table_value=13500),
            "adopt",
        )
        self.assertEqual(
            probe_existing_floor(0x10FE, owned_state=12, owned_floor=13500, table_value=13500),
            "refuse",
        )

    def test_kernel_version_bumps_with_the_source(self):
        version = re.search(r'DGX_EC_DRIVER_VERSION\s+"([^"]+)"', SOURCE)
        self.assertIsNotNone(version)
        package = re.search(
            r'PACKAGE_VERSION="([^"]+)"',
            (ROOT / "dkms.conf").read_text(encoding="utf-8"),
        )
        self.assertIsNotNone(package)
        self.assertEqual(version.group(1), package.group(1))
        recorded = (ROOT / "kernel" / "SOURCE_REV").read_text(encoding="ascii").strip()
        self.assertEqual(recorded, f"{version.group(1)} {kernel_source_digest()}")


def probe_existing_floor(floor, owned_state=-1, owned_floor=0, table_value=None,
                         state_count=13):
    """What probe does with an EC floor. Defaults match a load with no record.

    ``continue`` refuses nothing. ``clear-zero`` restores the NVIDIA curve.
    ``adopt`` takes a floor that matches the last-set record. ``refuse`` is
    the foreign-floor path.
    """
    if floor == 0:
        return "clear-zero"
    if floor == 0xFFFF:
        return "continue"
    owned = (
        owned_state >= 1
        and owned_state < state_count
        and owned_floor != 0
        and floor == owned_floor
        and table_value == owned_floor
    )
    return "adopt" if owned else "refuse"


def kernel_source_digest() -> str:
    digest = hashlib.sha256()
    for path in sorted((ROOT / "kernel").iterdir()):
        if not path.is_file() or path.name == "SOURCE_REV":
            continue
        if path.suffix not in {".c", ".h"} and path.name != "Makefile":
            continue
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


class UserlandPolicyTests(unittest.TestCase):
    def test_performance_curve_reaches_maximum_at_seventy(self):
        self.assertEqual(CONTROL.curve_state(0.1), 1)
        self.assertEqual(CONTROL.curve_state(49.9), 1)
        self.assertEqual(CONTROL.curve_state(50.0), 3)
        self.assertEqual(CONTROL.curve_state(60.0), 8)
        self.assertEqual(CONTROL.curve_state(70.0), 12)
        self.assertGreaterEqual(min(state for _, state in CONTROL.PERFORMANCE_CURVE), 1)

    def test_running_curve_never_selects_state_zero(self):
        for temperature in (0.1, 1, 20, 49.9, 50, 55, 60, 65, 70, 90, 120):
            self.assertGreaterEqual(CONTROL.curve_state(temperature), 1)
            for current in range(CONTROL.MAX_STATE + 1):
                self.assertGreaterEqual(CONTROL.next_state(current, temperature), 1)

    def test_rate_limit_rises_quickly_and_falls_with_hysteresis(self):
        self.assertEqual(CONTROL.next_state(0, 70.0), 2)
        self.assertEqual(CONTROL.next_state(10, 70.0), 12)
        self.assertEqual(CONTROL.next_state(12, 67.0), 12)
        self.assertEqual(CONTROL.next_state(12, 65.9), 11)

    def test_finds_exactly_one_device_and_authenticates_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            zone = root / "thermal_zone0"
            zone.mkdir()
            (zone / "temp").write_text("65000\n", encoding="ascii")
            cooling = root / "cooling_device0"
            cooling.mkdir()
            (cooling / "type").write_text("dgx_ec_fan_floor\n", encoding="ascii")
            (cooling / "max_state").write_text("12\n", encoding="ascii")
            (cooling / "cur_state").write_text("0\n", encoding="ascii")

            self.assertEqual(CONTROL.find_cooling_device(root), cooling)
            self.assertEqual(CONTROL.read_hottest_temperature_c(root), 65.0)
            CONTROL.write_state(cooling, 8)
            self.assertEqual(CONTROL.read_state(cooling), 8)

    def test_service_keeps_root_write_but_drops_ambient_authority(self):
        self.assertNotIn("User=", SERVICE)
        self.assertIn("CapabilityBoundingSet=\n", SERVICE)
        self.assertIn("NoNewPrivileges=yes", SERVICE)
        self.assertIn("PrivateNetwork=yes", SERVICE)
        self.assertIn("ProtectSystem=strict", SERVICE)
        self.assertNotIn("modprobe", SERVICE)
        self.assertIn("Restart=always\n", SERVICE)
        self.assertIn("RestartSec=30s\n", SERVICE)
        self.assertIn("StartLimitIntervalSec=0\n", SERVICE)
        self.assertIn("RestartPreventExitStatus=69 78\n", SERVICE)
        self.assertNotIn("StartLimitBurst", SERVICE)

    def test_watchdog_unit_is_independent_and_can_do_its_job(self):
        text = WATCHDOG_SERVICE
        self.assertIn("ExecStart=/usr/local/sbin/dgx-fan-control watchdog\n", text)
        self.assertIn("Restart=always\n", text)
        self.assertIn("RestartSec=5s\n", text)
        self.assertIn("StartLimitIntervalSec=0\n", text)
        self.assertIn("WantedBy=multi-user.target\n", text)
        for forbidden in (
            "Requires=",
            "BindsTo=",
            "PartOf=",
            "Wants=dgx-fan-control.service",
            "After=dgx-fan-control.service",
            "dgx-fan-control.service",
            "PrivateDevices=",
            "PrivateNetwork=",
            "ProtectKernelTunables=",
            "CapabilityBoundingSet=",
        ):
            self.assertNotIn(forbidden, text)
        self.assertIn("ProtectSystem=strict\n", text)
        self.assertIn("ReadWritePaths=/sys -/var/lib/dgx-fan-control\n", text)
        self.assertIn("RestrictAddressFamilies=AF_UNIX\n", text)
        self.assertIn("NoNewPrivileges=yes\n", text)
        self.assertIn("systemctl enable --now dgx-fan-watchdog.service\n", INSTALL_USERLAND)
        self.assertNotIn("enable --now dgx-fan-control.service", INSTALL_USERLAND)
        self.assertIn("--no-watchdog", INSTALL_USERLAND)
        self.assertIn("not enabling dgx-fan-watchdog.service (--no-watchdog)", INSTALL_USERLAND)
        self.assertIn("systemctl enable dgx-fan-health.service\n", INSTALL_USERLAND)
        self.assertIn("systemctl enable dgx-fan-manual.service\n", INSTALL_USERLAND)
        self.assertIn("/etc/systemd/system/dgx-fan-manual.service", INSTALL_USERLAND)
        self.assertIn("/etc/update-motd.d/95-dgx-fan-control", INSTALL_USERLAND)
        self.assertIn("systemctl disable --now dgx-fan-watchdog.service\n", UNINSTALL_USERLAND)
        self.assertIn("systemctl disable --now dgx-fan-health.service\n", UNINSTALL_USERLAND)
        self.assertIn("systemctl disable --now dgx-fan-manual.service\n", UNINSTALL_USERLAND)
        self.assertIn("rm -f /etc/modprobe.d/dgx-fan-control-owned.conf\n", UNINSTALL_USERLAND)
        self.assertIn("rm -f /etc/update-motd.d/95-dgx-fan-control\n", UNINSTALL_USERLAND)
        health = (ROOT / "systemd" / "dgx-fan-health.service").read_text()
        self.assertIn("Type=oneshot\n", health)
        self.assertIn(
            "ExecStart=/usr/local/sbin/dgx-fan-control health --record --settle 45\n",
            health,
        )
        self.assertIn("After=multi-user.target dgx-fan-watchdog.service\n", health)
        self.assertNotIn("Requires=", health)
        self.assertNotIn("PrivateNetwork=", health)
        motd = (ROOT / "systemd" / "95-dgx-fan-control").read_text()
        self.assertIn("/var/lib/dgx-fan-control/needs-intervention", motd)
        self.assertIn("s/^reason=//p", motd)
        defaults = (ROOT / "systemd" / "dgx-fan-control.default").read_text()
        self.assertIn("READY_CHECK=\n", defaults)
        self.assertIn("BUSY_CHECK=\n", defaults)
        self.assertIn("MAX_DEFER_SECONDS=1800\n", defaults)
        self.assertIn("MANUAL_STATE=\n", defaults)
        self.assertIn("PROGRESS_CHECK=\n", defaults)
        self.assertIn("STUCK_SECONDS=600\n", defaults)
        self.assertIn(
            "awk '/^vllm:(generation|prompt)_tokens_total[{ ]/{s+=$NF} END{print s+0}'",
            defaults,
        )

    def test_vllm_progress_example_sums_labeled_series_and_keeps_zero(self):
        awk = re.search(
            r"awk '([^']+)'",
            (ROOT / "systemd" / "dgx-fan-control.default").read_text(),
        )
        usage = re.search(r"awk '([^']+)'", (ROOT / "docs" / "usage.md").read_text())
        self.assertIsNotNone(awk)
        self.assertIsNotNone(usage)
        self.assertEqual(awk.group(1), usage.group(1))
        program = awk.group(1)
        metrics = "\n".join((
            '# HELP vllm:generation_tokens_total Generated tokens.',
            'vllm:generation_tokens_total{model_name="alpha"} 1000.0',
            'vllm:generation_tokens_total{model_name="beta"} 50',
            'vllm:prompt_tokens_total{model_name="alpha"} 200.5',
            'vllm:prompt_tokens_total{model_name="beta"} 0',
            "vllm:prompt_tokens_total 1",
            'vllm:generation_tokens_created{model_name="alpha"} 999',
            'vllm:request_success_total{model_name="alpha"} 7',
            "",
        ))

        def sampled(text: str) -> float | None:
            quoted = text.replace("'", "'\\''")
            return CONTROL.read_progress_value(
                f"printf '%s\\n' '{quoted}' | awk '{program}'"
            )

        self.assertEqual(sampled(metrics), 1251.5)
        self.assertEqual(sampled(""), 0.0)
        self.assertEqual(sampled("# no counters\n"), 0.0)
        state: dict = {}
        for moment, value in ((0.0, 0.0), (10.0, 0.0), (20.0, 1251.5)):
            CONTROL.observe_hot_job(
                now=moment, fan_rpms=[2700, 4050], hottest=90.0, busy=True,
                defaults={"PROGRESS_CHECK": "tokens"},
                state_dir=Path("/nonexistent-job-watch"),
                notifier=lambda _message: None, alert_repeat=1800,
                zero_fan_celsius=85, stuck_seconds=600, state=state,
                progress_reader=lambda _command, value=value: value,
                builtin_reader=lambda: (_ for _ in ()).throw(
                    AssertionError("zero is a hook sample"),
                ),
            )
            self.assertFalse(state.get("hook_failed"))
            self.assertEqual(state.get("source"), "hook")
        self.assertEqual(state.get("progress_value"), 1251.5)
        self.assertFalse(state.get("stuck"))


if __name__ == "__main__":
    unittest.main()
