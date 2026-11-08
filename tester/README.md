# Spark tester

Hardware-free runs of the research launchers, the reply-buffer code, and
`dgx-fan-control set-state`. The default command is:

```sh
./scripts/check
```

The launcher cases also run as `python3 -m unittest discover -s tester -v`.

## What the sandbox allows

Each launcher starts in a user and mount namespace (`unshare --user
--map-root-user --mount --propagation private --pid --fork`). `enter.sh`
sets up the mounts and then execs `tester/pid1`, which stays pid 1. It
forks the launcher and reaps every child with `wait(2)` until none remain.
TERM, INT, and HUP are forwarded to the launcher. Bash `wait` cannot reap
a process the launcher reparents, including the hostlog `tee`: that process
is not a bash job, and `wait -n` returns 127 whether the child exited 127
or no child is left. `pid1` keeps reaping after a 127. The namespace stays
up until `tee` has exited, so the last line is flushed. The status of the
namespace is the launcher's, including a signal exit such as 143.

`tester/nssetup` then:

- Bind-mounts the fixture on `/sys` over two empty tmpfs layers. One or two
  `umount` calls still do not show host sysfs. A third would. The launchers
  do not unmount `/sys`.
- Bind-mounts a fixture file on `/proc/modules`. `finit_module` returns
  `EPERM`. Nothing here writes a host kernel module.
- Mounts a private tmpfs on `/tmp` and on `/var/lib`. Host files in `/tmp`
  are not visible and are not written. If the checkout, the tool directory,
  or the run state is a subdirectory of `/tmp`, that directory is
  bind-mounted back onto the same path from a private tmpfs on `/mnt`.
  Preserving all of `/tmp` is refused. The refusal is
  `refusing to preserve all of /tmp`.
- Bind-mounts `/etc` read-only.
- When a signing test plants a kdir, covers the parent of `/lib/modules`
  with a private tmpfs, binds the other children back, and creates
  `/lib/modules/$(uname -r)/build` only on that tmpfs. The host path is
  not created and not modified. A host that has no `/lib/modules` stays
  that way.

`/proc/fsl*` and `/proc/fst_status*` are served by `tester/preload.c`.
procfs will not create those names. `insmod`, `rmmod`, `lsmod`,
`nvidia-smi`, `fwupdmgr`, `dmesg`, `mokutil`, `systemctl`, `modinfo`,
`make`, and `sleep` on `PATH` are `tester/fastshim`. `sleep 2` advances
the fixture clock. It does not wait two seconds. `systemctl` answers
`is-active`, `is-enabled`, `enable`, `disable`, and `show`. Any other
verb exits non-zero. `show` prints no properties.

The production scripts are not given a test switch. `THERMAL_ROOT`,
`NVIDIA_SMI_STUB`, and `RAMP_SOURCE_ONLY` still abort before any command.

## What actually runs

`tester/machine.py` is a Python model of the proc contract: status text,
one-step latch, tach program, and the reply text the preload returns.
The fast suite does not compile or execute `fsl_ramp.c`.

`fsl_reply.c` is compiled into `reply_harness` with kernel stubs. That
harness is the reply-buffer test and the text the model returns. The
production buffer is 16KiB. `snprintf` drops a line that would not fit
and sets the truncated flag. The `truncated N` footer is written only
when at least 32 bytes remain. If they do not, the footer is omitted
and a reader that requires it fails closed. That is not a proof for
every buffer size.

`research/fsl110/test-bounds.py` checks the 160..237 request list in
`fsl_ramp.c` against `ramp-ec358.sh`. `./scripts/check` runs it. It does
not execute the C.

## What the numbers mean

`tester/machine.py` keeps three values apart and writes the labels to
`evidence.txt` for each run:

| Value | Meaning in this fixture |
| --- | --- |
| Published tach | What `fan*_input` returns. A research reboot puts back 2700/4050 because that pair was observed. Unload does not. |
| AML `FLVL` | The level stored before the SMC, from the captured DSDT description. Cleared on reboot. The DSDT bytes are not in this tree. |
| Production floor | The driver's clamp, from `docs/protocol.md`. `reboot_research` does not change it. A 0 there is not the published tach. |

Arguments 160–237 moving the published pair, and argument 120 leaving it
still, are labeled assumptions. They are not shaft speeds. The spread dwell
(5000…5400) exists so a minimum cannot pass as the median of the last five.
The captured dwells were flat, and a flat dwell cannot show that.

## Optional guest

```sh
bash tester/guest/run-guest.sh
```

This is not part of `./scripts/check`. On a machine that is missing them,
the script installs whatever versions the distro currently provides of:

`qemu-system-arm`, `qemu-efi-aarch64`, `gcc-aarch64-linux-gnu`,
`acpica-tools`, `flex`, `bison`, `bc`, `libssl-dev`, `libelf-dev`,
`cpio`, `xz-utils`, `wget`, `ca-certificates`, and `make`.

Those package versions are not pinned. The kernel tarball is pinned to
Linux 6.12.12 unless `DGX_GUEST_KVER` is set. The boot is an ARM64 `virt`
machine with TCG, not nested KVM. The firmware file is
`/usr/share/qemu-efi-aarch64/QEMU_EFI.fd`. The kernel command line uses
`acpi=force`. Without that firmware, `acpi=force` produced no console.
Without `acpi=force`, the interpreter stayed off.

`DGX_GUEST_BOOT_TIMEOUT` bounds QEMU, default 180 seconds. A cold TCG
boot can produce an empty serial log for longer than 60 seconds and then
still come up. A later start of the same Image, initrd, and modules can
finish in about 2 seconds. Hitting the budget is a failure: the serial
log is left in place and the script exits non-zero. It does not start
QEMU again.

The SSDT in `tester/guest/ssdt.asl` is labeled synthetic. It only stores
`FLVL`. It does not raise an FFH operation, so the reply log shows
`writes 0`. The guest loads real `fsl_reply.ko` and `fsl_probe.ko` and
evaluates `\_SB.FAN0._FSL`. It does not emulate the EC, the GPU, or SMC
`0xC400008D`. A passing boot prints `GUEST_OK`. The last log is
`tester/guest/last-run.txt`.
