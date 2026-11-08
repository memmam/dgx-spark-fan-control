#!/bin/bash
# EC 3.13.7 only. One argument, 120. No second write.
# interpreter_ok is not an EC accept or reject.
# Cleanup only unloads a module this script loaded. It does not write a release.
set -u
if [ -n "${THERMAL_ROOT+x}" ] || [ -n "${NVIDIA_SMI_STUB+x}" ] || [ "${RAMP_SOURCE_ONLY:-}" = 1 ]; then
  echo STOP_TEST_ENV
  exit 1
fi
cd "$(dirname "$0")"
# shellcheck source=ec-profile.sh
source ./ec-profile.sh
# shellcheck source=ramp-temps-lib.sh
source ./ramp-temps-lib.sh
# shellcheck source=module-fresh.sh
source ./module-fresh.sh
exec > >(tee -a probe-ec3137-hostlog.txt) 2>&1
echo "HOST_DATE $(date -Is)"
echo "HOST_DATE is date -Is with this machine's offset. Do not relabel that offset as PST."
test "${DGX_FSL_PROBE_EC3137:-}" = yes || { echo STOP_NO_OPTIN; exit 1; }
test "$(id -u)" -eq 0 || { echo STOP_NOT_ROOT; exit 1; }
require_ec 3.13.7
armed=0
loaded_by_us=0
cleanup() {
  if [ "$loaded_by_us" = 1 ] && lsmod | grep -q '^fsl_probe '; then
    rmmod fsl_probe || true
    echo "unloaded; this did not undo a hold"
  fi
  if [ "$armed" = 1 ]; then
    echo "REBOOT_NOW. Unchanged speeds are no observed effect, not a protocol rejection. Do not write 0. Do not run this again."
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
zones_ok() {
  local z t
  for z in /sys/class/thermal/thermal_zone*; do
    if ! t=$(cat "$z/temp" 2>/dev/null); then
      echo "STOP_TEMP unreadable $z"
      return 1
    fi
    if ! [[ "$t" =~ ^-?[0-9]+$ ]]; then
      echo "STOP_TEMP nonnumeric $z=$t"
      return 1
    fi
    if [ "$t" -lt 25000 ] || [ "$t" -gt 60000 ]; then
      echo "STOP_TEMP out_of_range $z=$t"
      return 1
    fi
  done
}
gpu_idle() {
  local utils
  if ! utils=$(nvidia-smi --query-gpu=utilization.gpu,temperature.gpu --format=csv,noheader,nounits 2>/dev/null); then
    echo "STOP_GPU nvidia-smi_failed"
    return 1
  fi
  gpu_parse "$utils" || return 1
  echo "gpu$GPU_LINE"
}
mapfile -t paths < <(
  for h in /sys/class/hwmon/hwmon*; do
    test "$(cat "$h/name" 2>/dev/null || true)" = acpi_fan || continue
    for f in "$h"/fan*_input; do
      test -r "$f" && printf '%s\n' "$f"
    done
  done | sort
)
test "${#paths[@]}" -eq 2 || { echo "STOP_FAN_COUNT ${#paths[@]}"; exit 1; }
echo "paths ${paths[0]} ${paths[1]}"
check_pair() {
  local when="$1" a b
  if ! a=$(cat "${paths[0]}" 2>/dev/null); then
    echo "STOP_READ unreadable $when ${paths[0]}" >&2
    return 1
  fi
  if ! b=$(cat "${paths[1]}" 2>/dev/null); then
    echo "STOP_READ unreadable $when ${paths[1]} other=${paths[0]}=$a" >&2
    return 1
  fi
  if ! [[ "$a" =~ ^[0-9]+$ && "$b" =~ ^[0-9]+$ ]]; then
    echo "STOP_READ nonnumeric $when ${paths[0]}=$a ${paths[1]}=$b" >&2
    return 1
  fi
  if [ "$a" -eq 0 ] || [ "$b" -eq 0 ]; then
    echo "STOP_READ zero $when ${paths[0]}=$a ${paths[1]}=$b" >&2
    return 1
  fi
  if [ "$when" = base ]; then
    if [ "$a" -gt 4000 ] || [ "$b" -gt 4000 ]; then
      echo "STOP_NOT_IDLE $when ${paths[0]}=$a ${paths[1]}=$b" >&2
      return 1
    fi
    if [ "$a" -gt 2500 ] || [ "$b" -gt 2500 ]; then
      echo "IDLE_ABOVE_USUAL $when ${paths[0]}=$a ${paths[1]}=$b" >&2
    fi
  elif [ "$a" -gt 16000 ] || [ "$b" -gt 16000 ]; then
    echo "STOP_READ above_16000 $when ${paths[0]}=$a ${paths[1]}=$b" >&2
    return 1
  fi
  printf '%s %s\n' "$a" "$b"
}
echo "=== baseline 15 x 2s ==="
i=1
while [ "$i" -le 15 ]; do
  zones_ok || exit 1
  gpu_idle || exit 1
  pair=$(check_pair base) || exit 1
  echo "base $i $pair"
  test "$i" = 15 || sleep 2
  i=$((i + 1))
done
require_built_module ./fsl_probe.ko fsl_probe.c
insmod ./fsl_probe.ko || { echo STOP_INSMOD; exit 1; }
loaded_by_us=1
echo "loaded; unload will not restore fans"
echo "writing run; the write blocks until the interpreter returns"
armed=1
printf '%s\n' run > /proc/fsl_probe || { echo STOP_WRITE; exit 1; }
echo -n "status "
cat /proc/fsl_probe || true
echo
echo "=== after 15 x 2s ==="
i=1
while [ "$i" -le 15 ]; do
  zones_ok || exit 1
  gpu_idle || exit 1
  pair=$(check_pair post) || exit 1
  echo "post $i $pair"
  test "$i" = 15 || sleep 2
  i=$((i + 1))
done
echo "=== final zones ==="
zones_ok || exit 1
gpu_idle || exit 1
echo "PROBE_DONE"
