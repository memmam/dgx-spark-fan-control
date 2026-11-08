#!/bin/bash
# Bash 5.2 echo through a tee process substitution is visible before a
# later sleep. This is the hang-log hypothesis from the procedure audit.
# It does not touch hardware.
set -euo pipefail
out=$(mktemp)
bash -c '
exec > >(tee "$1")
echo LINE_BEFORE_SLEEP
sleep 1
echo LINE_AFTER_SLEEP
' _ "$out" &
sleep 0.4
mid=$(cat "$out" || true)
wait || true
case "$mid" in
  *LINE_BEFORE_SLEEP*) ;;
  *) echo "FAIL pre-sleep line was not in the tee file: [$mid]"; rm -f "$out"; exit 1 ;;
esac
case "$mid" in
  *LINE_AFTER_SLEEP*) echo "FAIL post-sleep line appeared early: [$mid]"; rm -f "$out"; exit 1 ;;
esac
rm -f "$out"
echo "log_flush_visible_ok"
