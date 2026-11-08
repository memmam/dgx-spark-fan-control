#!/bin/bash
# Read-only. Print decoded EC sources and the verdict. Does not load a
# module, does not change fwupd, and does not print device records.
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=ec-profile.sh
source ./ec-profile.sh
expect="${1:-}"
case "$expect" in
  3.5.8|3.13.7) ;;
  *)
    echo "usage: ec-profile-status.sh 3.5.8|3.13.7" >&2
    exit 2
    ;;
esac
line=$(ec_report_line "$expect") || {
  echo STOP_EC_UNVERIFIED
  exit 1
}
echo "$line"
case "$line" in
  *" verdict=OK") exit 0 ;;
  *) exit 1 ;;
esac
