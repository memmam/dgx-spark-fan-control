# Refuse a module that is missing, older than its source or Makefile, for
# another kernel, or unsigned. Does not load the module and does not print
# key paths. A same-release header change that does not touch those files
# is not detected here.

require_built_module() {
  local ko="$1" src="$2" vermagic signer krel verm_rel makefile
  [ -f "$ko" ] || { echo STOP_NO_MODULE; exit 1; }
  [ -f "$src" ] || { echo STOP_NO_SOURCE; exit 1; }
  makefile=$(dirname "$src")/Makefile
  if [ "$ko" -ot "$src" ] || { [ -f "$makefile" ] && [ "$ko" -ot "$makefile" ]; }; then
    echo STOP_STALE_MODULE
    exit 1
  fi
  krel=$(uname -r)
  vermagic=$(modinfo -F vermagic "$ko" 2>/dev/null | head -n 1 || true)
  signer=$(modinfo -F signer "$ko" 2>/dev/null | head -n 1 || true)
  verm_rel=${vermagic%% *}
  if [ "$verm_rel" != "$krel" ]; then
    echo STOP_VERMAGIC
    exit 1
  fi
  if [ -z "$signer" ]; then
    echo STOP_UNSIGNED
    exit 1
  fi
  echo "module $(basename "$ko") vermagic_ok signer_present"
}
