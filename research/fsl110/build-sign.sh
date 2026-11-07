#!/bin/bash
# Build and sign fsl110.ko with the already enrolled MOK. Does not insmod.
set -euo pipefail
cd "$(dirname "$0")"
kdir="/lib/modules/$(uname -r)/build"
cfg="$kdir/.config"
key=/root/.local/share/dgx-spark-fan-control/keys/MOK.priv
cert=/root/.local/share/dgx-spark-fan-control/keys/MOK.der
test -d "$kdir" || { echo "STOP_NO_HEADERS $kdir"; exit 1; }
test -f "$key" || { echo "STOP_NO_KEY_PATH"; exit 1; }
test -f "$cert" || { echo "STOP_NO_CERT_PATH"; exit 1; }
hash=$(sed -n 's/^CONFIG_MODULE_SIG_HASH="\(.*\)"$/\1/p' "$cfg")
test -n "$hash" || { echo "STOP_NO_SIG_HASH"; exit 1; }
make -C "$kdir" M="$PWD" modules
"$kdir/scripts/sign-file" "$hash" "$key" "$cert" "$PWD/fsl110.ko"
echo "SIGNED hash=$hash"
modinfo -F signer "$PWD/fsl110.ko" || true
echo "BUILD_SIGNED_NOT_LOADED"
