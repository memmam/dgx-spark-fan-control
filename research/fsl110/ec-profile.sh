# Read-only EC profile gate. Source this; call require_ec with 3.5.8 or 3.13.7.
# Does not change fwupd, does not load the fan controller, and does not print
# device records or key material.
# fwupd omits an Embedded Controller whose updates are blocked.
# The kernel pin exists only after the fan module has probed.
# The EFI system resource table still has that class's fw_version.
# Disagreement, or no reading at all, stops the caller.

_ec_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
EC_PROFILE_PY="$_ec_root/scripts/ec_profile.py"

_ec_frame() {
  local fw="$1" kern="$2" esrt="$3" fw_n kern_n esrt_n
  fw_n=$(printf '%s' "$fw" | LC_ALL=C wc -c)
  kern_n=$(printf '%s' "$kern" | LC_ALL=C wc -c)
  esrt_n=$(printf '%s' "$esrt" | LC_ALL=C wc -c)
  fw_n=${fw_n// /}
  kern_n=${kern_n// /}
  esrt_n=${esrt_n// /}
  printf 'FWUPD %s\n' "$fw_n"
  printf '%s' "$fw"
  printf '\nKERN %s\n' "$kern_n"
  printf '%s' "$kern"
  printf '\nESRT %s\n' "$esrt_n"
  printf '%s' "$esrt"
}

_ec_collect() {
  EC_FW=""
  EC_KERN=""
  EC_ESRT=""
  if command -v fwupdmgr >/dev/null 2>&1; then
    EC_FW=$(timeout 5 fwupdmgr get-devices --json 2>/dev/null || true)
  fi
  EC_KERN=$(timeout 5 dmesg 2>/dev/null || true)
  EC_ESRT=$(python3 "$EC_PROFILE_PY" esrt 2>/dev/null || true)
}

ec_report_line() {
  local expect="$1"
  _ec_collect
  _ec_frame "$EC_FW" "$EC_KERN" "$EC_ESRT" | python3 "$EC_PROFILE_PY" explain --expect "$expect"
}

require_ec() {
  local expect="$1" line
  line=$(ec_report_line "$expect") || {
    echo STOP_EC_UNVERIFIED
    exit 1
  }
  echo "$line"
  case "$line" in
    *" verdict=OK") ;;
    *) exit 1 ;;
  esac
}

refuse_loaded_research() {
  if lsmod | awk '$1 ~ /^(fsl|fst_status)/ { found = 1 } END { exit found ? 0 : 1 }'; then
    echo STOP_RESEARCH_MODULE
    exit 1
  fi
  local extra
  extra=$(compgen -G '/proc/fsl*' || true)
  extra="$extra $(compgen -G '/proc/fst_status*' || true)"
  if [ -n "${extra// /}" ]; then
    echo STOP_RESEARCH_MODULE
    exit 1
  fi
}
