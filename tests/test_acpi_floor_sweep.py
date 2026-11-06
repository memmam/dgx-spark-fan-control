# SPDX-License-Identifier: GPL-2.0-only
import importlib.machinery
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_loader = importlib.machinery.SourceFileLoader(
    "acpi_floor_sweep", str(ROOT / "scripts" / "acpi-floor-sweep"))
_spec = importlib.util.spec_from_loader("acpi_floor_sweep", _loader)
sweep_mod = importlib.util.module_from_spec(_spec)
_loader.exec_module(sweep_mod)


class FakeSpark:
    def __init__(self, base: Path, fans=(2000, 3000), temp_mc=40000):
        self.thermal = base / "thermal"
        self.hwmon = base / "hwmon"
        self.cooler = self.thermal / "cooling_device0"
        self.cooler.mkdir(parents=True)
        (self.cooler / "type").write_text("Fan\n")
        (self.cooler / "cur_state").write_text("100\n")
        zone = self.thermal / "thermal_zone0"
        zone.mkdir()
        (zone / "temp").write_text(f"{temp_mc}\n")
        self.fans = []
        for i, rpm in enumerate(fans):
            dev = self.hwmon / f"hwmon{i}"
            dev.mkdir(parents=True)
            (dev / "name").write_text("acpi_fan\n")
            (dev / "fan1_input").write_text(f"{rpm}\n")
            self.fans.append(dev)
        self.writes = []

    def level(self):
        return int((self.cooler / "cur_state").read_text())


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.spark = FakeSpark(Path(self.tmp.name))
        self.lines = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_sweep(self, levels, sleep=lambda _: None, **kw):
        args = dict(hold=3, interval=1, min_rpm=1000, max_temp=60.0)
        args.update(kw)
        return sweep_mod.sweep(levels, args["hold"], args["interval"],
                               args["min_rpm"], args["max_temp"],
                               self.spark.thermal, self.spark.hwmon,
                               self.lines.append, sleep=sleep,
                               now=lambda: "T")

    def test_full_sweep_restores_100(self):
        seen = []
        code = self.run_sweep([40, 35, 30],
                              sleep=lambda _: seen.append(self.spark.level()))
        self.assertEqual(code, 0)
        self.assertEqual(seen, [40] * 3 + [35] * 3 + [30] * 3)
        self.assertEqual(self.spark.level(), 100)
        self.assertIn("restored", self.lines[-1])

    def test_zero_rpm_aborts_and_restores(self):
        def sleep(_):
            if self.spark.level() == 35:
                (self.spark.fans[1] / "fan1_input").write_text("0\n")
        code = self.run_sweep([40, 35, 30], sleep=sleep)
        self.assertEqual(code, 2)
        self.assertEqual(self.spark.level(), 100)
        self.assertTrue(any("ABORT fan 2 reads 0 RPM" in l for l in self.lines))
        self.assertFalse(any(l.startswith("T,30,") for l in self.lines))

    def test_missing_reading_aborts(self):
        (self.spark.fans[0] / "fan1_input").unlink()
        self.assertEqual(self.run_sweep([40]), 2)
        self.assertTrue(any("fan 1 reading missing" in l for l in self.lines))
        self.assertEqual(self.spark.level(), 100)

    def test_low_rpm_and_heat_abort(self):
        (self.spark.fans[0] / "fan1_input").write_text("900\n")
        self.assertEqual(self.run_sweep([40]), 2)
        self.assertTrue(any("below --min-rpm 1000" in l for l in self.lines))
        (self.spark.fans[0] / "fan1_input").write_text("2000\n")
        (self.spark.thermal / "thermal_zone0" / "temp").write_text("61000\n")
        self.lines.clear()
        self.assertEqual(self.run_sweep([40]), 2)
        self.assertTrue(any("reached --max-temp" in l for l in self.lines))

    def test_interrupt_restores_100(self):
        def sleep(_):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_sweep([40], sleep=sleep)
        self.assertEqual(self.spark.level(), 100)

    def test_refuses_below_30_and_zero_before_writing(self):
        for bad in ([29], [40, 20], [0], [101]):
            with self.assertRaises(ValueError):
                self.run_sweep(bad)
            self.assertEqual(self.spark.level(), 100)
            self.assertEqual(self.lines, [])
        with self.assertRaises(ValueError):
            sweep_mod.write_level([self.spark.cooler], 0)


if __name__ == "__main__":
    unittest.main()
