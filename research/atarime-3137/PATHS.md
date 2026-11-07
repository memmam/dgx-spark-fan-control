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
| kprobe or handler replacement to grab the SMC reply | Not used. `acpi_ffh_address_space_arch_handler` is not a supported trace point, and replacing the handler would affect every fixed-hardware region. |
| `fst_status_atarime` | Ready, **unrun**. One read of the existing `_FST` package: revision, cached control, speed. That is the read path's published result, not `WRB`'s reply registers. |

## Live inventory, read-only

`fwupdmgr get-devices` is the supported list of what this machine's fwupd
reports (version, GUID, plugin, flags). It does not download or install.
`findmnt -t vfat` names a mounted EFI partition if the system already
mounted one; do not guess a disk node. `/lib/firmware` is the ordinary
Linux firmware directory. None of these is the EL3 SMC handler. If they
are only the LVFS capsules already studied, the handler is still absent.
