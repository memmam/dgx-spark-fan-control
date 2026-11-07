#!/bin/bash
# Offline. Fixture checks call the library only. The launcher is executed
# solely to prove test overrides die before hardware.
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=ramp-temps-lib.sh
source ./ramp-temps-lib.sh
root=$(mktemp -d)
mkdir -p "$root/thermal_zone0" "$root/thermal_zone1"
echo acpitz > "$root/thermal_zone0/type"
echo 42000 > "$root/thermal_zone0/temp"
echo acpitz > "$root/thermal_zone1/type"
echo 47000 > "$root/thermal_zone1/temp"
zones_ok "$root"
case "$ZONE_LINE" in
  *"thermal_zone0:acpitz=42000mC"*"thermal_zone1:acpitz=47000mC"*) ;;
  *) echo "FAIL zone line $ZONE_LINE"; exit 1 ;;
esac
case "$HOTTEST" in
  "thermal_zone1:acpitz=47000mC (47.000C)") ;;
  *) echo "FAIL hottest $HOTTEST"; exit 1 ;;
esac
gpu_parse $'0, 41'
case "$GPU_LINE" in
  *"nvidia_gpu_temp=41C"*) ;;
  *) echo "FAIL gpu $GPU_LINE"; exit 1 ;;
esac
echo 70000 > "$root/thermal_zone1/temp"
if zones_ok "$root"; then echo "FAIL accepted hot zone"; exit 1; fi
rm -f "$root/thermal_zone0/temp"
if zones_ok "$root"; then echo "FAIL accepted missing temp"; exit 1; fi
echo "fixture_prewrite_ok"
base_line=$(grep -n 'STOP_NOT_BASELINE' ramp-kensaki.sh | head -1 | cut -d: -f1)
insmod_line=$(grep -n 'insmod ./fsl_ramp_kensaki.ko' ramp-kensaki.sh | head -1 | cut -d: -f1)
test "$base_line" -lt "$insmod_line"
# The launcher must not consult these names after the reject line.
reject_line=$(grep -n 'STOP_TEST_ENV' ramp-kensaki.sh | head -1 | cut -d: -f1)
if awk -v n="$reject_line" 'NR>n && /THERMAL_ROOT|NVIDIA_SMI_STUB|RAMP_SOURCE_ONLY/' ramp-kensaki.sh | grep -q .; then
  echo "FAIL override name remains after the reject"
  exit 1
fi
reject_live() {
  local label="$1"
  shift
  local out rc
  set +e
  out=$("$@" ./ramp-kensaki.sh 2>&1)
  rc=$?
  set -e
  printf '%s\n' "$out"
  test "$rc" -ne 0
  test "$out" = "STOP_TEST_ENV"
  case "$out" in
    *loaded*|*writing\ step*|*insmod*) echo "FAIL $label reached hardware"; exit 1 ;;
  esac
  echo "override_rejected_before_hardware $label"
}
reject_live THERMAL_ROOT env THERMAL_ROOT=/tmp
reject_live NVIDIA_SMI_STUB env NVIDIA_SMI_STUB='0, 41'
reject_live RAMP_SOURCE_ONLY env RAMP_SOURCE_ONLY=1
grep -q 'none is filled in here' ../../docs/validation.md
echo "ramp_temps_ok"
