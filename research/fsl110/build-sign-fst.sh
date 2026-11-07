#!/bin/bash
# Build and sign the read-only _FST module. Does not insmod.
# Does not print key paths.
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=signing.sh
source ./signing.sh
resolve_signing_material
sign_named_module fst_status.ko
