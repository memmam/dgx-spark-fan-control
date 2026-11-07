#!/bin/bash
# Kensaki only. Up to three steps: arguments 120, 130, 140.
# Does not wrap the ACPI write in a timeout. Killing the shell does not
# cancel an SMC the kernel has already entered.
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
cleanup() {
  if lsmod | grep -q '^fsl_ramp_kensaki '; then
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
    t=$(cat "$z/temp") || return 1
    echo "$z type=$(cat "$z/type" 2>/dev/null || echo unreadable) temp=$t"
    test "$t" -ge 25000 && test "$t" -le 60000 || return 1
  done
}
gpu_idle() {
  local utils util rest
  utils=$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits) || return 1
  test -n "$utils" || return 1
  printf '%s\n' "$utils"
  while IFS= read -r util; do
    util=${util// /}
    test -n "$util" || return 1
    test "$util" = 0 || return 1
  done <<< "$utils"
}
echo "=== zones ==="
zones_ok || { echo STOP_TEMP; exit 1; }
gpu_idle || { echo STOP_GPU; exit 1; }
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
  a=$(cat "${paths[0]}") || return 1
  b=$(cat "${paths[1]}") || return 1
  [[ "$a" =~ ^[0-9]+$ && "$b" =~ ^[0-9]+$ ]] || return 1
  test "$a" -gt 0 && test "$b" -gt 0 || return 1
  test "$a" -le 7000 && test "$b" -le 7000 || return 1
  printf '%s %s\n' "$a" "$b"
}
echo "=== baseline 15 samples ==="
i=1
while [ "$i" -le 15 ]; do
  pair=$(read_pair) || { echo STOP_READ; exit 1; }
  echo "base $i $pair"
  sorted=$(printf '%s\n' $pair | sort -n | tr '\n' ' ')
  test "$sorted" = "2700 4050 " || { echo STOP_NOT_BASELINE; exit 1; }
  test "$i" = 15 || sleep 2
  i=$((i + 1))
done
pre_a=2700
pre_b=4050
test -f ./fsl_ramp_kensaki.ko || { echo STOP_NO_MODULE; exit 1; }
insmod ./fsl_ramp_kensaki.ko || { echo STOP_INSMOD; exit 1; }
echo "loaded; unload will not restore fans"
for expect in 120 130 140; do
  echo "=== before argument $expect ==="
  zones_ok || { echo STOP_TEMP; exit 1; }
  gpu_idle || { echo STOP_GPU; exit 1; }
  echo "writing step for $expect; the write blocks until the interpreter returns"
  stepped=1
  printf '%s\n' step > /proc/fsl_ramp_kensaki || { echo STOP_WRITE; exit 1; }
  status=$(cat /proc/fsl_ramp_kensaki || true)
  printf 'status %s\n' "$status"
  test "$status" = "ok $expect" || { echo STOP_STATUS; exit 1; }
  echo "=== after argument $expect, 15 samples ==="
  samples_a=()
  samples_b=()
  i=1
  while [ "$i" -le 15 ]; do
    pair=$(read_pair) || { echo STOP_READ; exit 1; }
    echo "post $expect $i $pair"
    samples_a+=("${pair%% *}")
    samples_b+=("${pair##* }")
    test "$i" = 15 || sleep 2
    i=$((i + 1))
  done
  med() {
    printf '%s\n' "$@" | sort -n | sed -n '3p'
  }
  med_a=$(med "${samples_a[@]:10:5}")
  med_b=$(med "${samples_b[@]:10:5}")
  echo "median_last5 $med_a $med_b previous $pre_a $pre_b"
  rise=0
  test $((med_a - pre_a)) -ge 200 && rise=1
  test $((med_b - pre_b)) -ge 200 && rise=1
  pre_a=$med_a
  pre_b=$med_b
  if [ "$rise" != 1 ] && [ "$expect" != 140 ]; then
    echo STOP_NO_RISE
    exit 1
  fi
  if [ "$rise" != 1 ]; then
    echo NO_RISE_AT_CAP
  fi
done
echo "RAMP_STEPS_DONE"
