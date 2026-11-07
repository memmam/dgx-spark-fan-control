#!/bin/bash
# Atarime only. One insmod, one "run", one rmmod.
# Requires DGX_FSL110_ATARIME=yes and a module already signed on this host.
# interpreter_ok is not an EC accept or reject. Unload does not release a hold.
set -euo pipefail
cd "$(dirname "$0")"
test "${DGX_FSL110_ATARIME:-}" = yes || { echo STOP_NO_OPTIN; exit 1; }
test "$(id -u)" -eq 0 || { echo STOP_NOT_ROOT; exit 1; }
hn=$(hostname -s 2>/dev/null || hostname); hn=${hn%%.*}
test "$hn" = atarime || { echo STOP_HOST; exit 1; }
if lsmod | grep -q '^dgx_ec_fan_control'; then echo STOP_CONTROLLER; exit 1; fi
if [ -e /usr/local/sbin/dgx-fan-control ]; then echo STOP_CONTROLLER; exit 1; fi
for u in dgx-fan-control dgx-fan-watchdog dgx-fan-manual dgx-fan-health; do
  test "$(systemctl is-active "$u.service" 2>/dev/null || true)" != active || {
    echo STOP_CONTROLLER; exit 1
  }
done
echo "=== zones before ==="
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
  util=${util// /}; gpu_temp=${gpu_temp// /}
  test "$util" = 0 || { echo "STOP_GPU util=$util"; exit 1; }
done <<< "$utils"
fans=()
echo "=== fans before ==="
for h in /sys/class/hwmon/hwmon*; do
  test "$(cat "$h/name" 2>/dev/null || true)" = acpi_fan || continue
  for f in "$h"/fan*_input; do
    test -r "$f" || continue
    v=$(cat "$f")
    echo "$f $v"
    [[ "$v" =~ ^[0-9]+$ ]] || { echo STOP_FAN; exit 1; }
    test "$v" -gt 0 || { echo STOP_FAN_ZERO; exit 1; }
    fans+=("$v")
  done
done
test "${#fans[@]}" -eq 2 || { echo "STOP_FAN_COUNT ${#fans[@]}"; exit 1; }
test -f ./fsl110.ko || { echo STOP_NO_MODULE; exit 1; }
test ! -e /proc/fsl110 || { echo STOP_ALREADY_LOADED; exit 1; }
insmod ./fsl110.ko
echo "loaded; unload will not restore fans"
set +e
printf '%s\n' run > /proc/fsl110
write_rc=$?
echo -n "status "
cat /proc/fsl110
set -e
if [ "$write_rc" -ne 0 ]; then
  echo "WRITE_FAILED $write_rc"
fi
for n in 1 2 3; do
  printf 'sample %s' "$n"
  for h in /sys/class/hwmon/hwmon*; do
    test "$(cat "$h/name" 2>/dev/null || true)" = acpi_fan || continue
    for f in "$h"/fan*_input; do
      test -r "$f" && printf ' %s=%s' "${h#/sys/class/hwmon/}" "$(cat "$f")"
    done
  done
  printf '\n'
  test "$n" = 3 || sleep 2
done
echo "=== zones after ==="
for z in /sys/class/thermal/thermal_zone*; do
  echo "$z type=$(cat "$z/type" 2>/dev/null || echo unreadable) temp=$(cat "$z/temp")"
done
rmmod fsl110
echo "unloaded; this did not undo a hold"
echo "REBOOT_NOW. Unchanged speeds are no observed effect, not a protocol rejection. Do not call again."
