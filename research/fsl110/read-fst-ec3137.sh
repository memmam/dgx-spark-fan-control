#!/bin/bash
# EC 3.13.7 only. One read of the existing _FST methods. No _FSL write.
# Test environment variables stop the script before any hardware read.
set -u
if [ -n "${THERMAL_ROOT+x}" ] || [ -n "${NVIDIA_SMI_STUB+x}" ] || [ "${RAMP_SOURCE_ONLY:-}" = 1 ]; then
  echo STOP_TEST_ENV
  exit 1
fi
cd "$(dirname "$0")"
# shellcheck source=ramp-temps-lib.sh
source ./ramp-temps-lib.sh
# shellcheck source=ec-profile.sh
source ./ec-profile.sh
# shellcheck source=module-fresh.sh
source ./module-fresh.sh
exec > >(tee -a fst-ec3137-hostlog.txt) 2>&1
echo "HOST_DATE $(date -Is)"
echo "HOST_DATE is date -Is with this machine's offset. Do not relabel that offset as PST."
echo "nvidia_gpu_temp is temperature.gpu, not an SoC reading"
test "${DGX_FST_READ_EC3137:-}" = yes || { echo STOP_NO_OPTIN; exit 1; }
test "$(id -u)" -eq 0 || { echo STOP_NOT_ROOT; exit 1; }
require_ec 3.13.7
loaded_by_us=0
cleanup() {
  if [ "$loaded_by_us" = 1 ] && lsmod | grep -q '^fst_status '; then
    rmmod fst_status || true
    echo "unloaded; _FST was only read"
  fi
}
trap cleanup EXIT
if lsmod | grep -q '^dgx_ec_fan_control'; then echo STOP_CONTROLLER; exit 1; fi
if [ -e /usr/local/sbin/dgx-fan-control ]; then echo STOP_CONTROLLER; exit 1; fi
for u in dgx-fan-control dgx-fan-watchdog dgx-fan-manual dgx-fan-health; do
  test "$(systemctl is-active "$u.service" 2>/dev/null || true)" != active || {
    echo STOP_CONTROLLER; exit 1
  }
done
refuse_loaded_research
zones_ok /sys/class/thermal || exit 1
echo "$(date -Is) zones$ZONE_LINE hottest=$HOTTEST"
utils=$(nvidia-smi --query-gpu=utilization.gpu,temperature.gpu --format=csv,noheader,nounits 2>/dev/null) || {
  echo STOP_GPU nvidia-smi_failed; exit 1
}
gpu_parse "$utils" || exit 1
echo "$(date -Is) $GPU_LINE"
mapfile -t paths < <(
  for h in /sys/class/hwmon/hwmon*; do
    test "$(cat "$h/name" 2>/dev/null || true)" = acpi_fan || continue
    for f in "$h"/fan*_input; do
      test -r "$f" && printf '%s\n' "$f"
    done
  done | sort
)
test "${#paths[@]}" -eq 2 || { echo "STOP_FAN_COUNT ${#paths[@]}"; exit 1; }
a=$(cat "${paths[0]}") || { echo "STOP_READ unreadable ${paths[0]}"; exit 1; }
b=$(cat "${paths[1]}") || { echo "STOP_READ unreadable ${paths[1]}"; exit 1; }
echo "fans ${paths[0]}=$a ${paths[1]}=$b"
[[ "$a" =~ ^[0-9]+$ && "$b" =~ ^[0-9]+$ ]] || { echo "STOP_READ nonnumeric $a $b"; exit 1; }
test "$a" -gt 0 && test "$b" -gt 0 || { echo "STOP_READ zero $a $b"; exit 1; }
require_built_module ./fst_status.ko fst_status.c
insmod ./fst_status.ko || { echo STOP_INSMOD; exit 1; }
loaded_by_us=1
printf '%s\n' read > /proc/fst_status || { echo STOP_WRITE; exit 1; }
echo -n "fst "
cat /proc/fst_status || true
echo
echo "FST_READ_DONE"
echo "This did not send a fan setpoint. The three numbers are revision, cached control, and speed."
