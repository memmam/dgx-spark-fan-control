# SPDX-License-Identifier: GPL-2.0-only
import importlib.machinery
import importlib.util
import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_loader = importlib.machinery.SourceFileLoader(
    "ec358_regression", str(ROOT / "scripts" / "ec358-regression"))
_spec = importlib.util.spec_from_loader("ec358_regression", _loader)
REG = importlib.util.module_from_spec(_spec)
_loader.exec_module(REG)

GUID = "3d13c989-e6a8-4ead-95ee-921f09868f65"


def result(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


def fwupd(version):
    return json.dumps({
        "Devices": [{"Guid": [GUID], "Version": version}],
    })


FW_358 = fwupd("0x03000508")
PIN_358 = "pinned EC 3.5.8 capabilities fan0=1260..9000\n"

GOOD_STATUS = (
    "state=0/12 (daemon not setting a floor; EC's own curve) hottest=42.0C\n"
    "watchdog-active: active\nwatchdog-enabled: enabled\n"
    "manual-floor: none\n"
)


def profile_reply(argv, fw=FW_358, kern=PIN_358, fw_code=0, kern_code=0):
    if argv == ["fwupdmgr", "get-devices", "--json"]:
        return result(code=fw_code, out=fw)
    if argv == ["dmesg"]:
        return result(code=kern_code, out=kern)
    return None


class Script(unittest.TestCase):
    def test_default_does_not_call_the_host(self):
        def boom(argv):
            raise AssertionError(argv)

        code = REG.main(["preflight"], env={}, runner=boom, reader=boom, fans=boom)
        self.assertEqual(code, 2)

    def test_mismatch_stops_before_status(self):
        seen = []

        def runner(argv):
            seen.append(argv)
            reply = profile_reply(argv, fw=fwupd("0x03000d07"), kern=PIN_358)
            if reply is None:
                raise AssertionError(argv)
            return reply

        code = REG.main(["preflight"], env={REG.OPT_IN: "1"}, runner=runner)
        self.assertEqual(code, 1)
        self.assertEqual(seen, [
            ["fwupdmgr", "get-devices", "--json"],
            ["dmesg"],
        ])

    def test_hidden_ec_without_pin_stops(self):
        seen = []

        def runner(argv):
            seen.append(argv)
            reply = profile_reply(argv, fw="{}", kern="", fw_code=0)
            if reply is None:
                raise AssertionError(argv)
            return reply

        code = REG.main(["preflight"], env={REG.OPT_IN: "1"}, runner=runner)
        self.assertEqual(code, 1)
        self.assertEqual(seen[0], ["fwupdmgr", "get-devices", "--json"])
        self.assertNotIn(["dgx-fan-control", "status"], seen)

    def test_kernel_pin_allows_hidden_fwupd_device(self):
        seen = []

        def runner(argv):
            seen.append(argv)
            reply = profile_reply(argv, fw="{}", kern=PIN_358)
            if reply is not None:
                return reply
            if argv == ["systemctl", "is-active", "dgx-fan-control.service"]:
                return result(code=3, out="inactive\n")
            if argv == ["dgx-fan-control", "status"]:
                return result(out=GOOD_STATUS)
            raise AssertionError(argv)

        code = REG.main(["preflight"], env={REG.OPT_IN: "1"}, runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(seen[0], ["fwupdmgr", "get-devices", "--json"])
        self.assertEqual(seen[1], ["dmesg"])

    def test_preflight_is_read_only(self):
        seen = []

        def runner(argv):
            seen.append(argv)
            reply = profile_reply(argv)
            if reply is not None:
                return reply
            if argv == ["systemctl", "is-active", "dgx-fan-control.service"]:
                return result(code=3, out="inactive\n")
            if argv == ["dgx-fan-control", "status"]:
                return result(out=GOOD_STATUS)
            raise AssertionError(argv)

        code = REG.main(["preflight"], env={REG.OPT_IN: "1"}, runner=runner)
        self.assertEqual(code, 0)
        self.assertNotIn("set-state", str(seen))
        self.assertNotIn("modprobe", str(seen))

    def test_status_text_still_rejects_other_firmware(self):
        def runner(argv):
            reply = profile_reply(argv)
            if reply is not None:
                return reply
            if argv == ["systemctl", "is-active", "dgx-fan-control.service"]:
                return result(code=3, out="inactive\n")
            if argv == ["dgx-fan-control", "status"]:
                return result(out=GOOD_STATUS + "control: unsupported (EC 3.13.7)\n")
            raise AssertionError(argv)

        code = REG.main(["preflight"], env={REG.OPT_IN: "1"}, runner=runner)
        self.assertEqual(code, 1)

    def test_floor_round_trip_does_not_change_acpi_fans(self):
        fans = [["/sys/class/thermal/cooling_device1/Fan", "40"]]
        state = {"n": 0}

        def runner(argv):
            reply = profile_reply(argv)
            if reply is not None:
                return reply
            if argv[:3] == ["systemctl", "is-active", "dgx-fan-control.service"]:
                return result(code=3, out="inactive\n")
            if argv == ["modinfo", "-F", "version", "dgx_ec_fan_control"]:
                return result(out="0.1.8\n")
            if argv == ["dgx-fan-control", "acpi-max"]:
                return result(code=2, err="invalid choice\n")
            if argv == ["sudo", "dgx-fan-control", "set-state", "6"]:
                state["n"] = 6
                return result()
            if argv == ["sudo", "dgx-fan-control", "automatic"]:
                state["n"] = 0
                return result()
            if argv == ["dgx-fan-control", "status"]:
                if state["n"] == 6:
                    text = GOOD_STATUS.replace("state=0/12", "state=6/12", 1)
                    text = text.replace(" (daemon not setting a floor; EC's own curve)", "")
                    return result(out=text)
                return result(out=GOOD_STATUS)
            raise AssertionError(argv)

        code = REG.main(
            ["floor"], env={REG.OPT_IN: "1"}, runner=runner,
            reader=lambda path: "same-bytes", fans=lambda: list(fans),
        )
        self.assertEqual(code, 0)
        self.assertEqual(fans, [["/sys/class/thermal/cooling_device1/Fan", "40"]])
        self.assertEqual(state["n"], 0)

    def test_floor_stops_when_the_installed_command_differs(self):
        def runner(argv):
            reply = profile_reply(argv)
            if reply is not None:
                return reply
            if argv[:3] == ["systemctl", "is-active", "dgx-fan-control.service"]:
                return result(code=3, out="inactive\n")
            if argv == ["dgx-fan-control", "status"]:
                return result(out=GOOD_STATUS)
            if argv == ["modinfo", "-F", "version", "dgx_ec_fan_control"]:
                return result(out="0.1.8\n")
            raise AssertionError(argv)

        reads = {REG.INSTALLED_CLI: "old", REG.SOURCE_CLI: "new"}
        code = REG.main(
            ["floor"], env={REG.OPT_IN: "1"}, runner=runner,
            reader=lambda path: reads[path], fans=lambda: [],
        )
        self.assertEqual(code, 1)

    def test_service_reaches_state_one_then_returns_to_automatic(self):
        fans = [["/sys/class/thermal/cooling_device1/Fan", "40"]]
        phase = {"n": 0}
        stopped = {"n": 0}

        def runner(argv):
            reply = profile_reply(argv)
            if reply is not None:
                return reply
            if argv == ["systemctl", "is-active", REG.DAEMON_UNIT]:
                if phase["n"] == 1:
                    return result(out="active\n")
                return result(code=3, out="inactive\n")
            if argv == ["modinfo", "-F", "version", "dgx_ec_fan_control"]:
                return result(out="0.1.8\n")
            if argv == ["sudo", "systemctl", "start", REG.DAEMON_UNIT]:
                phase["n"] = 1
                return result()
            if argv == ["sudo", "systemctl", "stop", REG.DAEMON_UNIT]:
                stopped["n"] += 1
                phase["n"] = 0
                return result()
            if argv == ["dgx-fan-control", "status"]:
                if phase["n"] == 1 and stopped["n"] == 0:
                    text = GOOD_STATUS.replace("state=0/12", "state=1/12", 1)
                    text = text.replace(
                        " (daemon not setting a floor; EC's own curve)", "")
                    return result(out=text)
                return result(out=GOOD_STATUS)
            raise AssertionError(argv)

        code = REG.main(
            ["service"], env={REG.OPT_IN: "1"}, runner=runner,
            reader=lambda path: "same-bytes", fans=lambda: list(fans),
            sleep=lambda _seconds: None,
        )
        self.assertEqual(code, 0)
        self.assertEqual(stopped["n"], 1)
        self.assertEqual(fans, [["/sys/class/thermal/cooling_device1/Fan", "40"]])

    def test_service_stops_the_unit_if_the_daemon_leaves_state_one(self):
        stopped = {"n": 0}
        polls = {"n": 0}

        def runner(argv):
            reply = profile_reply(argv)
            if reply is not None:
                return reply
            if argv == ["systemctl", "is-active", REG.DAEMON_UNIT]:
                return result(code=3, out="inactive\n")
            if argv == ["modinfo", "-F", "version", "dgx_ec_fan_control"]:
                return result(out="0.1.8\n")
            if argv == ["sudo", "systemctl", "start", REG.DAEMON_UNIT]:
                return result()
            if argv == ["sudo", "systemctl", "stop", REG.DAEMON_UNIT]:
                stopped["n"] += 1
                return result()
            if argv == ["dgx-fan-control", "status"]:
                polls["n"] += 1
                if polls["n"] == 1:
                    return result(out=GOOD_STATUS)
                text = GOOD_STATUS.replace("state=0/12", "state=12/12", 1)
                text = text.replace(
                    " (daemon not setting a floor; EC's own curve)", "")
                return result(out=text)
            raise AssertionError(argv)

        code = REG.main(
            ["service"], env={REG.OPT_IN: "1"}, runner=runner,
            reader=lambda path: "same-bytes", fans=lambda: [],
            sleep=lambda _seconds: None,
        )
        self.assertEqual(code, 1)
        self.assertEqual(stopped["n"], 1)

    def test_install_commands_remove_only_other_versions(self):
        commands = REG.install_commands("7.0.0-1024-nvidia", ["0.1.7", "0.1.8"])
        flat = [" ".join(item) for item in commands]
        self.assertIn("sudo dkms remove -m dgx-spark-fan-control -v 0.1.7 --all", flat)
        self.assertNotIn("sudo dkms remove -m dgx-spark-fan-control -v 0.1.8 --all", flat)
        self.assertIn(
            "sudo dkms install -m dgx-spark-fan-control -v 0.1.8 -k 7.0.0-1024-nvidia",
            flat,
        )
        self.assertTrue(all("acpi-max" not in line for line in flat))


if __name__ == "__main__":
    unittest.main()
