#!/bin/bash
# Kensaki only. Continuation from argument 160 through 237.
# 120-150 already ran; this binary does not issue them.
# Does not insmod until the baseline is clean. Does not wrap the ACPI write
# in a timeout.
# Killing the shell does not cancel an SMC already entered in the kernel.
# Cleanup only unloads a module this script loaded. It does not write a
# fan release. Reboot after any step.
set -u
cd "$(dirname "$0")"
exec > >(tee -a kensaki-ramp-hostlog.txt) 2>&1
echo "HOST_DATE $(date -Is)"
echo "host local clock, not labeled PST"
test "${DGX_FSL_RAMP_KENSAKI:-}" = yes || { echo STOP_NO_OPTIN; exit 1; }
test "$(id -u)" -eq 0 || { echo STOP_NOT_ROOT; exit 1; }
hn=$(hostname -s 2>/dev/null || hostname); hn=${hn%%.*}
test "$hn" = kensaki || { echo STOP_HOST; exit 1; }
stepped=0
loaded_by_us=0
steps=(160 170 180 190 200 210 220 225 230 232 234 236 237)
cleanup() {
  if [ "$loaded_by_us" = 1 ] && lsmod | grep -q '^fsl_ramp_kensaki '; then
    rmmod fsl_ramp_kensaki || true
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
for p in /proc/fsl110 /proc/fsl_ramp_kensaki /proc/fsl120_atarime; do
  test ! -e "$p" || { echo "STOP_ALREADY_LOADED $p"; exit 1; }
done
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
  local utils util
  if ! utils=$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null); then
    echo "STOP_GPU nvidia-smi_failed"
    return 1
  fi
  if [ -z "$utils" ]; then
    echo "STOP_GPU empty"
    return 1
  fi
  while IFS= read -r util; do
    util=${util// /}
    if [ "$util" != 0 ]; then
      echo "STOP_GPU util=$util raw=$utils"
      return 1
    fi
  done <<< "$utils"
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
    zones_ok || return 1
    gpu_idle || return 1
    if ! pair=$(read_pair); then
      return 1
    fi
    echo "$tag $i $pair"
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
    echo "STOP_NOT_BASELINE raw=$pair"
    exit 1
  }
  i=$((i + 1))
done
pre_a=$(med5 "${samples_a[@]:10:5}")
pre_b=$(med5 "${samples_b[@]:10:5}")
echo "baseline_median_last5 $pre_a $pre_b"
test -f ./fsl_ramp_kensaki.ko || { echo STOP_NO_MODULE; exit 1; }
insmod ./fsl_ramp_kensaki.ko || { echo STOP_INSMOD; exit 1; }
loaded_by_us=1
echo "loaded; unload will not restore fans"
for expect in "${steps[@]}"; do
  echo "writing step for $expect; the write blocks until the interpreter returns"
  stepped=1
  printf '%s\n' step > /proc/fsl_ramp_kensaki || { echo STOP_WRITE; exit 1; }
  status=$(cat /proc/fsl_ramp_kensaki || true)
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
zones_ok || exit 1
gpu_idle || exit 1
