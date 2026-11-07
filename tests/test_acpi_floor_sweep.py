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


class SweepRefusesBeforeWrite(unittest.TestCase):
    def test_main_exits_before_a_write(self):
        with tempfile.TemporaryDirectory() as directory:
            cooler = Path(directory) / "cooling_device0"
            cooler.mkdir()
            (cooler / "type").write_text("Fan\n", encoding="ascii")
            (cooler / "cur_state").write_text("40\n", encoding="ascii")
            (cooler / "max_state").write_text("100\n", encoding="ascii")
            code = sweep_mod.main(["40", "35"])
            self.assertEqual(code, 2)
            self.assertEqual((cooler / "cur_state").read_text(encoding="ascii"), "40\n")
            with self.assertRaises(RuntimeError):
                sweep_mod.write_level([cooler], 100)
            self.assertEqual((cooler / "cur_state").read_text(encoding="ascii"), "40\n")


if __name__ == "__main__":
    unittest.main()
