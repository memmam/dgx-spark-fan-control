#!/bin/bash
# EC 3.5.8 continuation from argument 160 through 237.
# 120-150 already ran; this binary does not issue them.
# Does not insmod until the baseline is clean. Does not wrap the ACPI write
# in a timeout.
# Killing the shell does not cancel an SMC already entered in the kernel.
# test-log-flush.sh shows bash 5.2 echo through this tee is already in the
# file before a later sleep. If the proc write does not return, reboot.
# Do not kill the shell and do not load the module again on that boot.
# A completed dwell is followed by the next argument through 237.
# Cleanup only unloads a module this script loaded. It does not write a
# fan release. Reboot after any step.
set -u
# A test variable must not be able to reach insmod. Exit before cd, tee,
# /sys, or nvidia-smi.
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
exec > >(tee -a ramp-ec358-hostlog.txt) 2>&1
echo "HOST_DATE $(date -Is)"
echo "HOST_DATE is date -Is with this machine's offset. Do not relabel that offset as PST."
echo "zone types are logged as read; nvidia_gpu_temp is temperature.gpu, not an SoC reading"
test "${DGX_FSL_RAMP_EC358:-}" = yes || { echo STOP_NO_OPTIN; exit 1; }
test "$(id -u)" -eq 0 || { echo STOP_NOT_ROOT; exit 1; }
require_ec 3.5.8
steps=(160 170 180 190 200 210 220 225 230 232 234 236 237)
gpu_idle() {
  local utils
  if ! utils=$(nvidia-smi --query-gpu=utilization.gpu,temperature.gpu --format=csv,noheader,nounits 2>/dev/null); then
    echo "STOP_GPU nvidia-smi_failed"
    return 1
  fi
  gpu_parse "$utils"
}
stepped=0
loaded_by_us=0
cleanup() {
  if [ "$loaded_by_us" = 1 ] && lsmod | grep -q '^fsl_ramp '; then
    rmmod fsl_ramp || true
    echo "unloaded; this did not undo a hold"
  fi
  if [ "$stepped" = 1 ]; then
    echo "REBOOT_NOW. Do not write 0. Do not run this again."
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
read_pair() {
  local a b
  if ! a=$(cat "${paths[0]}" 2>/dev/null); then
    echo "STOP_READ unreadable ${paths[0]}" >&2
    return 1
  fi
  if ! b=$(cat "${paths[1]}" 2>/dev/null); then
    echo "STOP_READ unreadable ${paths[1]} other=${paths[0]}=$a" >&2
    return 1
  fi
  if ! [[ "$a" =~ ^[0-9]+$ && "$b" =~ ^[0-9]+$ ]]; then
    echo "STOP_READ nonnumeric ${paths[0]}=$a ${paths[1]}=$b" >&2
    return 1
  fi
  if [ "$a" -eq 0 ] || [ "$b" -eq 0 ]; then
    echo "STOP_READ zero ${paths[0]}=$a ${paths[1]}=$b" >&2
    return 1
  fi
  if [ "$a" -gt 9500 ] || [ "$b" -gt 9500 ]; then
    echo "STOP_READ above_9500 ${paths[0]}=$a ${paths[1]}=$b" >&2
    return 1
  fi
  printf '%s %s\n' "$a" "$b"
}
sample_ok() {
  local tag="$1" i pair
  samples_a=()
  samples_b=()
  i=1
  while [ "$i" -le 15 ]; do
    if ! zones_ok /sys/class/thermal; then
      echo "DWELL_INCOMPLETE tag=$tag kept=$((i - 1))"
      return 1
    fi
    if ! gpu_idle; then
      echo "DWELL_INCOMPLETE tag=$tag kept=$((i - 1))"
      return 1
    fi
    if ! pair=$(read_pair); then
      echo "DWELL_INCOMPLETE tag=$tag kept=$((i - 1))"
      return 1
    fi
    zone_snap[$i]=$(date -Is)$ZONE_LINE" hottest=$HOTTEST$GPU_LINE"
    echo "${zone_snap[$i]} $tag $i fans=$pair"
    samples_a+=("${pair%% *}")
    samples_b+=("${pair##* }")
    test "$i" = 15 || sleep 2
    i=$((i + 1))
  done
}
med5() {
  printf '%s\n' "$@" | sort -n | sed -n '3p'
}
echo "=== baseline 15 x 2s ==="
sample_ok base || exit 1
i=0
while [ "$i" -lt 15 ]; do
  pair="${samples_a[$i]} ${samples_b[$i]}"
  sorted=$(printf '%s\n' $pair | sort -n | tr '\n' ' ')
  test "$sorted" = "2700 4050 " || {
    echo "STOP_NOT_BASELINE raw=$pair ${zone_snap[$((i + 1))]:-no_zone_snapshot}"
    exit 1
  }
  i=$((i + 1))
done
pre_a=$(med5 "${samples_a[@]:10:5}")
pre_b=$(med5 "${samples_b[@]:10:5}")
echo "baseline_median_last5 $pre_a $pre_b"
require_built_module ./fsl_ramp.ko fsl_ramp.c
insmod ./fsl_ramp.ko || { echo STOP_INSMOD; exit 1; }
loaded_by_us=1
echo "loaded; unload will not restore fans"
for expect in "${steps[@]}"; do
  echo "writing step for $expect; the write blocks until the interpreter returns"
  stepped=1
  printf '%s\n' step > /proc/fsl_ramp || { echo STOP_WRITE; exit 1; }
  status=$(cat /proc/fsl_ramp || true)
  printf 'status %s\n' "$status"
  test "$status" = "ok $expect" || { echo "STOP_STATUS raw=$status"; exit 1; }
  echo "=== dwell after $expect ==="
  sample_ok "post_$expect" || exit 1
  med_a=$(med5 "${samples_a[@]:10:5}")
  med_b=$(med5 "${samples_b[@]:10:5}")
  echo "median_last5 ${paths[0]}=$med_a ${paths[1]}=$med_b previous $pre_a $pre_b"
  if [ "$med_a" -lt "$pre_a" ] || [ "$med_b" -lt "$pre_b" ]; then
    echo "STOP_DECREASE ${paths[0]}=$med_a prev=$pre_a ${paths[1]}=$med_b prev=$pre_b"
    exit 1
  fi
  up_a=0
  up_b=0
  test "$med_a" -gt "$pre_a" && up_a=1
  test "$med_b" -gt "$pre_b" && up_b=1
  if [ "$up_a" = 0 ] && [ "$up_b" = 0 ]; then
    echo "STOP_PLATEAU both ${paths[0]}=$med_a prev=$pre_a ${paths[1]}=$med_b prev=$pre_b"
    exit 1
  fi
  if [ "$up_a" = 0 ] || [ "$up_b" = 0 ]; then
    echo "PLATEAU_RECORDED ${paths[0]}=$med_a prev=$pre_a ${paths[1]}=$med_b prev=$pre_b"
  fi
  pre_a=$med_a
  pre_b=$med_b
done
echo "RAMP_STEPS_DONE"
echo "=== final zones ==="
zones_ok /sys/class/thermal || exit 1
gpu_idle || exit 1
echo "$(date -Is) final zones$ZONE_LINE hottest=$HOTTEST$GPU_LINE"
