#!/bin/bash
# EC 3.13.7 only. One argument, 120, after the reply observer is armed.
# The observer does not issue the write. interpreter_ok is not an EC accept
# or reject. A captured qword is not proof the fan loop stored the bytes.
# Cleanup only unloads modules this script loaded. It does not write a release.
set -u
if [ -n "${THERMAL_ROOT+x}" ] || [ -n "${NVIDIA_SMI_STUB+x}" ] || [ "${RAMP_SOURCE_ONLY:-}" = 1 ]; then
  echo STOP_TEST_ENV
  exit 1
fi
cd "$(dirname "$0")"
# shellcheck source=ec-profile.sh
source ./ec-profile.sh
exec > >(tee -a reply-ec3137-hostlog.txt) 2>&1
echo "HOST_DATE $(date -Is)"
echo "host local clock, not labeled PST"
test "${DGX_FSL_REPLY_EC3137:-}" = yes || { echo STOP_NO_OPTIN; exit 1; }
test "$(id -u)" -eq 0 || { echo STOP_NOT_ROOT; exit 1; }
require_ec 3.13.7
wrote=0
loaded_reply=0
loaded_probe=0
cleanup() {
  if [ "$loaded_probe" = 1 ] && lsmod | grep -q '^fsl_probe '; then
    rmmod fsl_probe || true
    echo "unloaded probe; this did not undo a hold"
  fi
  if [ "$loaded_reply" = 1 ] && lsmod | grep -q '^fsl_reply '; then
    rmmod fsl_reply || true
    echo "unloaded reply observer"
  fi
  if [ "$wrote" = 1 ]; then
    echo "REBOOT_NOW. Unchanged speeds are no observed effect, not a protocol rejection. Do not write 0. Do not run this again."
  fi
}
trap cleanup EXIT
lock=$(cat /sys/kernel/security/lockdown 2>/dev/null || echo missing)
echo "lockdown $lock"
case "$lock" in
  *'[confidentiality]'*) echo STOP_LOCKDOWN; exit 1 ;;
esac
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
  elif [ "$a" -gt 16000 ] || [ "$b" -gt 16000 ]; then
    echo "STOP_READ above_16000 $when ${paths[0]}=$a ${paths[1]}=$b" >&2
    return 1
  fi
  printf '%s %s\n' "$a" "$b"
}
echo "=== baseline 15 x 2s, before the observer ==="
i=1
while [ "$i" -le 15 ]; do
  zones_ok || exit 1
  gpu_idle || exit 1
  pair=$(check_pair base) || exit 1
  echo "base $i $pair"
  test "$i" = 15 || sleep 2
  i=$((i + 1))
done
test -f ./fsl_reply.ko || { echo STOP_NO_MODULE; exit 1; }
test -f ./fsl_probe.ko || { echo STOP_NO_MODULE; exit 1; }
insmod ./fsl_reply.ko || { echo STOP_INSMOD_REPLY; exit 1; }
loaded_reply=1
echo "reply observer loaded; no fan write yet"
snap=$(cat /proc/fsl_reply) || { echo STOP_REPLY_READ; exit 1; }
printf '%s\n' "$snap"
printf '%s\n' "$snap" | head -n 1 | grep -qx 'state armed' || {
  echo STOP_REPLY_NOT_ARMED
  exit 1
}
printf '%s\n' "$snap" | sed -n '2p' | grep -qx 'count 0' || {
  echo STOP_REPLY_NOT_EMPTY
  exit 1
}
insmod ./fsl_probe.ko || { echo STOP_INSMOD; exit 1; }
loaded_probe=1
echo "loaded probe; unload will not restore fans"
echo "writing run; the write blocks until the interpreter returns"
wrote=1
printf '%s\n' run > /proc/fsl_probe || { echo STOP_WRITE; exit 1; }
echo -n "status "
cat /proc/fsl_probe || true
echo
echo "=== reply buffer, before further fan reads ==="
cat /proc/fsl_reply || true
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
echo "REPLY_DONE"
echo "post_a0 is the first qword left in the handler buffer. The read path publishes a speed only when that qword is c400008e. The same value here is not proof the fan loop stored the bytes."
