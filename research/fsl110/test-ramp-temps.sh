#!/bin/bash
# Offline checks for ramp temperature logging. No /sys writes and no module.
set -euo pipefail
cd "$(dirname "$0")"
root=$(mktemp -d)
mkdir -p "$root/thermal_zone0" "$root/thermal_zone1"
echo acpitz > "$root/thermal_zone0/type"
echo 42000 > "$root/thermal_zone0/temp"
echo acpitz > "$root/thermal_zone1/type"
echo 47000 > "$root/thermal_zone1/temp"
export THERMAL_ROOT=$root
export NVIDIA_SMI_STUB=$'0, 41'
export RAMP_SOURCE_ONLY=1
source ./ramp-kensaki.sh
zones_ok
case "$ZONE_LINE" in
  *"thermal_zone0:acpitz=42000mC"*"thermal_zone1:acpitz=47000mC"*) ;;
  *) echo "FAIL zone line $ZONE_LINE"; exit 1 ;;
esac
case "$HOTTEST" in
  "thermal_zone1:acpitz=47000mC (47.000C)") ;;
  *) echo "FAIL hottest $HOTTEST"; exit 1 ;;
esac
gpu_idle
case "$GPU_LINE" in
  *"nvidia_gpu_temp=41C"*) ;;
  *) echo "FAIL gpu $GPU_LINE"; exit 1 ;;
esac
echo 70000 > "$root/thermal_zone1/temp"
if zones_ok; then echo "FAIL accepted hot zone"; exit 1; fi
echo acpitz > "$root/thermal_zone1/type"
rm -f "$root/thermal_zone0/temp"
if zones_ok; then echo "FAIL accepted missing temp"; exit 1; fi
# Baseline rejection is still before insmod, so a bad pair cannot load.
base_line=$(grep -n 'STOP_NOT_BASELINE' ramp-kensaki.sh | head -1 | cut -d: -f1)
insmod_line=$(grep -n 'insmod ./fsl_ramp_kensaki.ko' ramp-kensaki.sh | head -1 | cut -d: -f1)
test "$base_line" -lt "$insmod_line"
# The 14:55 2610/3915 capture is not given a temperature by this test.
grep -q 'none is filled in here' ../docs/validation.md 2>/dev/null || \
  grep -q 'none is filled in here' ../../docs/validation.md
echo "ramp_temps_ok"
