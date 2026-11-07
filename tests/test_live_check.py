# SPDX-License-Identifier: GPL-2.0-only
"""The live harness stays opted in and is not part of ./scripts/check."""

import importlib.machinery
import importlib.util
import io
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOADER = importlib.machinery.SourceFileLoader(
    "dgx_live_check", str(ROOT / "research" / "live-check" / "live-check"),
)
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
LIVE = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(LIVE)


def snapshot(**overrides):
    base = {
        "hostname": "fixture",
        "profile": "EC 3.5.8",
        "module_loaded": True,
        "loaded_version": "0.1.7",
        "checkout_version": "0.1.7",
        "params": {
            "automatic_floor_recovery": "N",
            "allow_unverified_writes": "N",
        },
        "state": 0,
        "manual": "none",
        "service_active": "inactive",
        "watchdog_active": "active",
        "manual_unit": "disabled",
        "hottest_c": 42.0,
        "temperature_source": "status",
        "gpu_job": "none",
        "foreign": False,
        "cooling_device": True,
        "rpms": ["acpi_fan:fan1_input=2700"],
        "health": "healthy",
        "status_code": 0,
        "root": True,
        "manual_file": None,
    }
    base.update(overrides)
    return base


class LiveGateTests(unittest.TestCase):
    def test_check_script_does_not_launch_the_harness(self):
        text = (ROOT / "scripts" / "check").read_text(encoding="utf-8")
        self.assertNotIn("live-check", text)
        self.assertNotIn("DGX_FAN_LIVE", text)
        self.assertIn('discover -s "${root}/tests"', text)

    def test_missing_opt_in_does_not_collect(self):
        def collect(_run):
            raise AssertionError("collect")

        def run(*_args, **_kwargs):
            raise AssertionError("run")

        for env in ({}, {"DGX_FAN_LIVE": "1", "DGX_FAN_CONTROL_OFFLINE": "1"}):
            with self.subTest(env=env):
                out = io.StringIO()
                with redirect_stderr(out):
                    code = LIVE.main([], environ=env, collect=collect, run=run)
                self.assertEqual(code, 2)
                self.assertIn("refusing", out.getvalue())

    def test_case_without_consent_does_not_collect(self):
        def collect(_run):
            raise AssertionError("collect")

        out = io.StringIO()
        with redirect_stderr(out):
            code = LIVE.main(
                ["--case", "floor-automatic"],
                environ={"DGX_FAN_LIVE": "1"},
                collect=collect,
                run=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("run")),
            )
        self.assertEqual(code, 2)
        self.assertIn("without matching --consent", out.getvalue())

    def test_telemetry_only_profile_does_not_set_a_floor_or_start(self):
        calls = []

        def run(argv, timeout):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        snaps = [snapshot(
            profile="EC 3.13.7", cooling_device=False, state=None, manual="",
            manual_file="state=12\n",
        )]

        def collect(_run):
            return snaps[0]

        out = io.StringIO()
        with redirect_stdout(out):
            code = LIVE.main(
                ["--case", "floor-automatic", "--consent", "floor-automatic",
                 "--case", "service-start-stop", "--consent", "service-start-stop"],
                environ={"DGX_FAN_LIVE": "1"},
                collect=collect, run=run,
            )
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        text = out.getvalue()
        self.assertIn("MANUAL floor-automatic", text)
        self.assertIn("MANUAL service-start-stop", text)
        self.assertIn("telemetry only", text)
        self.assertNotIn("PASS floor-automatic", text)

    def test_unmet_preconditions_skip_before_any_command(self):
        calls = []

        def run(argv, timeout):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        out = io.StringIO()
        with redirect_stdout(out):
            code = LIVE.main(
                ["--case", "floor-automatic", "--consent", "floor-automatic"],
                environ={"DGX_FAN_LIVE": "1"},
                collect=lambda _run: snapshot(hottest_c=70.0, gpu_job="present"),
                run=run,
            )
        self.assertEqual(code, 3)
        self.assertEqual(calls, [])
        text = out.getvalue()
        self.assertIn("SKIP floor-automatic", text)
        self.assertIn("70.0C", text)
        self.assertIn("GPU job is present", text)

    def test_consented_floor_case_uses_only_set_state_and_automatic(self):
        calls = []
        status_n = {"n": 0}

        def run(argv, timeout):
            calls.append(list(argv))
            self.assertNotIn("enable", argv)
            self.assertNotIn("disable", argv)
            self.assertNotIn("reboot", argv)
            self.assertNotIn("poweroff", argv)
            self.assertNotIn("acpi-max", argv)
            if argv[:2] == ["dgx-fan-control", "status"]:
                status_n["n"] += 1
                state = "6" if status_n["n"] == 1 else "0"
                manual = "6 (last-set)" if state == "6" else "none"
                text = f"state={state}/12 hottest=42.0C\nmanual-floor: {manual}\n"
                return subprocess.CompletedProcess(argv, 0, text, "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        snaps = [
            snapshot(),
            snapshot(),
        ]

        def collect(_run):
            return snaps.pop(0)

        out = io.StringIO()
        with redirect_stdout(out):
            code = LIVE.main(
                ["--case", "floor-automatic", "--consent", "floor-automatic"],
                environ={"DGX_FAN_LIVE": "1"},
                collect=collect, run=run,
            )
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(calls, [
            ["dgx-fan-control", "set-state", "6"],
            ["dgx-fan-control", "status"],
            ["dgx-fan-control", "automatic"],
            ["dgx-fan-control", "status"],
        ])
        self.assertIn("PASS floor-automatic", out.getvalue())
        self.assertIn("MANUAL reboot-notifier", out.getvalue())

    def test_missing_temperature_is_not_a_read_only_pass(self):
        calls = []

        def run(argv, timeout):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = LIVE.main(
                ["--case", "unload-reload", "--consent", "unload-reload"],
                environ={"DGX_FAN_LIVE": "1"},
                collect=lambda _run: snapshot(
                    profile="EC 3.13.7", cooling_device=False, state=None,
                    hottest_c=None, temperature_source="missing (health=absent, zones=missing)",
                ),
                run=run,
            )
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        text = out.getvalue() + err.getvalue()
        self.assertIn("temperature was not read", text)
        self.assertNotIn("PASS read-only", text)
        self.assertIn("not a temperature pass", text)

    def test_health_temperature_is_trusted_and_malformed_is_not(self):
        value, note = LIVE.parse_health_temperature("temperature: pass (39.5C)\n")
        self.assertEqual((value, note), (39.5, "health"))
        warn, warn_note = LIVE.parse_health_temperature(
            "temperature: warn (hot under load, 70.0C)\n",
        )
        self.assertEqual((warn, warn_note), (70.0, "health"))
        for line in (
            "temperature: pass (hot)\n",
            "temperature: pass (0C)\n",
            "temperature: fail (hottest zone is not readable)\n",
            "fans: pass\n",
        ):
            parsed, kind = LIVE.parse_health_temperature(line)
            self.assertIsNone(parsed, line)
            self.assertNotEqual(kind, "health")
        chosen, source = LIVE.choose_temperature(
            None, "temperature: pass (39.5C)\n", (None, "missing"),
        )
        self.assertEqual((chosen, source), (39.5, "health"))
        missing, missing_source = LIVE.choose_temperature(
            None, "temperature: pass (nope)\n", (None, "malformed"),
        )
        self.assertIsNone(missing)
        self.assertIn("health=malformed", missing_source)
        self.assertFalse(LIVE.temperature_trusted(
            snapshot(hottest_c=None, temperature_source=missing_source),
        ))

    def test_kernel_zone_reader_ignores_malformed_samples(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, raw in (("thermal_zone0", "0"), ("thermal_zone1", "nope"),
                              ("thermal_zone2", "206000")):
                zone = root / name
                zone.mkdir()
                (zone / "temp").write_text(raw, encoding="ascii")
            self.assertEqual(LIVE.read_zone_temperature(root), (None, "malformed"))
            (root / "thermal_zone3").mkdir()
            (root / "thermal_zone3" / "temp").write_text("41250", encoding="ascii")
            self.assertEqual(LIVE.read_zone_temperature(root), (41.25, "thermal-zone"))

    def test_enabled_manual_unit_is_not_disabled_to_run_floor_case(self):
        calls = []

        def run(argv, timeout):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        out = io.StringIO()
        with redirect_stdout(out):
            code = LIVE.main(
                ["--case", "floor-automatic", "--consent", "floor-automatic"],
                environ={"DGX_FAN_LIVE": "1"},
                collect=lambda _run: snapshot(manual_unit="enabled"),
                run=run,
            )
        self.assertEqual(code, 3)
        self.assertEqual(calls, [])
        text = out.getvalue()
        self.assertIn("SKIP floor-automatic", text)
        self.assertIn("will not change that saved setting", text)
        self.assertNotIn("systemctl", text)
        self.assertEqual(
            LIVE.unmet("service-start-stop", snapshot(manual_unit="enabled")),
            [],
        )
        self.assertEqual(
            LIVE.unmet("unload-reload", snapshot(manual_unit="enabled")),
            [],
        )

    def test_zero_clamp_wording_is_not_a_state_zero_stop(self):
        text = " ".join(why for _name, why in LIVE.MANUAL)
        self.assertNotIn("state 0 as a written stop", text)
        self.assertIn("0xffff", text)
        self.assertIn("literal lower clamp of 0", text)

    def test_unload_does_not_pass_module_parameters(self):
        self.assertEqual(LIVE.commands_for("unload-reload")[1], ["modprobe", LIVE.MODULE])
        self.assertEqual(
            LIVE.commands_for("service-start-stop")[0],
            ["systemctl", "start", LIVE.DAEMON],
        )
        self.assertNotIn("enable", LIVE.commands_for("service-start-stop")[2])


if __name__ == "__main__":
    unittest.main()
