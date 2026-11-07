#!/bin/bash
# Atarime only. One argument, 120. No second write.
# interpreter_ok is not an EC accept or reject.
set -u
cd "$(dirname "$0")"
exec > >(tee -a atarime-probe120-hostlog.txt) 2>&1
echo "HOST_DATE $(date -Is)"
echo "host local clock, not labeled PST"
test "${DGX_FSL_PROBE_ATARIME:-}" = yes || { echo STOP_NO_OPTIN; exit 1; }
test "$(id -u)" -eq 0 || { echo STOP_NOT_ROOT; exit 1; }
hn=$(hostname -s 2>/dev/null || hostname); hn=${hn%%.*}
test "$hn" = atarime || { echo STOP_HOST; exit 1; }
armed=0
cleanup() {
  if lsmod | grep -q '^fsl_probe_atarime '; then
    rmmod fsl_probe_atarime || true
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
for p in /proc/fsl110 /proc/fsl_ramp_kensaki /proc/fsl120_atarime; do
  test ! -e "$p" || { echo "STOP_ALREADY_LOADED $p"; exit 1; }
done
echo "=== zones ==="
for z in /sys/class/thermal/thermal_zone*; do
  t=$(cat "$z/temp") || { echo STOP_TEMP; exit 1; }
  echo "$z type=$(cat "$z/type" 2>/dev/null || echo unreadable) temp=$t"
  test "$t" -ge 25000 && test "$t" -le 60000 || { echo "STOP_TEMP $z $t"; exit 1; }
done
utils=$(nvidia-smi --query-gpu=utilization.gpu,temperature.gpu --format=csv,noheader,nounits) || {
  echo STOP_GPU; exit 1
}
printf '%s\n' "$utils"
while IFS=',' read -r util gpu_temp; do
  util=${util// /}
  test "$util" = 0 || { echo "STOP_GPU util=$util"; exit 1; }
done <<< "$utils"
mapfile -t paths < <(
  for h in /sys/class/hwmon/hwmon*; do
    test "$(cat "$h/name" 2>/dev/null || true)" = acpi_fan || continue
    for f in "$h"/fan*_input; do
      test -r "$f" && printf '%s\n' "$f"
    done
  done | sort
)
test "${#paths[@]}" -eq 2 || { echo "STOP_FAN_COUNT ${#paths[@]}"; exit 1; }
echo "=== baseline 15 samples ==="
i=1
while [ "$i" -le 15 ]; do
  a=$(cat "${paths[0]}") || { echo STOP_READ; exit 1; }
  b=$(cat "${paths[1]}") || { echo STOP_READ; exit 1; }
  echo "base $i $a $b"
  [[ "$a" =~ ^[0-9]+$ && "$b" =~ ^[0-9]+$ ]] || { echo STOP_READ; exit 1; }
  test "$a" -gt 0 && test "$b" -gt 0 || { echo STOP_ZERO; exit 1; }
  test "$a" -le 4000 && test "$b" -le 4000 || { echo STOP_NOT_IDLE; exit 1; }
  test "$i" = 15 || sleep 2
  i=$((i + 1))
done
test -f ./fsl_probe_atarime.ko || { echo STOP_NO_MODULE; exit 1; }
insmod ./fsl_probe_atarime.ko || { echo STOP_INSMOD; exit 1; }
echo "loaded; unload will not restore fans"
echo "writing run; the write blocks until the interpreter returns"
armed=1
printf '%s\n' run > /proc/fsl120_atarime || { echo STOP_WRITE; exit 1; }
echo -n "status "
cat /proc/fsl120_atarime || true
echo
echo "=== after 15 samples ==="
i=1
while [ "$i" -le 15 ]; do
  a=$(cat "${paths[0]}") || { echo STOP_READ; exit 1; }
  b=$(cat "${paths[1]}") || { echo STOP_READ; exit 1; }
  echo "post $i $a $b"
  [[ "$a" =~ ^[0-9]+$ && "$b" =~ ^[0-9]+$ ]] || { echo STOP_READ; exit 1; }
  test "$i" = 15 || sleep 2
  i=$((i + 1))
done
echo "=== zones after ==="
for z in /sys/class/thermal/thermal_zone*; do
  echo "$z type=$(cat "$z/type" 2>/dev/null || echo unreadable) temp=$(cat "$z/temp" 2>/dev/null || echo unreadable)"
done
echo "PROBE_DONE"
