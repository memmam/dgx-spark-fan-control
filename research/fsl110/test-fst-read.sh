#!/bin/bash
# Offline checks for the Atarime _FST reader. No hardware.
set -euo pipefail
cd "$(dirname "$0")"
src=$(cat fst_status_atarime.c)
case "$src" in
  *'_FST'*) ;;
  *) echo "FAIL missing _FST"; exit 1 ;;
esac
case "$src" in
  *'_FSL'*|*'0xC400008D'*|*'0x06000790'*) echo "FAIL writer constant present"; exit 1 ;;
esac
case "$src" in
  *'ACPI_ALLOCATE_BUFFER'*) ;;
  *) echo "FAIL no return buffer"; exit 1 ;;
esac
bash -n read-fst-atarime.sh
bash -n build-sign-fst-atarime.sh
reject_line=$(grep -n 'STOP_TEST_ENV' read-fst-atarime.sh | head -1 | cut -d: -f1)
insmod_line=$(grep -n 'insmod ./fst_status_atarime.ko' read-fst-atarime.sh | head -1 | cut -d: -f1)
test "$reject_line" -lt "$insmod_line"
if awk -v n="$reject_line" 'NR>n && /THERMAL_ROOT|NVIDIA_SMI_STUB|RAMP_SOURCE_ONLY/' read-fst-atarime.sh | grep -q .; then
  echo "FAIL override name remains after the reject"
  exit 1
fi
for label in THERMAL_ROOT NVIDIA_SMI_STUB RAMP_SOURCE_ONLY; do
  if [ "$label" = THERMAL_ROOT ]; then
    out=$(env THERMAL_ROOT=/tmp ./read-fst-atarime.sh 2>&1 || true)
  elif [ "$label" = NVIDIA_SMI_STUB ]; then
    out=$(env NVIDIA_SMI_STUB='0, 41' ./read-fst-atarime.sh 2>&1 || true)
  else
    out=$(env RAMP_SOURCE_ONLY=1 ./read-fst-atarime.sh 2>&1 || true)
  fi
  test "$out" = "STOP_TEST_ENV"
  echo "override_rejected_before_hardware $label"
done
echo "fst_read_offline_ok"
