#!/bin/bash
# Offline checks for the EC 3.13.7 reply observer. No hardware.
set -euo pipefail
cd "$(dirname "$0")"
src=$(cat fsl_reply.c)
case "$src" in
  *'acpi_ffh_address_space_arch_handler'*) ;;
  *) echo "FAIL missing handler symbol"; exit 1 ;;
esac
case "$src" in
  *'0xC400008DULL'*) ;;
  *) echo "FAIL missing SMC id"; exit 1 ;;
esac
case "$src" in
  *'acpi_evaluate_object'*|*'_FSL'*|*'_FST'*) echo "FAIL method call present"; exit 1 ;;
esac
case "$src" in
  *'FSL_REPLY_WRITES'*'8'*) ;;
  *) echo "FAIL write slot cap missing"; exit 1 ;;
esac
case "$src" in
  *'FSL_REPLY_READS'*'32'*) ;;
  *) echo "FAIL read slot cap missing"; exit 1 ;;
esac
case "$src" in
  *'nmissed'*) ;;
  *) echo "FAIL nmissed not reported"; exit 1 ;;
esac
case "$src" in
  *'register_kretprobe'*) ;;
  *) echo "FAIL probe is not a kretprobe"; exit 1 ;;
esac
bash -n reply-ec3137.sh
bash -n build-sign-reply.sh
sh=reply-ec3137.sh
line() { grep -n "$1" "$sh" | head -1 | cut -d: -f1; }
reject=$(line 'STOP_TEST_ENV')
ec=$(line 'require_ec 3.13.7')
lock=$(line 'STOP_LOCKDOWN')
reply=$(line 'insmod ./fsl_reply.ko')
armed=$(line 'STOP_REPLY_NOT_ARMED')
probe=$(line 'insmod ./fsl_probe.ko')
write=$(line "printf '%s\\\\n' run > /proc/fsl_probe")
test -n "$reject" && test -n "$ec" && test -n "$lock"
test -n "$reply" && test -n "$armed" && test -n "$probe" && test -n "$write"
test "$reject" -lt "$reply"
test "$ec" -lt "$reply"
test "$lock" -lt "$reply"
test "$reply" -lt "$armed"
test "$armed" -lt "$probe"
test "$probe" -lt "$write"
if grep -q 'fsl_ramp\|hostname' "$sh" fsl_reply.c; then
  echo "FAIL ramp or hostname in reply path"
  exit 1
fi
if awk -v n="$reject" 'NR>n && /THERMAL_ROOT|NVIDIA_SMI_STUB|RAMP_SOURCE_ONLY/' "$sh" | grep -q .; then
  echo "FAIL override name remains after the reject"
  exit 1
fi
for label in THERMAL_ROOT NVIDIA_SMI_STUB RAMP_SOURCE_ONLY; do
  if [ "$label" = THERMAL_ROOT ]; then
    out=$(env THERMAL_ROOT=/tmp ./reply-ec3137.sh 2>&1 || true)
  elif [ "$label" = NVIDIA_SMI_STUB ]; then
    out=$(env NVIDIA_SMI_STUB='0, 41' ./reply-ec3137.sh 2>&1 || true)
  else
    out=$(env RAMP_SOURCE_ONLY=1 ./reply-ec3137.sh 2>&1 || true)
  fi
  test "$out" = "STOP_TEST_ENV"
  echo "override_rejected_before_hardware $label"
done
echo "reply_offline_ok"
bash -n ec-profile-status.sh
if grep -q insmod ec-profile-status.sh; then
  echo "FAIL status script loads a module"
  exit 1
fi
echo "ec_status_offline_ok"
bash -n reply-ec3137-preflight.sh
out=$(env RAMP_SOURCE_ONLY=1 ./reply-ec3137-preflight.sh 2>&1 || true)
test "$out" = "STOP_TEST_ENV"
if grep -q insmod reply-ec3137-preflight.sh; then
  echo "FAIL reply preflight loads a module"
  exit 1
fi
if ! grep -q 'layout=unverified' reply-ec3137-preflight.sh; then
  echo "FAIL preflight claims the buffer layout"
  exit 1
fi
echo "reply_preflight_offline_ok"
python3 - << 'PY'
import pathlib, subprocess, sys
root = pathlib.Path("classify-reply.py")
def run(text):
    out = subprocess.check_output([sys.executable, str(root)], input=text.encode())
    return out.decode().strip()
good = """state armed
writes 4
reads 2
dropped_writes 0
dropped_reads 0
nmissed 0
W 0 pre_a0=c400008d pre_a4=9 post_a0=c400008e post_a4=9 handler=0 post_ok=1
W 1 pre_a0=c400008d pre_a4=109 post_a0=c400008e post_a4=109 handler=0 post_ok=1
W 2 pre_a0=c400008d pre_a4=209 post_a0=c400008e post_a4=209 handler=0 post_ok=1
W 3 pre_a0=c400008d pre_a4=309 post_a0=c400008e post_a4=309 handler=0 post_ok=1
truncated 0
"""
assert run(good) == "CAPTURE_COMPLETE", run(good)
lost = good.replace("dropped_writes 0", "dropped_writes 1")
assert run(lost).startswith("INCONCLUSIVE"), run(lost)
miss = good.replace("nmissed 0", "nmissed 2")
assert "nmissed=2" in run(miss)
short = good.replace("writes 4", "writes 1", 1)
# listed W lines still 4, so count_mismatch and writes=1
assert "INCONCLUSIVE" in run(short)
trunc = good.replace("truncated 0", "truncated 1")
assert "truncated" in run(trunc)
blank = "state armed\n"
assert run(blank).startswith("INCONCLUSIVE malformed")
failed_copy = good.replace("post_ok=1", "post_ok=0")
assert run(failed_copy).startswith("INCONCLUSIVE") and "post_ok" in run(failed_copy)
assert "CAPTURE_COMPLETE" not in run(failed_copy)
read_as_write = good.replace("pre_a4=9", "pre_a4=c").replace("pre_a4=109", "pre_a4=10c").replace("pre_a4=209", "pre_a4=20c").replace("pre_a4=309", "pre_a4=30c")
assert "opcode" in run(read_as_write)
broken = good.replace(
    "W 0 pre_a0=c400008d pre_a4=9 post_a0=c400008e post_a4=9 handler=0 post_ok=1",
    "W 0 not-a-record",
)
assert "record_format" in run(broken)
print("classify_reply_ok")
PY
python3 - << 'PY'
import pathlib, re
src = pathlib.Path("fsl_reply.c").read_text()
text = int(re.search(r"#define FSL_REPLY_TEXT\s+(\d+)", src).group(1))
writes = int(re.search(r"#define FSL_REPLY_WRITES\s+(\d+)", src).group(1))
reads = int(re.search(r"#define FSL_REPLY_READS\s+(\d+)", src).group(1))
# One full line is under 220 bytes. The header is under 200.
need = 200 + (writes + reads) * 220
if text <= need:
    raise SystemExit(f"FAIL reply text {text} <= worst case {need}")
print("reply_text_fits", text, need)
PY
