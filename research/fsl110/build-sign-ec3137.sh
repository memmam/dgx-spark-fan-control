#!/bin/bash
# Build and sign fsl110.ko for an EC 3.13.7 read of the same module.
# Does not insmod. Does not print key paths. Profile is checked by the launcher.
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=signing.sh
source ./signing.sh
resolve_signing_material
sign_named_module fsl110.ko
