# Read-only EC profile gate. Source this; call require_ec with 3.5.8 or 3.13.7.
# Does not change fwupd, does not load the fan controller, and does not print
# device records or key material. A hidden Embedded Controller is unverified.
# A kernel pin is the current boot's "pinned EC <version> capabilities" line.
# Disagreement, or no reading at all, stops the caller.

_ec_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
EC_PROFILE_PY="$_ec_root/scripts/ec_profile.py"

require_ec() {
  local expect="$1" fw="" kern="" verdict
  if command -v fwupdmgr >/dev/null 2>&1; then
    fw=$(timeout 5 fwupdmgr get-devices --json 2>/dev/null || true)
  fi
  kern=$(timeout 5 dmesg 2>/dev/null || true)
  local fw_n kern_n
  fw_n=$(printf '%s' "$fw" | LC_ALL=C wc -c)
  kern_n=$(printf '%s' "$kern" | LC_ALL=C wc -c)
  fw_n=${fw_n// /}
  kern_n=${kern_n// /}
  verdict=$(
    {
      printf 'FWUPD %s\n' "$fw_n"
      printf '%s' "$fw"
      printf '\nKERN %s\n' "$kern_n"
      printf '%s' "$kern"
    } | python3 "$EC_PROFILE_PY" decide --expect "$expect"
  ) || {
    echo STOP_EC_UNVERIFIED
    exit 1
  }
  case "$verdict" in
    OK) ;;
    STOP_EC_MISMATCH|STOP_EC_UNVERIFIED)
      echo "$verdict"
      exit 1
      ;;
    *)
      echo STOP_EC_UNVERIFIED
      exit 1
      ;;
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
