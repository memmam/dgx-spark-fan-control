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
echo "$krel SMP preempt" > "$ko.vermagic"
touch -d '2020-01-05' "$root/Makefile"
set +e
out=$(require_built_module "$ko" "$src" 2>&1)
rc=$?
set -e
test "$rc" -ne 0
test "$out" = "STOP_STALE_MODULE"
rm -rf "$root"
if ! grep -q 'for ko in fsl110.ko fsl_ramp.ko fsl_probe.ko fst_status.ko fsl_reply.ko' signing.sh; then
  echo "FAIL signer does not cover every research module"
  exit 1
fi
makes=$(grep -c 'make -C' signing.sh || true)
test "$makes" = 1

# Execute the sign-all function. make and sign-file are shims. No kernel build.
signroot=$(mktemp -d)
kdir="$signroot/kdir/build"
mkdir -p "$kdir/scripts" "$signroot/work" "$signroot/bin"
printf '%s\n' 'CONFIG_MODULE_SIG_HASH="sha512"' > "$kdir/.config"
cat > "$kdir/scripts/sign-file" << 'EOF'
#!/bin/bash
# hash key cert module
printf 'SIG %s\n' "$1" >> "$4"
printf '%s\n' "$4" >> "$SIGN_CALLS"
EOF
cat > "$signroot/bin/make" << 'EOF'
#!/bin/bash
target="$PWD"
for arg in "$@"; do
  case "$arg" in
    M=*) target="${arg#M=}" ;;
  esac
done
printf '%s\n' "$target" >> "$MAKE_CALLS"
for ko in fsl110.ko fsl_ramp.ko fsl_probe.ko fst_status.ko fsl_reply.ko; do
  printf 'UNSIGNED\n' > "$target/$ko"
done
EOF
cat > "$signroot/bin/modinfo" << 'EOF'
#!/bin/bash
exit 0
EOF
chmod 755 "$kdir/scripts/sign-file" "$signroot/bin/make" "$signroot/bin/modinfo"
: > "$signroot/key" 
: > "$signroot/cert"
: > "$signroot/make-calls"
: > "$signroot/sign-calls"
(
  cd "$signroot/work"
  export SIGN_KEY="$signroot/key" SIGN_CERT="$signroot/cert"
  export MAKE_CALLS="$signroot/make-calls" SIGN_CALLS="$signroot/sign-calls"
  export PATH="$signroot/bin:$PATH"
  # shellcheck source=signing.sh
  source "$OLDPWD/signing.sh"
  sign_research_modules "$kdir"
  sign_research_modules "$kdir"
)
test "$(wc -l < "$signroot/make-calls")" -eq 2
test "$(wc -l < "$signroot/sign-calls")" -eq 10
for ko in fsl110.ko fsl_ramp.ko fsl_probe.ko fst_status.ko fsl_reply.ko; do
  got=$(cat "$signroot/work/$ko")
  test "$got" = "$(printf 'UNSIGNED\nSIG sha512\n')"
done
rm -rf "$signroot"
echo "module_fresh_ok"
