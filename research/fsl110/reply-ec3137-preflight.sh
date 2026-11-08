#!/bin/bash
# Read-only prerequisites for the EC 3.13.7 reply observer.
# Does not load a module and does not write a fan level.
# A present kallsyms entry does not prove the FFH buffer layout.
set -u
if [ -n "${THERMAL_ROOT+x}" ] || [ -n "${NVIDIA_SMI_STUB+x}" ] || [ "${RAMP_SOURCE_ONLY:-}" = 1 ]; then
  echo STOP_TEST_ENV
  exit 1
fi
cd "$(dirname "$0")"
# shellcheck source=ec-profile.sh
source ./ec-profile.sh
echo "HOST_DATE $(date -Is)"
echo "HOST_DATE is date -Is with this machine's offset. Do not relabel that offset as PST."
test "$(id -u)" -eq 0 || { echo STOP_NOT_ROOT; exit 1; }
require_ec 3.13.7
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
if [ ! -r /proc/kallsyms ]; then
  echo "symbol=unreadable"
  echo STOP_SYMBOL
  exit 1
fi
if grep -F -q ' acpi_ffh_address_space_arch_handler' /proc/kallsyms; then
  echo "symbol=present"
else
  echo "symbol=absent"
  echo STOP_SYMBOL
  exit 1
fi
echo "layout=unverified"
echo "Mainline arm64 FFH offset 1 stores the SMCCC result at buffer offsets 0 and 0x20. This tree does not contain the running kernel's ffh.c."
echo "PREFLIGHT_DONE no module loaded. This does not authorize a write."
