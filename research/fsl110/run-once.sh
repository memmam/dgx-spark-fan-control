#!/bin/bash
# One insmod, one "run", one rmmod. Requires DGX_FSL110=yes.
# Build-sign.sh must already have printed BUILD_SIGNED_NOT_LOADED.
set -euo pipefail
cd "$(dirname "$0")"
test "${DGX_FSL110:-}" = yes || { echo STOP_NO_OPTIN; exit 1; }
test "$(id -u)" -eq 0 || { echo STOP_NOT_ROOT; exit 1; }
hn=$(hostname -s 2>/dev/null || hostname); hn=${hn%%.*}
test "$hn" = kensaki || { echo STOP_HOST; exit 1; }
if lsmod | grep -q '^dgx_ec_fan_control'; then echo STOP_CONTROLLER; exit 1; fi
if [ -e /usr/local/sbin/dgx-fan-control ]; then echo STOP_CONTROLLER; exit 1; fi
for u in dgx-fan-control dgx-fan-watchdog dgx-fan-manual dgx-fan-health; do
  test "$(systemctl is-active "$u.service" 2>/dev/null || true)" != active || { echo STOP_CONTROLLER; exit 1; }
done
for z in /sys/class/thermal/thermal_zone*; do
  t=$(cat "$z/temp") || { echo STOP_TEMP; exit 1; }
  test "$t" -ge 25000 && test "$t" -le 60000 || { echo "STOP_TEMP $z $t"; exit 1; }
done
vals=""
for h in /sys/class/hwmon/hwmon*; do
  test "$(cat "$h/name" 2>/dev/null || true)" = acpi_fan || continue
  for f in "$h"/fan*_input; do
    test -r "$f" || continue
    vals="$vals $(cat "$f")"
  done
done
sorted=$(printf '%s\n' $vals | sort -n | tr '\n' ' ')
echo "before$sorted"
test "$sorted" = "2700 4050 " || { echo STOP_NOT_STOCK; exit 1; }
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
rmmod fsl110 && echo "unloaded; this did not undo a hold"
echo "If the speeds are no longer 2700 and 4050, reboot. Do not write 0. Do not insmod again."
