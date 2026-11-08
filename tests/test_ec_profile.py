# SPDX-License-Identifier: GPL-2.0-only
import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_loader = importlib.machinery.SourceFileLoader(
    "ec_profile", str(ROOT / "scripts" / "ec_profile.py"))
_spec = importlib.util.spec_from_loader("ec_profile", _loader)
EC = importlib.util.module_from_spec(_spec)
_loader.exec_module(EC)

GUID = EC.EC_FWUPD_GUID


def devices(version):
    return json.dumps({"Devices": [{"Guid": [GUID], "Version": version}]})


class Profile(unittest.TestCase):
    def test_release_names(self):
        self.assertEqual(EC.ec_release_name("0x03000508"), "3.5.8")
        self.assertEqual(EC.ec_release_name("0x03000d07"), "3.13.7")
        self.assertIsNone(EC.ec_release_name("nope"))

    def test_match_from_either_source(self):
        self.assertEqual(EC.classify(devices("0x03000508"), "", "3.5.8"), "OK")
        self.assertEqual(
            EC.classify("", "pinned EC 3.13.7 capabilities\n", "3.13.7"), "OK")

    def test_hidden_device_is_unverified(self):
        self.assertEqual(EC.classify("{}", "", "3.5.8"), "STOP_EC_UNVERIFIED")
        self.assertEqual(EC.classify("not-json", "", "3.13.7"), "STOP_EC_UNVERIFIED")

    def test_disagreement_is_a_mismatch(self):
        self.assertEqual(
            EC.classify(devices("0x03000d07"), "pinned EC 3.5.8 capabilities\n", "3.5.8"),
            "STOP_EC_MISMATCH",
        )
        self.assertEqual(
            EC.classify(devices("0x03000508"), "", "3.13.7"),
            "STOP_EC_MISMATCH",
        )

    def test_two_kernel_pins_disagree(self):
        log = (
            "pinned EC 3.5.8 capabilities\n"
            "pinned EC 3.13.7 capabilities\n"
        )
        self.assertEqual(EC.classify("", log, "3.5.8"), "STOP_EC_MISMATCH")

    def test_esrt_decimal_counts_when_fwupd_is_hidden(self):
        self.assertEqual(EC.u32_release("50332936"), "3.5.8")
        self.assertEqual(EC.u32_release("50334983"), "3.13.7")
        self.assertEqual(EC.classify("{}", "", "3.5.8", "50332936"), "OK")
        self.assertEqual(
            EC.explain("{}", "", "50332936", "3.5.8"),
            "fwupd=absent pin=absent esrt=3.5.8 verdict=OK",
        )

    def test_esrt_disagreement_still_stops(self):
        self.assertEqual(
            EC.classify(devices("0x03000d07"), "", "3.5.8", "50332936"),
            "STOP_EC_MISMATCH",
        )

    def test_esrt_directory_reads_only_the_ec_class(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            other = root / "entry0"
            match = root / "entry1"
            other.mkdir()
            match.mkdir()
            (other / "fw_class").write_text("b488217b-3895-4fc0-b1bf-ab7005a2d45a\n")
            (other / "fw_version").write_text("1\n")
            (match / "fw_class").write_text(GUID + "\n")
            (match / "fw_version").write_text("50332936\n")
            self.assertEqual(EC.esrt_versions(root), "50332936")


if __name__ == "__main__":
    unittest.main()
