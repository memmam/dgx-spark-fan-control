#!/bin/bash
# Kensaki only. Build and sign fsl_ramp_kensaki.ko. Does not insmod.
set -euo pipefail
cd "$(dirname "$0")"
hn=$(hostname -s 2>/dev/null || hostname); hn=${hn%%.*}
test "$hn" = kensaki || { echo STOP_HOST; exit 1; }
sb=$(mokutil --sb-state 2>&1 || true)
printf '%s\n' "$sb"
case "$sb" in
  *"SecureBoot enabled"*) ;;
  *) echo STOP_SECUREBOOT; exit 1 ;;
esac
files=(/etc/dkms/framework.conf)
if [ -d /etc/dkms/framework.conf.d ]; then
  for f in /etc/dkms/framework.conf.d/*.conf; do
    [ -f "$f" ] && files+=("$f")
  done
fi
one_setting() {
  local name="$1" f line val
  local -a vals=()
  for f in "${files[@]}"; do
    [ -f "$f" ] || continue
    while IFS= read -r line; do
      val=${line#*=}
      val=${val#"${val%%[![:space:]]*}"}
      val=${val%"${val##*[![:space:]]}"}
      val=${val#\"}
      val=${val%\"}
      vals+=("$val")
    done < <(grep -E "^[[:space:]]*${name}=" "$f" || true)
  done
  local uniq
  uniq=$(printf '%s\n' "${vals[@]:-}" | sed '/^$/d' | sort -u)
  test "$(printf '%s\n' "$uniq" | sed '/^$/d' | wc -l)" -eq 1 || {
    echo "STOP_SIGNING_CONFIG $name"; exit 1
  }
  printf '%s\n' "$uniq"
}
key=$(one_setting mok_signing_key)
cert=$(one_setting mok_certificate)
case "$key" in
  *.priv) ;;
  *) echo STOP_KEY_PATH; exit 1 ;;
esac
test -f "$key" || { echo STOP_NO_KEY_PATH; exit 1; }
test -f "$cert" || { echo STOP_NO_CERT_PATH; exit 1; }
enroll=$(mokutil --test-key "$cert" 2>&1 || true)
printf '%s\n' "$enroll"
case "$enroll" in
  *"is already enrolled"*) ;;
  *) echo STOP_CERT_NOT_ENROLLED; exit 1 ;;
esac
kdir="/lib/modules/$(uname -r)/build"
test -d "$kdir" || { echo "STOP_NO_HEADERS $kdir"; exit 1; }
hash=$(sed -n 's/^CONFIG_MODULE_SIG_HASH="\(.*\)"$/\1/p' "$kdir/.config")
test -n "$hash" || { echo STOP_NO_SIG_HASH; exit 1; }
make -C "$kdir" M="$PWD" modules
"$kdir/scripts/sign-file" "$hash" "$key" "$cert" "$PWD/fsl_ramp_kensaki.ko"
echo "SIGNED hash=$hash"
modinfo -F signer "$PWD/fsl_ramp_kensaki.ko" || true
echo "BUILD_SIGNED_NOT_LOADED"
