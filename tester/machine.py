#!/usr/bin/env python3
"""Stateful machine the launchers talk to.

Published tach, the AML name FLVL, and a production floor are separate.
Evidence labels say which of those come from a capture and which are a
model used so the launchers have something to run against.
"""
import os
import socket
import subprocess
import threading
from pathlib import Path

EC_GUID = "3d13c989-e6a8-4ead-95ee-921f09868f65"
EC_VERSION = {
    "3.5.8": ("0x03000508", "50332936"),
    "3.13.7": ("0x03000d07", "50334983"),
}
RAMP_LEVELS = (160, 170, 180, 190, 200, 210, 220, 225, 230, 232, 234, 236, 237)

# Kept on the state directory so a reviewer can see them next to a run.
EVIDENCE = {
    "reported_idle": (
        "observed: after a research hold, reboot returned the published "
        "pair 2700/4050. This is not a shaft-speed measurement."
    ),
    "unload": (
        "observed: unloading the research module left the published pair "
        "where the write had put it."
    ),
    "lag": (
        "observed: the first sample of a later EC 3.5.8 dwell was still "
        "the previous pair."
    ),
    "aml_flvl": (
        "described from the captured DSDT: WFCF stores FLVL before WRB. "
        "The DSDT bytes are not in this tree. A reboot clears FLVL because "
        "the namespace is rebuilt."
    ),
    "accept_358": (
        "assumption for arguments 160 through 237: interpreter success "
        "publishes a higher pair on the next settled samples. Arguments "
        "120 through 150 did that once. 160 through 237 were not a finished "
        "dwell. Not a shaft speed and not a production EC clamp."
    ),
    "accept_3137": (
        "assumption: interpreter_ok does not change the published pair. "
        "The argument 110 and 120 captures showed no sustained change. "
        "That is not a proven protocol rejection."
    ),
    "production_floor": (
        "separate path. protocol.md says a 0 clamp is retained across a "
        "warm reboot and 0xffff means unset. reboot_research does not apply "
        "that rule to the published tach."
    ),
    "spread": (
        "the last five samples of one dwell are spread on purpose so a "
        "minimum is not the median. The captured dwells were flat; a flat "
        "dwell cannot tell those apart."
    ),
}


class Machine:
    def __init__(self, root: Path, scenario: dict, harness: str):
        self.root = Path(root)
        self.scenario = scenario
        self.harness = harness
        self.lock = threading.Lock()
        self.virtual_seconds = 0
        self.boot_id = 1
        self.pair_index = 0
        self.open_pair = None
        self.samples = []
        self.attempts = []
        self.completed_writes = []
        self.modules = set()
        self.aml_requested = None
        self.firmware_accepted = None
        self.accept_log = []
        self.production_floor = scenario.get("production_floor", "unset")
        self.reported = list(scenario.get("baseline", (2700, 4050)))
        self.staged = None
        self.lag_remaining = 0
        self.spread_active = False
        self.ramp_next = 0
        self.ramp_status = "idle\n"
        self.ramp_latched = False
        self.probe_used = False
        self.probe_status = "idle\n"
        self.reply_text = ""
        self.gpu_index = 0
        self.blocked = threading.Event()
        if scenario.get("block_proc"):
            self.blocked.set()
        self._stop = threading.Event()
        self._thread = None
        self._sock = None

    def prepare(self) -> None:
        sysroot = self.root / "sys"
        if sysroot.exists():
            for child in sorted(sysroot.rglob("*"), reverse=True):
                if child.is_file() or child.is_symlink():
                    child.unlink()
                elif child.is_dir():
                    child.rmdir()
        temp = "70000\n" if self.scenario.get("bad_temp") else "42000\n"
        self._write("class/thermal/thermal_zone0/type", "acpitz\n")
        self._write("class/thermal/thermal_zone0/temp", temp)
        self._write("class/hwmon/hwmon0/name", "acpi_fan\n")
        self._write_fans()
        self._write("kernel/security/lockdown", "none [none]\n")
        ec = self.scenario.get("ec", "3.5.8")
        _hex, decimal = EC_VERSION[ec]
        self._write("firmware/efi/esrt/entries/entry0/fw_class", EC_GUID + "\n")
        self._write("firmware/efi/esrt/entries/entry0/fw_version", decimal + "\n")
        self._write("class/thermal/cooling_device0/type", "dgx_ec_fan_floor\n")
        self._write("class/thermal/cooling_device0/cur_state", "0\n")
        self._write("class/thermal/cooling_device0/max_state", "12\n")
        self._write("class/thermal/cooling_device1/type", "Fan\n")
        self._write("class/thermal/cooling_device1/cur_state", "30\n")
        self._write("class/thermal/cooling_device1/max_state", "100\n")
        version = EC_VERSION[ec][0]
        payload = (
            '{"Devices":[{"Name":"Embedded Controller","Guid":["%s"],'
            '"Version":"%s"}]}\n' % (EC_GUID, version)
        )
        (self.root / "fwupd.json").write_text(payload, encoding="ascii")
        (self.root / "dmesg.txt").write_text("", encoding="ascii")
        (self.root / "proc_modules").write_text("fixture_modules 0 0 -\n", encoding="ascii")
        (self.root / "evidence.txt").write_text(
            "\n".join(f"{key}: {value}" for key, value in EVIDENCE.items()) + "\n",
            encoding="ascii",
        )
        (self.root / "make-calls").write_text("", encoding="ascii")

    def _write(self, rel: str, text: str) -> None:
        path = self.root / "sys" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="ascii")

    def _write_fans(self) -> None:
        self._write("class/hwmon/hwmon0/fan1_input", f"{self.reported[0]}\n")
        self._write("class/hwmon/hwmon0/fan2_input", f"{self.reported[1]}\n")

    def start(self) -> None:
        self.prepare()
        sock_path = self.root / "sock"
        if sock_path.exists():
            sock_path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        server.listen(16)
        server.settimeout(0.2)
        self._sock = server
        self._thread = threading.Thread(target=self._serve, name="dgx-tester", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.blocked.clear()
        if self._sock is not None:
            self._sock.close()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn: socket.socket) -> None:
        try:
            line = self._line(conn)
            if line is None:
                return
            if line.startswith("CMD "):
                payload = self.command(line[4:].strip())
                conn.sendall(payload.encode())
            elif line.startswith("R "):
                path = line[2:].strip()
                conn.sendall(self.proc_read(path).encode())
            elif line.startswith("W "):
                path = line[2:].strip()
                with self.lock:
                    self.attempts.append(path)
                while self.blocked.is_set() and not self._stop.is_set():
                    self.blocked.wait(0.05)
                while not self._stop.is_set():
                    header = self._line(conn)
                    if header is None or not header.startswith("B "):
                        break
                    length = int(header.split()[1])
                    body = self._exact(conn, length)
                    text = body.decode("ascii", "replace").strip("\n")
                    if text == "":
                        conn.sendall(b"0")
                        continue
                    ack = self.proc_write(path, body)
                    conn.sendall(ack.encode())
                    if ack != "0":
                        break
        except (OSError, ValueError):
            return
        finally:
            conn.close()

    @staticmethod
    def _line(conn: socket.socket):
        data = b""
        while b"\n" not in data:
            piece = conn.recv(1)
            if not piece:
                return None
            data += piece
            if len(data) > 1024:
                return None
        return data.decode()

    @staticmethod
    def _exact(conn: socket.socket, length: int) -> bytes:
        data = b""
        while len(data) < length:
            piece = conn.recv(length - len(data))
            if not piece:
                break
            data += piece
        return data

    def command(self, text: str) -> str:
        parts = text.split()
        op = parts[0]
        with self.lock:
            if op == "sleep":
                self.virtual_seconds += int(parts[1])
                return "OK\n"
            if op == "fan":
                path = parts[1]
                which = 0 if "fan1_input" in path else 1
                return self._fan_locked(which) + "\n"
            if op == "nvidia":
                return self._nvidia_locked()
            if op == "insmod":
                return self._insmod_locked(parts[1])
            if op == "rmmod":
                name = parts[1]
                self.modules.discard(name)
                return "OK\n"
            if op == "lsmod":
                if not self.modules:
                    return ""
                return "".join(f"{name} 0 0\n" for name in sorted(self.modules))
        return "ERR unknown\n"

    def _nvidia_locked(self) -> str:
        index = self.gpu_index
        self.gpu_index += 1
        if index == self.scenario.get("gpu_fail_at"):
            return "FAIL\n"
        override = self.scenario.get("gpu_override", {})
        return override.get(index, "0, 41") + "\n"

    def _begin_pair(self):
        index = self.pair_index
        if index in set(self.scenario.get("fault_pairs", ())):
            return None
        if self.spread_active and 25 <= index <= 29:
            slot = index - 25
            return (5000 + slot * 100, 6000 + slot * 100)
        return (self.reported[0], self.reported[1])

    def _finish_pair(self, pair) -> None:
        self.samples.append(pair if pair is not None else ("bad", "bad"))
        self.pair_index += 1
        if pair is not None and self.lag_remaining:
            self.lag_remaining -= 1
            if self.lag_remaining == 0 and self.staged is not None:
                self.reported = list(self.staged)
                self.staged = None
                self._write_fans()
        if self.spread_active and self.pair_index == 30:
            self.reported = [5400, 6400]
            self.staged = None
            self.lag_remaining = 0
            self.spread_active = False
            self._write_fans()

    def _fan_locked(self, which: int) -> str:
        if which == 0:
            self.open_pair = self._begin_pair()
            if self.open_pair is None:
                return "xx"
            return str(self.open_pair[0])
        pair = self.open_pair
        self._finish_pair(pair)
        self.open_pair = None
        if pair is None:
            return "xx"
        return str(pair[1])

    def _insmod_locked(self, path: str) -> str:
        ko = Path(path)
        if not ko.is_file():
            ko = Path(os.getcwd()) / path
        if not ko.is_file():
            return "ERR missing\n"
        signer = Path(str(ko) + ".signer")
        try:
            signed = signer.read_text(encoding="ascii").strip()
        except OSError:
            signed = ""
        if not signed:
            return "ERR unsigned\n"
        name = ko.name[:-3] if ko.name.endswith(".ko") else ko.name
        self.modules.add(name)
        if name == "fsl_reply":
            self.reply_text = self._harness("dump\n")
        if name == "fsl_probe":
            self.probe_used = False
            self.probe_status = "idle\n"
        if name == "fsl_ramp":
            self.ramp_next = 0
            self.ramp_status = "idle\n"
            self.ramp_latched = False
        return "OK\n"

    def _harness(self, script: str) -> str:
        proc = subprocess.run(
            [self.harness], input=script, text=True, capture_output=True, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr)
        return proc.stdout

    def proc_read(self, path: str) -> str:
        with self.lock:
            if path == "/proc/fsl_ramp":
                return self.ramp_status
            if path == "/proc/fsl_probe":
                return self.probe_status
            if path == "/proc/fsl_reply":
                return self.reply_text
        return ""

    def proc_write(self, path: str, body: bytes) -> str:
        text = body.decode("ascii", "replace")
        if text.endswith("\n"):
            text = text[:-1]
        # bash printf emits the newline in a separate fputc. It is not a command.
        if text == "":
            return "0"
        with self.lock:
            if path == "/proc/fsl_ramp":
                return self._ramp_write(text)
            if path == "/proc/fsl_probe":
                return self._probe_write(text)
            if path == "/proc/fsl_reply":
                self.completed_writes.append({"path": path, "text": text, "rejected": True})
                return "E"
        return "E"

    def _record(self, path: str, text: str, rejected: bool) -> None:
        self.completed_writes.append({"path": path, "text": text, "rejected": rejected})

    def _ramp_write(self, text: str) -> str:
        if text != "step":
            self._record("/proc/fsl_ramp", text, True)
            return "E"
        if self.ramp_latched or self.ramp_next >= len(RAMP_LEVELS):
            self._record("/proc/fsl_ramp", text, True)
            return "E"
        level = RAMP_LEVELS[self.ramp_next]
        self.ramp_next += 1
        self.ramp_status = f"ok {level}\n"
        self._record("/proc/fsl_ramp", text, False)
        self._accept(level, hold=self.scenario.get("tach") == "hold")
        return "0"

    def _probe_write(self, text: str) -> str:
        if text != "run":
            self._record("/proc/fsl_probe", text, True)
            return "E"
        if self.probe_used:
            self._record("/proc/fsl_probe", text, True)
            return "E"
        self.probe_used = True
        self.probe_status = "interpreter_ok\n"
        self._record("/proc/fsl_probe", text, False)
        self._accept(120, hold=True)
        if "fsl_reply" in self.modules:
            self.reply_text = self._harness(self._reply_program())
        return "0"

    def _accept(self, level: int, hold: bool) -> None:
        """Record the AML store. Tach movement is a separate, labeled choice."""
        self.aml_requested = level
        if hold or self.scenario.get("tach") == "hold":
            self.firmware_accepted = None
            self.accept_log.append({"level": level, "effect": "tach-unchanged", "evidence": "accept_3137"})
            return
        program = self.scenario.get("tach", "rise")
        base = list(self.staged if self.staged is not None else self.reported)
        if program == "one_flat" and level == self.scenario.get("flat_at", 170):
            nxt = [base[0] + 30, base[1]]
        elif program == "both_flat" and level == self.scenario.get("flat_at", 180):
            nxt = list(base)
        elif program == "fall" and level == self.scenario.get("fall_at", 170):
            nxt = [base[0] - 10, base[1]]
        elif program == "spread" and level == 160:
            self.spread_active = True
            nxt = [5400, 6400]
        else:
            nxt = [base[0] + 30, base[1] + 30]
        self.staged = nxt
        self.lag_remaining = 1
        self.firmware_accepted = level
        self.accept_log.append({"level": level, "effect": "staged", "evidence": "accept_358"})

    def _reply_program(self) -> str:
        if self.scenario.get("reply_program"):
            return self.scenario["reply_program"]
        fail = 1 if self.scenario.get("reply_fail_copy") else 0
        lines = []
        for _ in range(4):
            lines.append(f"event 9 c400008e 0 {fail} 0")
        lines.append("event c 11 0 0 0")
        lines.append("event c 11 0 0 0")
        lines.append("dump")
        return "\n".join(lines) + "\n"

    def reboot_research(self) -> None:
        """Observed research-path reboot. Does not implement a surviving clamp."""
        with self.lock:
            self.boot_id += 1
            self.reported = [2700, 4050]
            self.staged = None
            self.lag_remaining = 0
            self.spread_active = False
            self.aml_requested = 0
            self.firmware_accepted = None
            self.modules.clear()
            self.ramp_next = 0
            self.ramp_status = "idle\n"
            self.ramp_latched = False
            self.probe_used = False
            self.probe_status = "idle\n"
            self._write_fans()

    def reboot_production_model(self) -> None:
        """protocol.md only. A 0 clamp stays 0. unset stays unset.
        The published tach is not this value.
        """
        with self.lock:
            if self.production_floor == 0:
                self.production_floor = 0
