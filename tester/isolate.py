#!/usr/bin/env python3
"""Run a launcher where host /sys, /proc/modules, and module loading are unreachable.

The process is root only inside a user namespace, so finit_module fails with
EPERM. enter.sh stays pid 1 and waits for the launcher and for the hostlog
tee. nssetup mounts the fixture and a private /tmp. When a kdir is planted,
/lib/modules is created on a tmpfs that covers that parent inside the
namespace only. LD_PRELOAD covers /proc/fsl* because procfs will not let us
create those names.
"""
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from tester.machine import Machine

SHIMS = (
    "cat", "sleep", "nvidia-smi", "insmod", "rmmod", "lsmod", "modinfo",
    "systemctl", "fwupdmgr", "dmesg", "mokutil", "make",
)
MODULES = ("fsl110.ko", "fsl_ramp.ko", "fsl_probe.ko", "fst_status.ko", "fsl_reply.ko")


def _plant_modules(fsl: Path, signed: bool) -> None:
    now = time.time()
    for src in list(fsl.glob("*.c")) + [fsl / "Makefile"]:
        if src.exists():
            os.utime(src, (now - 30, now - 30))
    release = os.uname().release
    for name in MODULES:
        ko = fsl / name
        ko.write_text("ko\n", encoding="ascii")
        os.utime(ko, (now, now))
        (fsl / (name + ".vermagic")).write_text(release + " SMP\n", encoding="ascii")
        if signed:
            (fsl / (name + ".signer")).write_text("fixture signer\n", encoding="ascii")


def _plant_kdir(state: Path) -> None:
    kdir = state / "kdir"
    (kdir / "scripts").mkdir(parents=True)
    (kdir / ".config").write_text('CONFIG_MODULE_SIG_HASH="sha512"\n', encoding="ascii")
    sign = kdir / "scripts" / "sign-file"
    sign.write_text(
        "#!/bin/bash\n"
        'printf "%s\\n" "$4" >> "$DGX_TESTER_STATE/sign-calls"\n'
        'printf "SIG %s\\n" "$1" >> "$4"\n',
        encoding="ascii",
    )
    sign.chmod(0o755)
    (state / "sign-calls").write_text("", encoding="ascii")


class Session:
    def __init__(self, repo: Path, tools: Path, scenario: dict, mutate=None,
                 signed=True, signing=False):
        self.repo = Path(repo)
        self.state = Path(tools) / "run"
        if self.state.exists():
            shutil.rmtree(self.state)
        self.state.mkdir(parents=True)
        work = self.state / "work"
        shutil.copytree(
            self.repo / "research" / "fsl110",
            work / "research" / "fsl110",
            ignore=shutil.ignore_patterns("*hostlog*", "__pycache__", "*.ko", "*.o"),
        )
        (work / "scripts").mkdir(parents=True)
        shutil.copy(self.repo / "scripts" / "ec_profile.py", work / "scripts" / "ec_profile.py")
        if mutate:
            mutate(work)
        _plant_modules(work / "research" / "fsl110", signed=signed)
        if signing:
            _plant_kdir(self.state)
        shutil.copy(tools / "preload.so", self.state / "preload.so")
        bindir = self.state / "bin"
        bindir.mkdir()
        binary = tools / "fastshim"
        for name in SHIMS:
            (bindir / name).symlink_to(binary)
        self.machine = Machine(self.state, scenario, str(tools / "reply_harness"))
        self.machine.start()
        key = self.state / "local.priv"
        cert = self.state / "local.cert"
        key.write_text("not-a-real-key\n", encoding="ascii")
        cert.write_text("not-a-real-cert\n", encoding="ascii")
        self.key = key
        self.cert = cert
        nssetup = str(tools / "nssetup")
        enter = self.state / "enter.sh"
        enter.write_text(
            "#!/bin/bash\n"
            "set -u\n"
            'STATE="$1"\n'
            "shift\n"
            f'"{nssetup}" "$STATE" || exit 125\n'
            'export LD_PRELOAD="$STATE/preload.so"\n'
            'export PATH="$STATE/bin:$PATH"\n'
            'export DGX_TESTER_STATE="$STATE"\n'
            'export DGX_TESTER_SOCK="$STATE/sock"\n'
            'if [ "${DGX_TESTER_NO_CD:-}" != 1 ]; then\n'
            '  cd "$STATE/work/research/fsl110"\n'
            "fi\n"
            '"$@" &\n'
            "child=$!\n"
            "forward() {\n"
            '  kill -s "$1" "$child" 2>/dev/null || true\n'
            "}\n"
            "trap 'forward TERM' TERM\n"
            "trap 'forward INT' INT\n"
            "trap 'forward HUP' HUP\n"
            "set +e\n"
            'wait "$child"\n'
            "status=$?\n"
            "trap - TERM INT HUP\n"
            "while true; do\n"
            "  wait -n\n"
            "  rc=$?\n"
            '  if [ "$rc" -eq 127 ]; then\n'
            "    break\n"
            "  fi\n"
            "done\n"
            'exit "$status"\n',
            encoding="ascii",
        )
        enter.chmod(0o755)
        self.enter = enter

    def popen(self, argv, extra_env=None):
        env = os.environ.copy()
        env.pop("LD_PRELOAD", None)
        env["DGX_TESTER_STATE"] = str(self.state)
        env["DGX_MOK_KEY"] = str(self.key)
        env["DGX_MOK_CERT"] = str(self.cert)
        if extra_env:
            env.update(extra_env)
        return subprocess.Popen(
            ["unshare", "--user", "--map-root-user", "--mount", "--propagation", "private",
             "--pid", "--fork", "--mount-proc", "bash", str(self.enter), str(self.state), *argv],
            cwd=self.repo,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def run(self, argv, extra_env=None, timeout=90):
        proc = self.popen(argv, extra_env)
        try:
            out, _err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                proc.kill()
            try:
                out, _err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                out = ""
            return subprocess.CompletedProcess(argv, 124, out or "", "")
        return subprocess.CompletedProcess(argv, proc.returncode, out, "")

    def close(self) -> None:
        self.machine.stop()
