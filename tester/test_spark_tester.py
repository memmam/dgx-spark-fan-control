#!/usr/bin/env python3
"""Run the real launchers against the fixture machine.

Oracles read the machine's sample log and the proc text. They do not call
the launcher's median or the reply classifier to discover the expected value.
"""
import os
import re
import shutil
import signal
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from tester.isolate import Session
from tester.machine import EVIDENCE, RAMP_LEVELS

ROOT = Path(__file__).resolve().parents[1]
TOOLS = None

W_LINE = re.compile(
    r"^W (\d+) pre_a0=([0-9a-fA-F]+) pre_a4=([0-9a-fA-F]+) "
    r"post_a0=([0-9a-fA-F]+) post_a4=([0-9a-fA-F]+) "
    r"handler=(-?\d+) post_ok=([01])$"
)
R_LINE = re.compile(
    r"^R (\d+) pre_a0=([0-9a-fA-F]+) pre_a4=([0-9a-fA-F]+) "
    r"post_a0=([0-9a-fA-F]+) post_a4=([0-9a-fA-F]+) "
    r"handler=(-?\d+) post_ok=([01])$"
)


def compile_tools(dst: Path) -> None:
    subprocess.check_call(
        ["gcc", "-shared", "-fPIC", "-O2", "-Wall", "-o", str(dst / "preload.so"),
         str(ROOT / "tester" / "preload.c"), "-ldl",
         "-Wl,--version-script=" + str(ROOT / "tester" / "preload.map")],
    )
    subprocess.check_call(
        ["gcc", "-O2", "-Wall", "-o", str(dst / "fastshim"), str(ROOT / "tester" / "fastshim.c")],
    )
    common = [
        "gcc", "-O2", "-Wall",
        "-I", str(ROOT / "tester" / "include"),
        "-include", str(ROOT / "tester" / "kernel_stubs.h"),
    ]
    subprocess.check_call(
        common + [
            "-include", str(ROOT / "research" / "fsl110" / "fsl_reply.c"),
            "-Wno-unused-function",
            "-o", str(dst / "reply_harness"),
            str(ROOT / "tester" / "harness_main.c"),
            "-pthread",
        ],
    )
    subprocess.check_call(
        common + [
            "-DFSL_REPLY_TEXT=174",
            "-Wno-unused-function",
            "-include", str(ROOT / "research" / "fsl110" / "fsl_reply.c"),
            "-o", str(dst / "reply_small"),
            str(ROOT / "tester" / "harness_main.c"),
            "-pthread",
        ],
    )
    mutated = dst / "fsl_reply_scnprintf.c"
    mutated.write_text(
        (ROOT / "research" / "fsl110" / "fsl_reply.c").read_text(encoding="utf-8").replace(
            "snprintf(", "scnprintf("
        ),
        encoding="utf-8",
    )
    subprocess.check_call(
        common + [
            "-DFSL_REPLY_TEXT=174",
            "-Wno-unused-function",
            "-include", str(mutated),
            "-o", str(dst / "reply_clipped"),
            str(ROOT / "tester" / "harness_main.c"),
            "-pthread",
        ],
    )
    finit = dst / "finit.c"
    finit.write_text(textwrap.dedent("""\
        #define _GNU_SOURCE
        #include <errno.h>
        #include <fcntl.h>
        #include <stdio.h>
        #include <sys/syscall.h>
        #include <unistd.h>
        int main(void) {
          int fd = open("/dev/null", O_RDONLY);
          long rc = syscall(SYS_finit_module, fd, "", 0);
          printf("rc=%ld errno=%d\\n", rc, errno);
          return 0;
        }
        """), encoding="ascii")
    subprocess.check_call(["gcc", "-O2", "-o", str(dst / "finit"), str(finit)])


def setUpModule():
    global TOOLS
    TOOLS = Path(tempfile.mkdtemp(prefix="dgx-tester-"))
    compile_tools(TOOLS)


def tearDownModule():
    if TOOLS is not None:
        shutil.rmtree(TOOLS, ignore_errors=True)


def harness(binary: Path, script: str) -> str:
    proc = subprocess.run(
        [str(binary)], input=script, text=True, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return proc.stdout


def field(text: str, name: str):
    match = re.search(rf"^{name} (\d+)$", text, re.M)
    if match is None:
        return None
    return int(match.group(1))


def reply_problems(text: str, *, expect_complete: bool) -> list:
    """Independent capture contract. Empty means the text is a complete capture
    when expect_complete is set; otherwise the list is why it is not.
    """
    problems = []
    names = ("writes", "reads", "dropped_writes", "dropped_reads", "nmissed", "truncated")
    values = {name: field(text, name) for name in names}
    if any(value is None for value in values.values()):
        return ["malformed"]
    records = []
    for line in text.splitlines():
        if line.startswith("W ") or line.startswith("R "):
            records.append(line)
    partial = [line for line in records if W_LINE.match(line) is None and R_LINE.match(line) is None]
    if partial:
        problems.append("partial")
    writes = [line for line in records if line.startswith("W ")]
    reads = [line for line in records if line.startswith("R ")]
    if values["truncated"] == 0 and not partial:
        if len(writes) != values["writes"] or len(reads) != values["reads"]:
            problems.append("count_mismatch")
    if values["dropped_writes"]:
        problems.append(f"dropped_writes={values['dropped_writes']}")
    if values["dropped_reads"]:
        problems.append(f"dropped_reads={values['dropped_reads']}")
    if values["nmissed"]:
        problems.append(f"nmissed={values['nmissed']}")
    if values["truncated"]:
        problems.append("truncated")
    bad_copy = 0
    bad_opcode = 0
    for line in writes:
        match = W_LINE.match(line)
        if match is None:
            continue
        if match.group(7) != "1":
            bad_copy += 1
        if int(match.group(3), 16) & 0xFF != 0x09:
            bad_opcode += 1
    if bad_copy:
        problems.append("post_ok")
    if bad_opcode:
        problems.append("opcode")
    if expect_complete and values["writes"] != 4:
        problems.append(f"writes={values['writes']}")
    return problems


def classify(text: str) -> str:
    proc = subprocess.run(
        ["python3", str(ROOT / "research" / "fsl110" / "classify-reply.py")],
        input=text, text=True, capture_output=True, check=True,
    )
    return proc.stdout.strip()


def median5(values):
    last = list(values)[-5:]
    if len(last) != 5:
        raise AssertionError(f"need 5 samples, got {last}")
    return sorted(last)[2]


def completed(machine, path):
    return [item for item in machine.completed_writes if item["path"] == path and not item["rejected"]]


def median_problems(output: str, machine) -> list:
    problems = []
    printed = re.findall(r"median_last5 \S+=(\d+) \S+=(\d+)", output)
    baseline = re.findall(r"baseline_median_last5 (\d+) (\d+)", output)
    if len(machine.samples) < 15:
        return ["no baseline samples"]
    groups = [machine.samples[index:index + 15] for index in range(0, len(machine.samples), 15)]
    if baseline:
        got = (int(baseline[0][0]), int(baseline[0][1]))
        want = (median5([item[0] for item in groups[0]]), median5([item[1] for item in groups[0]]))
        if got != want:
            problems.append(f"baseline median {got} != {want}")
    # One printed post median per completed dwell after the baseline.
    post_groups = groups[1:1 + len(printed)]
    if len(post_groups) != len(printed):
        problems.append(f"median lines {len(printed)} dwells {len(post_groups)}")
        return problems
    for group, line in zip(post_groups, printed):
        if any(item[0] == "bad" for item in group):
            continue
        got = (int(line[0]), int(line[1]))
        want = (median5(item[0] for item in group), median5(item[1] for item in group))
        if got != want:
            problems.append(f"median {got} != served last5 {want}")
    return problems


class ReplyBufferTests(unittest.TestCase):
    def test_ninth_write_is_dropped_and_does_not_evict(self):
        lines = [f"event {(index + 1) << 8 | 0x09:x} 1 0 0 0" for index in range(9)]
        text = harness(TOOLS / "reply_harness", "\n".join(lines + ["dump"]) + "\n")
        self.assertEqual(field(text, "writes"), 8)
        self.assertEqual(field(text, "dropped_writes"), 1)
        stored = [int(W_LINE.match(line).group(3), 16) for line in text.splitlines() if line.startswith("W ")]
        self.assertEqual(stored, [((index + 1) << 8) | 0x09 for index in range(8)])

    def test_reads_do_not_evict_writes(self):
        lines = [f"event {0x09:x} 1 0 0 0" for _ in range(8)]
        lines += [f"event {0x0c:x} 1 0 0 0" for _ in range(40)]
        text = harness(TOOLS / "reply_harness", "\n".join(lines + ["dump"]) + "\n")
        self.assertEqual(field(text, "writes"), 8)
        self.assertEqual(field(text, "dropped_writes"), 0)
        self.assertEqual(field(text, "reads"), 32)
        self.assertEqual(field(text, "dropped_reads"), 8)

    def test_concurrent_entries_keep_eight_and_count_the_rest(self):
        text = harness(TOOLS / "reply_harness", "concurrent 20\ndump\n")
        self.assertEqual(field(text, "writes"), 8)
        self.assertEqual(field(text, "dropped_writes"), 12)
        self.assertEqual(field(text, "writes") + field(text, "dropped_writes"), 20)
        for line in text.splitlines():
            match = W_LINE.match(line)
            if match:
                self.assertEqual(int(match.group(3), 16) & 0xFF, 0x09)

    def test_failed_copy_is_not_a_complete_capture(self):
        lines = ["event 9 c400008e 0 1 0" for _ in range(4)]
        text = harness(TOOLS / "reply_harness", "\n".join(lines + ["dump"]) + "\n")
        problems = reply_problems(text, expect_complete=True)
        self.assertIn("post_ok", problems)
        self.assertNotEqual(classify(text), "CAPTURE_COMPLETE")
        self.assertIn("post_ok", classify(text))

    def test_missed_probe_is_visible(self):
        lines = ["missed 2"] + ["event 9 c400008e 0 0 0" for _ in range(4)] + ["dump"]
        text = harness(TOOLS / "reply_harness", "\n".join(lines) + "\n")
        self.assertIn("nmissed=2", reply_problems(text, expect_complete=True))
        self.assertIn("nmissed=2", classify(text))

    def test_complete_capture_agrees_with_the_classifier(self):
        lines = ["event 9 c400008e 0 0 0" for _ in range(4)]
        lines += ["event c 11 0 0 0", "event c 11 0 0 0", "dump"]
        text = harness(TOOLS / "reply_harness", "\n".join(lines) + "\n")
        self.assertEqual(reply_problems(text, expect_complete=True), [])
        self.assertEqual(classify(text), "CAPTURE_COMPLETE")

    def test_short_buffer_sets_truncated_and_does_not_emit_a_partial_line(self):
        script = "event ffffffffffffffff ffffffffffffffff ffffffffffffffff 0 0\ndump\n"
        text = harness(TOOLS / "reply_small", script)
        problems = reply_problems(text, expect_complete=False)
        self.assertIn("truncated", problems)
        self.assertNotIn("partial", problems)
        self.assertEqual(field(text, "truncated"), 1)
        self.assertNotEqual(classify(text), "CAPTURE_COMPLETE")

    def test_mutation_clipped_scnprintf_fails_the_contract(self):
        script = "event ffffffffffffffff ffffffffffffffff ffffffffffffffff 0 0\ndump\n"
        text = harness(TOOLS / "reply_clipped", script)
        partial = [
            line for line in text.splitlines()
            if line.startswith(("W ", "R "))
            and W_LINE.match(line) is None and R_LINE.match(line) is None
        ]
        self.assertTrue(partial, text)
        self.assertNotEqual(reply_problems(text, expect_complete=False), [])

    def test_mutation_classifier_that_ignores_a_failed_copy(self):
        lines = ["event 9 c400008e 0 1 0" for _ in range(4)]
        text = harness(TOOLS / "reply_harness", "\n".join(lines + ["dump"]) + "\n")
        mutated = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
        source = (ROOT / "research" / "fsl110" / "classify-reply.py").read_text(encoding="utf-8")
        source = source.replace(
            "def classify(text: str) -> str:\n",
            "def classify(text: str) -> str:\n    return \"CAPTURE_COMPLETE\"\n",
            1,
        )
        mutated.write(source)
        mutated.close()
        try:
            proc = subprocess.run(
                ["python3", mutated.name], input=text, text=True, capture_output=True, check=True,
            )
        finally:
            os.unlink(mutated.name)
        self.assertEqual(proc.stdout.strip(), "CAPTURE_COMPLETE")
        self.assertIn("post_ok", reply_problems(text, expect_complete=True))
        self.assertNotEqual(classify(text), "CAPTURE_COMPLETE")


class LauncherTests(unittest.TestCase):
    def run_script(self, script, scenario, extra, mutate=None, timeout=180, signed=True, signing=False):
        session = Session(ROOT, TOOLS, scenario, mutate=mutate, signed=signed, signing=signing)
        started = time.perf_counter()
        try:
            result = session.run(["bash", script], extra_env=extra, timeout=timeout)
            elapsed = time.perf_counter() - started
            return session, result, elapsed
        except Exception:
            session.close()
            raise

    def close(self, session):
        session.close()

    def test_full_ramp_median_hold_and_observed_reboot(self):
        scenario = {"ec": "3.5.8", "baseline": (2700, 4050), "tach": "rise"}
        session, result, elapsed = self.run_script(
            "ramp-ec358.sh", scenario, {"DGX_FSL_RAMP_EC358": "yes"},
        )
        try:
            out = result.stdout
            self.assertEqual(result.returncode, 0, out[-2000:])
            self.assertIn("RAMP_STEPS_DONE", out)
            self.assertIn("REBOOT_NOW", out)
            self.assertIn("unloaded; this did not undo a hold", out)
            self.assertIn("acpitz=42000mC", out)
            self.assertEqual(median_problems(out, session.machine), [], out[-1500:])
            writes = completed(session.machine, "/proc/fsl_ramp")
            self.assertEqual(len(writes), len(RAMP_LEVELS))
            self.assertEqual(session.machine.virtual_seconds, 14 * 14 * 2)
            self.assertLess(elapsed, session.machine.virtual_seconds)
            self.assertEqual(session.machine.aml_requested, 237)
            self.assertNotEqual(tuple(session.machine.reported), (2700, 4050))
            floor = (session.state / "sys/class/thermal/cooling_device0/cur_state").read_text()
            fan = (session.state / "sys/class/thermal/cooling_device1/cur_state").read_text()
            self.assertEqual(floor, "0\n")
            self.assertEqual(fan, "30\n")
            (session.state / "sys/class/thermal/cooling_device0/cur_state").write_text("6\n")
            session.machine.production_floor = 0
            session.machine.reboot_research()
            self.assertEqual(session.machine.reported, [2700, 4050])
            self.assertEqual(session.machine.aml_requested, 0)
            self.assertIsNone(session.machine.firmware_accepted)
            self.assertEqual(session.machine.production_floor, 0)
            self.assertEqual(
                (session.state / "sys/class/thermal/cooling_device0/cur_state").read_text(),
                "6\n",
            )
            evidence = (session.state / "evidence.txt").read_text()
            self.assertIn(EVIDENCE["reported_idle"], evidence)
            self.assertIn(EVIDENCE["production_floor"], evidence)
            self.assertNotIn("research hold survives warm reboot", evidence)
            print(f"full ramp wall {elapsed:.2f}s virtual {session.machine.virtual_seconds}s")
        finally:
            self.close(session)

    def test_spread_median_is_not_the_minimum(self):
        scenario = {"ec": "3.5.8", "baseline": (2700, 4050), "tach": "spread"}
        session, result, _elapsed = self.run_script(
            "ramp-ec358.sh", scenario, {"DGX_FSL_RAMP_EC358": "yes"},
        )
        try:
            self.assertEqual(result.returncode, 0, result.stdout[-1500:])
            self.assertEqual(median_problems(result.stdout, session.machine), [])
            self.assertIn("median_last5", result.stdout)
            self.assertRegex(result.stdout, r"median_last5 \S+=5200 \S+=6200 ")
        finally:
            self.close(session)

    def test_one_fan_flat_continues_and_both_flat_stops(self):
        one = {"ec": "3.5.8", "baseline": (2700, 4050), "tach": "one_flat", "flat_at": 170}
        session, result, _elapsed = self.run_script(
            "ramp-ec358.sh", one, {"DGX_FSL_RAMP_EC358": "yes"},
        )
        try:
            self.assertEqual(result.returncode, 0, result.stdout[-1500:])
            self.assertIn("PLATEAU_RECORDED", result.stdout)
            self.assertIn("writing step for 180", result.stdout)
            self.assertEqual(len(completed(session.machine, "/proc/fsl_ramp")), 13)
        finally:
            self.close(session)
        both = {"ec": "3.5.8", "baseline": (2700, 4050), "tach": "both_flat", "flat_at": 180}
        session, result, _elapsed = self.run_script(
            "ramp-ec358.sh", both, {"DGX_FSL_RAMP_EC358": "yes"},
        )
        try:
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("STOP_PLATEAU", result.stdout)
            self.assertNotIn("writing step for 190", result.stdout)
            self.assertEqual(len(completed(session.machine, "/proc/fsl_ramp")), 3)
            self.assertIn("unloaded; this did not undo a hold", result.stdout)
        finally:
            self.close(session)

    def test_a_fall_stops_and_a_post_write_fault_does_not_take_the_next_step(self):
        fall = {"ec": "3.5.8", "baseline": (2700, 4050), "tach": "fall", "fall_at": 170}
        session, result, _elapsed = self.run_script(
            "ramp-ec358.sh", fall, {"DGX_FSL_RAMP_EC358": "yes"},
        )
        try:
            self.assertIn("STOP_DECREASE", result.stdout)
            self.assertNotIn("writing step for 180", result.stdout)
            self.assertEqual(len(completed(session.machine, "/proc/fsl_ramp")), 2)
        finally:
            self.close(session)
        fault = {
            "ec": "3.5.8", "baseline": (2700, 4050), "tach": "rise", "fault_pairs": [16],
        }
        session, result, _elapsed = self.run_script(
            "ramp-ec358.sh", fault, {"DGX_FSL_RAMP_EC358": "yes"},
        )
        try:
            self.assertIn("DWELL_INCOMPLETE", result.stdout)
            self.assertNotIn("writing step for 170", result.stdout)
            self.assertEqual(len(completed(session.machine, "/proc/fsl_ramp")), 1)
            self.assertIn("unloaded; this did not undo a hold", result.stdout)
            self.assertNotEqual(tuple(session.machine.reported), (2700, 4050))
            self.assertNotIn("\n0\n", "\n".join(item["text"] for item in session.machine.completed_writes))
        finally:
            self.close(session)

    def test_baseline_failures_do_not_load_or_write(self):
        cases = [
            ({"ec": "3.5.8", "baseline": (2610, 3915), "tach": "rise"}, "STOP_NOT_BASELINE"),
            ({"ec": "3.5.8", "baseline": (2700, 4050), "tach": "rise", "bad_temp": True}, "STOP_TEMP"),
            ({"ec": "3.5.8", "baseline": (2700, 4050), "tach": "rise", "gpu_override": {6: "5, 40"}}, "STOP_GPU"),
            ({"ec": "3.5.8", "baseline": (2700, 4050), "tach": "rise", "gpu_override": {0: "N/A, 41"}}, "nonnumeric"),
        ]
        for scenario, marker in cases:
            with self.subTest(marker=marker):
                session, result, _elapsed = self.run_script(
                    "ramp-ec358.sh", scenario, {"DGX_FSL_RAMP_EC358": "yes"},
                )
                try:
                    self.assertIn(marker, result.stdout)
                    self.assertEqual(session.machine.modules, set(), result.stdout[-800:])
                    self.assertEqual(session.machine.completed_writes, [])
                    self.assertEqual(session.machine.attempts, [])
                finally:
                    self.close(session)

    def test_gpu_note_between_one_and_four_does_not_stop(self):
        scenario = {
            "ec": "3.5.8", "baseline": (2700, 4050), "tach": "rise",
            "gpu_override": {17: "3, 40"},
        }
        session, result, _elapsed = self.run_script(
            "ramp-ec358.sh", scenario, {"DGX_FSL_RAMP_EC358": "yes"},
        )
        try:
            self.assertEqual(result.returncode, 0, result.stdout[-800:])
            self.assertIn("GPU_NOTED util=3", result.stdout)
            self.assertIn("RAMP_STEPS_DONE", result.stdout)
            self.assertIn(EVIDENCE["accept_358"].split(".")[0], (session.state / "evidence.txt").read_text())
        finally:
            self.close(session)

    def test_blocked_proc_write_is_not_retried(self):
        scenario = {"ec": "3.5.8", "baseline": (2700, 4050), "tach": "rise", "block_proc": True}
        session = Session(ROOT, TOOLS, scenario)
        proc = session.popen(["bash", "ramp-ec358.sh"], {"DGX_FSL_RAMP_EC358": "yes"})
        hostlog_path = session.state / "work/research/fsl110/ramp-ec358-hostlog.txt"
        try:
            hostlog = ""
            deadline = time.perf_counter() + 20
            while time.perf_counter() < deadline:
                if proc.poll() is not None:
                    break
                if hostlog_path.exists():
                    hostlog = hostlog_path.read_text(encoding="utf-8", errors="replace")
                    if "writing step" in hostlog:
                        break
                time.sleep(0.2)
            self.assertIsNone(proc.poll(), "ramp returned while the proc write was blocked\n" + hostlog[-800:])
            hostlog = hostlog_path.read_text(encoding="utf-8", errors="replace")
            self.assertEqual(hostlog.count("writing step"), 1, hostlog[-1000:])
            self.assertNotIn("dwell after", hostlog)
            self.assertEqual(session.machine.attempts, ["/proc/fsl_ramp"])
            self.assertEqual(session.machine.completed_writes, [])
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            session.close()

    def test_reply_capture_and_failed_copy_banner(self):
        ok = {
            "ec": "3.13.7", "baseline": (2066, 2156), "tach": "hold",
        }
        session, result, elapsed = self.run_script(
            "reply-ec3137.sh", ok, {"DGX_FSL_REPLY_EC3137": "yes"},
        )
        try:
            out = result.stdout
            self.assertEqual(result.returncode, 0, out[-2000:])
            self.assertLess(out.index("reply observer loaded"), out.index("loaded probe"))
            self.assertLess(out.index("state armed"), out.index("writing run"))
            self.assertIn("CAPTURE_COMPLETE", out)
            self.assertIn("REPLY_DONE", out)
            self.assertIn("hottest=", out)
            self.assertIn("zones", out)
            self.assertEqual(reply_problems(session.machine.reply_text, expect_complete=True), [])
            self.assertEqual(len(completed(session.machine, "/proc/fsl_probe")), 1)
            self.assertEqual(session.machine.reported, [2066, 2156])
            self.assertEqual(session.machine.aml_requested, 120)
            self.assertIsNone(session.machine.firmware_accepted)
            self.assertEqual(session.machine.virtual_seconds, 28 * 2)
            self.assertLess(elapsed, session.machine.virtual_seconds)
            self.assertIn("unloaded probe; this did not undo a hold", out)
            print(f"reply wall {elapsed:.2f}s virtual {session.machine.virtual_seconds}s")
        finally:
            self.close(session)
        bad = {"ec": "3.13.7", "baseline": (2066, 2156), "tach": "hold", "reply_fail_copy": True}
        session, result, _elapsed = self.run_script(
            "reply-ec3137.sh", bad, {"DGX_FSL_REPLY_EC3137": "yes"},
        )
        try:
            self.assertIn("REPLY_INCONCLUSIVE", result.stdout)
            self.assertIn("not a result", result.stdout)
            self.assertNotIn("REPLY_DONE", result.stdout)
            self.assertIn("post_ok", reply_problems(session.machine.reply_text, expect_complete=True))
            self.assertEqual(len(completed(session.machine, "/proc/fsl_probe")), 1)
            self.assertEqual(session.machine.reported, [2066, 2156])
        finally:
            self.close(session)

    def test_reply_baseline_failure_loads_nothing(self):
        scenario = {
            "ec": "3.13.7", "baseline": (2066, 2156), "tach": "hold", "gpu_fail_at": 0,
        }
        session, result, _elapsed = self.run_script(
            "reply-ec3137.sh", scenario, {"DGX_FSL_REPLY_EC3137": "yes"},
        )
        try:
            self.assertIn("STOP_GPU", result.stdout)
            self.assertEqual(session.machine.modules, set())
            self.assertEqual(session.machine.attempts, [])
        finally:
            self.close(session)

    def test_unsigned_module_never_reaches_insmod(self):
        scenario = {"ec": "3.5.8", "baseline": (2700, 4050), "tach": "rise"}
        session, result, _elapsed = self.run_script(
            "ramp-ec358.sh", scenario, {"DGX_FSL_RAMP_EC358": "yes"}, signed=False,
        )
        try:
            self.assertIn("STOP_UNSIGNED", result.stdout, f"rc={result.returncode} tail={result.stdout[-400:]!r}")
            self.assertEqual(session.machine.attempts, [])
        finally:
            self.close(session)

    def test_sign_all_research_modules_and_the_missing_probe_mutation(self):
        scenario = {"ec": "3.5.8", "baseline": (2700, 4050), "tach": "hold"}
        session, result, _elapsed = self.run_script(
            "build-sign-reply.sh", scenario, {}, signing=True,
        )
        try:
            self.assertEqual(result.returncode, 0, result.stdout[-1500:])
            names = {Path(line).name for line in (session.state / "sign-calls").read_text().splitlines()}
            self.assertEqual(names, set(MODULES) if False else {
                "fsl110.ko", "fsl_ramp.ko", "fsl_probe.ko", "fst_status.ko", "fsl_reply.ko",
            })
            self.assertEqual(len((session.state / "make-calls").read_text().splitlines()), 1)
            for name in names:
                self.assertIn(f"SIGNED {name} hash=sha512", result.stdout)
        finally:
            self.close(session)

        def drop_probe(work: Path) -> None:
            path = work / "research" / "fsl110" / "signing.sh"
            text = path.read_text(encoding="utf-8")
            path.write_text(text.replace("fsl_probe.ko ", ""), encoding="utf-8")

        session, result, _elapsed = self.run_script(
            "build-sign-reply.sh", scenario, {}, mutate=drop_probe, signing=True,
        )
        try:
            names = {Path(line).name for line in (session.state / "sign-calls").read_text().splitlines()}
            self.assertNotIn("fsl_probe.ko", names)
            self.assertNotEqual(names, {
                "fsl110.ko", "fsl_ramp.ko", "fsl_probe.ko", "fst_status.ko", "fsl_reply.ko",
            })
        finally:
            self.close(session)

    def test_median_and_plateau_and_retry_mutations_fail_the_oracle(self):
        def med5(work: Path) -> None:
            path = work / "research" / "fsl110" / "ramp-ec358.sh"
            path.write_text(path.read_text(encoding="utf-8").replace("sed -n '3p'", "sed -n '1p'", 1), encoding="utf-8")

        scenario = {"ec": "3.5.8", "baseline": (2700, 4050), "tach": "spread"}
        session, result, _elapsed = self.run_script(
            "ramp-ec358.sh", scenario, {"DGX_FSL_RAMP_EC358": "yes"}, mutate=med5,
        )
        try:
            self.assertNotEqual(median_problems(result.stdout, session.machine), [])
        finally:
            self.close(session)

        def invert(work: Path) -> None:
            path = work / "research" / "fsl110" / "ramp-ec358.sh"
            text = path.read_text(encoding="utf-8")
            old = '''  if [ "$up_a" = 0 ] && [ "$up_b" = 0 ]; then
    echo "STOP_PLATEAU both ${paths[0]}=$med_a prev=$pre_a ${paths[1]}=$med_b prev=$pre_b"
    exit 1
  fi
  if [ "$up_a" = 0 ] || [ "$up_b" = 0 ]; then
    echo "PLATEAU_RECORDED ${paths[0]}=$med_a prev=$pre_a ${paths[1]}=$med_b prev=$pre_b"
  fi
'''
            new = '''  if [ "$up_a" = 0 ] || [ "$up_b" = 0 ]; then
    if [ "$up_a" = 0 ] && [ "$up_b" = 0 ]; then
      echo "PLATEAU_RECORDED ${paths[0]}=$med_a prev=$pre_a ${paths[1]}=$med_b prev=$pre_b"
    else
      echo "STOP_PLATEAU both ${paths[0]}=$med_a prev=$pre_a ${paths[1]}=$med_b prev=$pre_b"
      exit 1
    fi
  fi
'''
            if old not in text:
                raise AssertionError("plateau block moved")
            path.write_text(text.replace(old, new, 1), encoding="utf-8")

        one = {"ec": "3.5.8", "baseline": (2700, 4050), "tach": "one_flat", "flat_at": 170}
        session, result, _elapsed = self.run_script(
            "ramp-ec358.sh", one, {"DGX_FSL_RAMP_EC358": "yes"}, mutate=invert,
        )
        try:
            self.assertIn("STOP_PLATEAU", result.stdout)
            self.assertNotEqual(len(completed(session.machine, "/proc/fsl_ramp")), 13)
        finally:
            self.close(session)

        def retry(work: Path) -> None:
            path = work / "research" / "fsl110" / "probe-ec3137.sh"
            text = path.read_text(encoding="utf-8")
            line = "printf '%s\\n' run > /proc/fsl_probe || { echo STOP_WRITE; exit 1; }\n"
            if line not in text:
                raise AssertionError("probe write line moved")
            path.write_text(text.replace(line, line + line, 1), encoding="utf-8")

        probe = {"ec": "3.13.7", "baseline": (2066, 2156), "tach": "hold"}
        session, result, _elapsed = self.run_script(
            "probe-ec3137.sh", probe, {"DGX_FSL_PROBE_EC3137": "yes"},
        )
        try:
            self.assertEqual(len(completed(session.machine, "/proc/fsl_probe")), 1, result.stdout[-800:])
            self.assertEqual(result.returncode, 0, result.stdout[-800:])
        finally:
            self.close(session)
        session, result, _elapsed = self.run_script(
            "probe-ec3137.sh", probe, {"DGX_FSL_PROBE_EC3137": "yes"}, mutate=retry,
        )
        try:
            detail = f"rc={result.returncode} writes={session.machine.completed_writes} attempts={session.machine.attempts} tail={result.stdout[-400:]!r}"
            self.assertGreater(len(session.machine.attempts), 1, detail)
            self.assertNotEqual(result.returncode, 0, detail)
        finally:
            self.close(session)


MODULES = ("fsl110.ko", "fsl_ramp.ko", "fsl_probe.ko", "fst_status.ko", "fsl_reply.ko")


class ControllerAndIsolationTests(unittest.TestCase):
    def test_controller_set_state_does_not_write_the_acpi_fan(self):
        scenario = {"ec": "3.5.8", "baseline": (2700, 4050), "tach": "hold"}
        session = Session(ROOT, TOOLS, scenario)
        try:
            result = session.run(
                ["python3", str(ROOT / "userspace" / "dgx_fan_control.py"), "set-state", "6"],
                extra_env={"DGX_TESTER_NO_CD": "1"},
            )
            self.assertEqual(result.returncode, 0, result.stdout[-1500:])
            floor = session.state / "sys/class/thermal/cooling_device0/cur_state"
            fan = session.state / "sys/class/thermal/cooling_device1/cur_state"
            self.assertEqual(floor.read_text(), "6\n")
            self.assertEqual(fan.read_text(), "30\n")
            session.machine.reported = [4500, 4590]
            session.machine.production_floor = 0
            session.machine.reboot_research()
            self.assertEqual(session.machine.reported, [2700, 4050])
            self.assertEqual(floor.read_text(), "6\n")
            self.assertEqual(session.machine.production_floor, 0)
            session.machine.reboot_production_model()
            self.assertEqual(session.machine.production_floor, 0)
            self.assertEqual(session.machine.reported, [2700, 4050])
        finally:
            session.close()

    def test_sandbox_cannot_see_host_sysfs_or_load_a_module(self):
        host_net = sorted(path.name for path in Path("/sys/class/net").iterdir())
        host_thermal = sorted(path.name for path in Path("/sys/class/thermal").iterdir())
        scenario = {"ec": "3.5.8", "baseline": (2700, 4050), "tach": "hold"}
        session = Session(ROOT, TOOLS, scenario)
        try:
            result = session.run(
                ["bash", "-c", "ls /sys/class/net >/tmp/net.out 2>&1; echo NET:$?; cat /proc/modules; echo ---; " + str(TOOLS / "finit")],
                extra_env={"DGX_TESTER_NO_CD": "1"},
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("NET:2", result.stdout.replace(" ", ""))
            self.assertIn("fixture_modules", result.stdout)
            self.assertIn("rc=-1 errno=1", result.stdout)
            self.assertNotIn("lo", result.stdout.split("fixture_modules")[0])
        finally:
            session.close()
        self.assertEqual(sorted(path.name for path in Path("/sys/class/net").iterdir()), host_net)
        self.assertEqual(sorted(path.name for path in Path("/sys/class/thermal").iterdir()), host_thermal)


if __name__ == "__main__":
    unittest.main()
