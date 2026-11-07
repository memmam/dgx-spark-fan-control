# EC 3.13.7 fan paths

No new Atarime write. Unchanged tach at arguments 110 and 120 is not a
firmware rejection. `interpreter_ok` is not an acknowledgement.

## What the bytes show

The captured DSDT is the same in archives `8f6831` and `981cff`
(SHA-256 `1a160b3bfcbd0d61df99fd3aa468582b4142ed1e2615a2b50c00cd808d726908`).
`_FSL` calls `WFCF`. `WFCF` calls `WRB` four times. `WRB` writes the SMC
buffer and does not compare `XX0` with `0xC400008E`. That compare is only
in `_FST`, and only to decide whether a speed word is published.

`drivers/acpi/arm64/ffh.c` handles the fan region (`FFixedHW` offset 1,
the 64-bit path). If the function id is a fast 64-bit STANDARD, SIP, or
OEM call, it issues the SMC, copies the result registers back, and returns
success. It does not read those registers and it does not log them. The
research caller passes a NULL return buffer, so the reply is gone when the
method ends. `~/atarime-fsl110-kernel.log` was not supplied in full. The
probe module would not have printed the reply anyway.

Family 7 through SoC service `0x81` is a different command. On EC 3.13.7,
selectors 3 and 5 return `0xff` and do not store a floor or ceiling. The
public EC capsules do not contain the ACPI addresses `0x06000790`–
`0x06000793` as immediates, so that `0xff` is not shown to be this write.
The fan-relay slice is identical in SoC 2.155.11 and 2.158.15
(`SOC-TRANSPORT.md`). No second duty setter is in that slice.

## Blobs that are not here

The SMC `0xC400008D` handler is not in the fan-relay slice. The UEFI slot
in both public SoC capsules is `0xFF` padding (`UEFI-GET.md`). The recovery
archive `dgx-spark-recovery-image-1.155.49.tar.gz`
(SHA-256 `43ba19088676550a76c1a25cd9aa25cbb01cbd6bab572b0d8b391a8b6f4b5de7`)
contains the same SoC and EC capsules, plus shim/GRUB, not the platform
ROM. The UEFI build named in that manifest is internal
(`gitlab-master.nvidia.com/digits/uefi/ami`, tag `1.111.17`). The coreboot
tree it points at is on `artifactory.nvidia.com`, which does not resolve
from the analysis network. Those are the images that could implement the
ACPI SMC. They are not in the saved captures.

## Tests

| Item | Status |
| --- | --- |
| Another `_FSL` level, including 110 and 120 | Blocked. No new evidence goal. |
| kprobe or handler replacement to grab the SMC reply | Not used. Replacing `acpi_ffh_address_space_arch_handler` would affect every fixed-hardware region. |
| `fst_status_atarime` | PASS as a read, 7 Nov 2026 15:11:56 PST. Package was `0 0 2068` and `0 0 2156`. Cached control is 0. Speeds match `fan1_input`. Not a write acknowledgement, not a clear floor, not a reboot confirmation. Do not repeat. |

## What Atarime's fwupd report adds

Embedded Controller `0x03000d07` (minimum `0x02003400`), GUID
`3d13c989-e6a8-4ead-95ee-921f09868f65`, updated `0x03000508` to
`0x03000d07`, capsule on disk. UEFI device firmware
`b488217b-3895-4fc0-b1bf-ab7005a2d45a` at `0x02009e0f` and
`dd1a238a-5f8e-46bd-9401-a88da99c5a96` at `0x00000519`, both success,
capsule on disk. `/boot/efi` is the mounted vfat volume. `nvidia_ffa_ec`
is builtin at `drivers/platform/arm64/nvidia-ffa-ec`. A capsule on disk is
the update image fwupd staged. It is not the SMC handler that is running.
The builtin driver is the host side of FF-A. It is not that handler either.

## Next listing, still missing

The EFI directory names were not listed. One level of `/boot/efi/EFI`,
then one more level only if a name is `fwupd`, `UpdateCapsule`, or
`NVIDIA`, with sizes and no file contents. Under `/lib/firmware`, only
`nvidia`, `7.0.0-1019-nvidia`, and `7.0.0-1024-nvidia`, and only names
matching `ffa`, `ec`, or `soc`. Under `/usr/src`, only a file whose name
contains `nvidia-ffa-ec`. Stop when those listings are in hand. Do not
read `/dev/nvme0n1p1`, do not dump EFI variables, and do not install or
update firmware. If the listings are boot loaders plus the capsules
already identified, the running SMC handler is still not a file on this
machine.
