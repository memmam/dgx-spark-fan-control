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
  *'FSL_REPLY_SLOTS		8'*) ;;
  *) echo "FAIL slot cap missing"; exit 1 ;;
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
