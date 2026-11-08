#!/bin/bash
# Optional ARM64 virt guest under QEMU TCG. Not part of ./scripts/check.
# Not nested KVM. Not a Spark, an EC, or a GPU.
#
# Packages installed when the commands or the UEFI firmware are missing:
#   qemu-system-arm qemu-efi-aarch64 gcc-aarch64-linux-gnu acpica-tools
#   flex bison bc libssl-dev libelf-dev cpio xz-utils wget ca-certificates
#   make
# Firmware: /usr/share/qemu-efi-aarch64/QEMU_EFI.fd
# Kernel: Linux 6.12.12, defconfig, ACPI, ACPI_FFH, kprobes, kretprobes.
# Boot: qemu-system-aarch64 -machine virt,acpi=on -cpu cortex-a57 -bios
#   QEMU_EFI.fd -append 'acpi=force'. Without the firmware, acpi=force
#   produced no console. Without acpi=force the interpreter stayed off.
# The SSDT only stores FLVL. It raises no FFH event, so reply writes stay 0.
#
# Apt package versions are not pinned. The kernel tarball is 6.12.12 unless
# DGX_GUEST_KVER overrides it. DGX_GUEST_BOOT_TIMEOUT bounds the QEMU process
# and defaults to 180 seconds. A cold TCG boot can sit with an empty serial
# log for more than 60 seconds. A later start of the same Image, initrd, and
# modules can finish in about 2 seconds. A timeout keeps serial.log and
# exits non-zero. The script does not start QEMU a second time.
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
work="${DGX_GUEST_WORK:-/tmp/dgx-guest}"
kver="${DGX_GUEST_KVER:-6.12.12}"
boot_timeout="${DGX_GUEST_BOOT_TIMEOUT:-180}"
jobs="$(nproc)"
case "$boot_timeout" in
  ''|*[!0-9]*)
    echo "GUEST_BOOT_TIMEOUT_INVALID ${boot_timeout}"
    exit 1
    ;;
esac
mkdir -p "$work"
exec > >(tee "$work/guest.log") 2>&1
echo "GUEST_BUILD_START $(date -Is)"
seconds=$SECONDS

if ! command -v qemu-system-aarch64 >/dev/null || ! command -v aarch64-linux-gnu-gcc >/dev/null || ! command -v iasl >/dev/null || [ ! -f /usr/share/qemu-efi-aarch64/QEMU_EFI.fd ]; then
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    qemu-system-arm qemu-efi-aarch64 gcc-aarch64-linux-gnu acpica-tools \
    flex bison bc libssl-dev libelf-dev cpio xz-utils wget ca-certificates make
fi

if [ ! -d "$work/linux-$kver" ]; then
  wget -O "$work/linux.tar.xz" \
    "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-$kver.tar.xz"
  tar -C "$work" -xf "$work/linux.tar.xz"
fi
kdir="$work/linux-$kver"

if [ ! -f "$kdir/.config" ]; then
  make -C "$kdir" ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- defconfig
  "$kdir/scripts/config" --file "$kdir/.config" \
    --disable DEBUG_INFO \
    --disable DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT \
    --disable MODULE_SIG \
    --disable MODULE_SIG_FORCE \
    --enable MODULES \
    --enable MODULE_UNLOAD \
    --enable KPROBES \
    --enable KRETPROBES \
    --enable ACPI \
    --enable ACPI_FFH
fi
if ! grep -q '^CONFIG_ACPI_FFH=y' "$kdir/.config"; then
  "$kdir/scripts/config" --file "$kdir/.config" --enable ACPI_FFH
fi
if [ ! -f "$kdir/arch/arm64/boot/Image" ] || ! grep -q '^CONFIG_ACPI_FFH=y' "$kdir/include/config/auto.conf" 2>/dev/null; then
  make -C "$kdir" ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- olddefconfig
  make -C "$kdir" ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- -j"$jobs" Image modules_prepare
  rm -f "$kdir/Module.symvers"
fi

cp "$root/tester/guest/ssdt.asl" "$work/ssdt.asl"
iasl -tc "$work/ssdt.asl" || {
  echo "GUEST_SSDT_IASL_FAIL"
  exit 1
}
ssdt="$work/ssdt.aml"

moddir="$work/modules"
rm -rf "$moddir"
mkdir -p "$moddir"
cat > "$moddir/Makefile" << 'EOF'
obj-m += fsl_probe.o
obj-m += fsl_reply.o
EOF
cp "$root/research/fsl110/fsl_probe.c" "$root/research/fsl110/fsl_reply.c" "$moddir/"
if [ ! -f "$kdir/Module.symvers" ]; then
  cp "$kdir/vmlinux.symvers" "$kdir/Module.symvers"
fi
make -C "$kdir" ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- M="$moddir" modules

aarch64-linux-gnu-gcc -static -O2 -Wall -o "$work/init" "$root/tester/guest/init.c"
stage="$work/stage"
rm -rf "$stage"
mkdir -p "$stage/kernel/firmware/acpi"
cp "$work/init" "$stage/init"
cp "$moddir/fsl_probe.ko" "$moddir/fsl_reply.ko" "$stage/"
# The kernel loads extra ACPI tables from an uncompressed initrd. This QEMU
# build has no -acpitable flag for aarch64.
cp "$ssdt" "$stage/kernel/firmware/acpi/ssdt.aml"
(cd "$stage" && find . | cpio -o -H newc) > "$work/initrd"
echo "GUEST_BOOT_START elapsed_build=$((SECONDS - seconds))s boot_timeout=${boot_timeout}s"
set +e
timeout "$boot_timeout" qemu-system-aarch64 \
  -machine virt,acpi=on \
  -cpu cortex-a57 \
  -m 512 \
  -nographic \
  -no-reboot \
  -bios /usr/share/qemu-efi-aarch64/QEMU_EFI.fd \
  -kernel "$kdir/arch/arm64/boot/Image" \
  -initrd "$work/initrd" \
  -append "console=ttyAMA0 earlycon rdinit=/init acpi=force" \
  > "$work/serial.log" 2>&1
qrc=$?
set -e
echo "GUEST_QEMU_EXIT $qrc elapsed_total=$((SECONDS - seconds))s"
if grep -q GUEST_OK "$work/serial.log"; then
  echo "GUEST_PASS"
  grep GUEST_ "$work/serial.log"
  exit 0
fi
echo "GUEST_BOOT_FAIL"
grep -E 'GUEST_|Kernel panic|not syncing' "$work/serial.log" | tail -40 || true
if [ "$qrc" -eq 124 ]; then
  exit 124
fi
exit 1
