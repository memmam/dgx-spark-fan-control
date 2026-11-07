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
| Another `_FSL` level, including 110 and 120 | Not prepared. Same-boot `_FST` control is fixed by AML. See below. |
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

## ESP and firmware names, 7 Nov 2026

`/boot/efi/EFI` contains `BOOT`, `ubuntu`, and `UpdateCapsule`.
`UpdateCapsule` is empty. `ubuntu/fw` is empty. `ubuntu` contains
`grubaa64.efi`, `grub.cfg`, `shimaa64.efi`, `mmaa64.efi`, `BOOTAA64.CSV`,
and `fw/`. `BOOT` contains `BOOTAA64.EFI`, `fbaa64.efi`, and `mmaa64.efi`.
No further EFI listing.

Under `/lib/firmware/nvidia`, `7.0.0-1019-nvidia`, and
`7.0.0-1024-nvidia`, the name filter `*ffa*`, `*ec*`, `*socfw*` matched
NVIDIA GPU firmware (`sec2`, `nvdec`, and `fecs` blobs for the `ga`, `gm`,
`gp`, `gv`, and `tu` families, plus `tu10x/typec/ccg_secondary.cyacd.zst`)
and kernel device-tree board files whose names contain those letters. No
name matched `ffa` or `socfw`. `/usr/src` has no `nvidia-ffa-ec*` file.
That is a filename filter, not a search of file contents. It does not show
that the SMC handler is absent from the machine.

An empty `UpdateCapsule` and an empty `ubuntu/fw` mean no capsule is staged
for a later boot. The fwupd history line "capsule-on-disk" names the method
of an update that already succeeded. Those directories do not still hold
the image. These UEFI capsule devices report the running version. They do
not export the resident firmware. A cabinet left in fwupd's cache would be
another copy of an update payload. The public EC 3.13.7 and SoC 2.158.15
capsules already studied do not contain this SMC handler in the fan-relay
slice, and the SoC capsule's UEFI slot is padding. A matching checksum
would not add the handler. A different checksum would name a file that is
not in the ESP directories above.

The host side is already known and does not need a kernel source download.
`drivers/acpi/arm64/ffh.c` issues the SMC, copies the result registers back,
and returns success without consulting them. `nvidia_ffa_ec` is the builtin
FF-A driver, a different service from SMC `0xC400008D`. Neither file is the
monitor that decides whether the setpoint is kept.

## What would move the write

Another directory listing does not show whether EC 3.13.7 kept the
setpoint. Unchanged tach, `interpreter_ok`, and cached control 0 still do
not separate a rejected write from a write the EC stored and did not apply.

`_FST` published both speeds, and they matched `fan1_input`. In the captured
DSDT that speed word is published only when the read SMC returns
`XX0 == 0xC400008E`. The read of `0x06000704` and `0x06000706` cleared that
check. The write is opcode `0x09` on `0x06000790` through `0x06000793`.
`WRB` does not make that comparison, and the research caller drops the
buffer. The read result does not transfer to the write.

Two later observations would move it. Neither is requested from this
listing, and neither is a new fan level.

1. The result registers of one existing `WRB` call, and whether `XX0` is
   `0xC400008E`. That is the sentinel the read path already returned. It is
   not a replacement of `acpi_ffh_address_space_arch_handler`. A success
   sentinel would still not prove the fan loop uses the stored bytes.
2. The image that implements SMC `0xC400008D`. The saved captures place that
   image outside the public EC immediates, outside the fan-relay slice, and
   outside the SoC capsule's UEFI slot. The recovery manifest names the
   internal AMI UEFI tree tag `1.111.17` and a coreboot artifact on
   `artifactory.nvidia.com`. Those are not the Ubuntu boot files listed
   above. Until one of those images is available, static inspection cannot
   show a consumer of `0x06000790`.

The Kensaki continuation stays blocked on the 2610/3915 baseline. No new
module follows from this inventory.

## Same-boot `_FST` control, not prepared

A same-boot `_FST` before and after one `_FSL` of 110 or 120 would read the
AML name `FLVL`. In the captured DSDT, `WFCF` begins with `FLVL = Arg0` and
only then calls `WRB` four times. That assignment does not look at `XX0`.
`FAN0._FSL` and `FAN1._FSL` both call `WFCF`. Both `_FST` methods copy
`FAN0.FLVL` into the control field. A completed `_FSL(120)` therefore leaves
both control fields at 120 for the rest of that namespace, including when
every SMC is ignored. `_FSL(110)` leaves 110. The 15:11 package control 0 is
the `Name (FLVL, Zero)` initializer. A later 0 is what a rebuilt namespace
shows. The store itself does not need another setpoint to establish that.

No launcher is added. Do not rerun 110 or 120 for this question.

Keeping the `WRB` reply would be a separate observation. A kretprobe on
`acpi_ffh_address_space_arch_handler` can read the buffer after the existing
handler returns and keep `XX0` when the request id is `0xC400008D`. That
does not replace the handler and does not issue a write. It is not
implemented here. `XX0 == 0xC400008E` would mean the monitor returned the
same sentinel the read path already uses. It would still leave the fan-loop
consumer unshown.
