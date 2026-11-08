# Resolve an already enrolled module key. Does not create or enroll a key
# and does not print the key path, the certificate path, or mokutil output.
# DGX_MOK_KEY and DGX_MOK_CERT override the DKMS paths when both are set.

resolve_signing_material() {
  local key="" cert="" sb enroll
  if [ -n "${DGX_MOK_KEY+x}" ] || [ -n "${DGX_MOK_CERT+x}" ]; then
    if [ -z "${DGX_MOK_KEY:-}" ] || [ -z "${DGX_MOK_CERT:-}" ]; then
      echo STOP_SIGNING_CONFIG
      exit 1
    fi
    key=$DGX_MOK_KEY
    cert=$DGX_MOK_CERT
  else
    local files=(/etc/dkms/framework.conf) f line val name
    if [ -d /etc/dkms/framework.conf.d ]; then
      for f in /etc/dkms/framework.conf.d/*.conf; do
        [ -f "$f" ] && files+=("$f")
      done
    fi
    one_setting() {
      name="$1"
      local -a vals=()
      local uniq
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
      uniq=$(printf '%s\n' "${vals[@]:-}" | sed '/^$/d' | sort -u)
      test "$(printf '%s\n' "$uniq" | sed '/^$/d' | wc -l)" -eq 1 || {
        echo STOP_SIGNING_CONFIG
        exit 1
      }
      printf '%s\n' "$uniq"
    }
    key=$(one_setting mok_signing_key)
    cert=$(one_setting mok_certificate)
  fi
  case "$key" in
    *.priv) ;;
    *) echo STOP_KEY_PATH; exit 1 ;;
  esac
  [ -f "$key" ] || { echo STOP_NO_KEY_PATH; exit 1; }
  [ -f "$cert" ] || { echo STOP_NO_CERT_PATH; exit 1; }
  sb=$(mokutil --sb-state 2>&1 || true)
  case "$sb" in
    *"SecureBoot enabled"*) ;;
    *) echo STOP_SECUREBOOT; exit 1 ;;
  esac
  enroll=$(mokutil --test-key "$cert" 2>&1 || true)
  case "$enroll" in
    *"is already enrolled"*) ;;
    *) echo STOP_CERT_NOT_ENROLLED; exit 1 ;;
  esac
  SIGN_KEY=$key
  SIGN_CERT=$cert
}

# One make, then sign every research module. Signing only one .ko after make
# leaves the others unsigned, because make relinks them.
sign_research_modules() {
  local kdir hash ko
  kdir="${1:-/lib/modules/$(uname -r)/build}"
  [ -d "$kdir" ] || { echo "STOP_NO_HEADERS"; exit 1; }
  hash=$(sed -n 's/^CONFIG_MODULE_SIG_HASH="\(.*\)"$/\1/p' "$kdir/.config")
  [ -n "$hash" ] || { echo STOP_NO_SIG_HASH; exit 1; }
  make -C "$kdir" M="$PWD" modules
  for ko in fsl110.ko fsl_ramp.ko fsl_probe.ko fst_status.ko fsl_reply.ko; do
    [ -f "$PWD/$ko" ] || { echo "STOP_NO_MODULE $ko"; exit 1; }
    "$kdir/scripts/sign-file" "$hash" "$SIGN_KEY" "$SIGN_CERT" "$PWD/$ko"
    echo "SIGNED $ko hash=$hash"
    modinfo -F signer "$PWD/$ko" || true
  done
  echo BUILD_SIGNED_NOT_LOADED
}
