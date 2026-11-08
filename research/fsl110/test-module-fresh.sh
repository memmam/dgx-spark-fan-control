#!/bin/bash
# Offline checks for the module freshness gate. No hardware and no insmod.
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=module-fresh.sh
source ./module-fresh.sh
root=$(mktemp -d)
bin="$root/bin"
mkdir -p "$bin"
cat > "$bin/modinfo" << 'EOF'
#!/bin/bash
field=""
file=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -F) field=$2; shift 2 ;;
    *) file=$1; shift ;;
  esac
done
cat "$file.$field"
EOF
chmod 755 "$bin/modinfo"
krel=$(uname -r)
ko="$root/demo.ko"
src="$root/demo.c"
: > "$src"
: > "$ko"
echo "$krel SMP preempt" > "$ko.vermagic"
echo "Test Signer" > "$ko.signer"
# ko must not be older than src
touch -d '2020-01-01' "$src"
touch -d '2020-01-02' "$ko"
PATH="$bin:$PATH"
if ! out=$(require_built_module "$ko" "$src" 2>&1); then
  echo "FAIL fresh module rejected: $out"
  exit 1
fi
case "$out" in
  *vermagic_ok*signer_present*) ;;
  *) echo "FAIL missing ok line: $out"; exit 1 ;;
esac
touch -d '2020-01-03' "$src"
set +e
out=$(require_built_module "$ko" "$src" 2>&1)
rc=$?
set -e
test "$rc" -ne 0
test "$out" = "STOP_STALE_MODULE"
echo "stale" > "$ko.signer"
touch -d '2020-01-04' "$ko"
: > "$ko.signer"
set +e
out=$(require_built_module "$ko" "$src" 2>&1)
rc=$?
set -e
test "$rc" -ne 0
test "$out" = "STOP_UNSIGNED"
echo "Test Signer" > "$ko.signer"
echo "not-this-kernel SMP" > "$ko.vermagic"
set +e
out=$(require_built_module "$ko" "$src" 2>&1)
rc=$?
set -e
test "$rc" -ne 0
test "$out" = "STOP_VERMAGIC"
rm -rf "$root"
if ! grep -q 'for ko in fsl110.ko fsl_ramp.ko fsl_probe.ko fst_status.ko fsl_reply.ko' signing.sh; then
  echo "FAIL signer does not cover every research module"
  exit 1
fi
makes=$(grep -c 'make -C' signing.sh || true)
test "$makes" = 1
echo "module_fresh_ok"
