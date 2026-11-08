#!/bin/bash
# Read-only readiness for the EC 3.5.8 ramp. Does not load a module and
# does not write a fan level. A passing printout is not a baseline and
# does not authorize ramp-ec358.sh.
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
echo "HOST_DATE $(date -Is)"
echo "HOST_DATE is date -Is with this machine's offset. Do not relabel that offset as PST."
test "$(id -u)" -eq 0 || { echo STOP_NOT_ROOT; exit 1; }
require_ec 3.5.8
lock=$(cat /sys/kernel/security/lockdown 2>/dev/null || echo missing)
echo "lockdown $lock"
if lsmod | grep -q '^dgx_ec_fan_control'; then echo STOP_CONTROLLER; exit 1; fi
if [ -e /usr/local/sbin/dgx-fan-control ]; then echo STOP_CONTROLLER; exit 1; fi
for u in dgx-fan-control dgx-fan-watchdog dgx-fan-manual dgx-fan-health; do
  test "$(systemctl is-active "$u.service" 2>/dev/null || true)" != active || {
    echo STOP_CONTROLLER; exit 1
  }
done
refuse_loaded_research
zones_ok /sys/class/thermal || exit 1
echo "zones$ZONE_LINE hottest=$HOTTEST"
utils=$(nvidia-smi --query-gpu=utilization.gpu,temperature.gpu --format=csv,noheader,nounits 2>/dev/null) || {
  echo STOP_GPU nvidia-smi_failed
  exit 1
}
gpu_parse "$utils" || exit 1
echo "gpu$GPU_LINE"
mapfile -t paths < <(
  for h in /sys/class/hwmon/hwmon*; do
    test "$(cat "$h/name" 2>/dev/null || true)" = acpi_fan || continue
    for f in "$h"/fan*_input; do
      test -r "$f" && printf '%s\n' "$f"
    done
  done | sort
)
test "${#paths[@]}" -eq 2 || { echo "STOP_FAN_COUNT ${#paths[@]}"; exit 1; }
a=$(cat "${paths[0]}") || { echo STOP_READ; exit 1; }
b=$(cat "${paths[1]}") || { echo STOP_READ; exit 1; }
echo "fans ${paths[0]}=$a ${paths[1]}=$b"
echo "PREFLIGHT_DONE no module loaded. One fan pair is not the fifteen-sample baseline."
echo "The idle pair after the partial 160 reboot is already reported. This reading does not replace the launcher's own fifteen samples."
