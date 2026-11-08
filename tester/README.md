# Spark tester

Hardware-free runs of the research launchers, the reply-buffer code, and
`dgx-fan-control set-state`. The default command is:

```sh
./scripts/check
```

The launcher cases also run as `python3 -m unittest discover -s tester -v`.

## What the sandbox allows

Each launcher starts in a user and mount namespace (`unshare --user
--map-root-user --mount --pid`). Inside it:

- `/sys` is a bind mount of the fixture. Host sysfs is not mounted.
- `/proc/modules` is a fixture file. `finit_module` returns `EPERM`.
- `/proc/fsl*` and `/proc/fst_status*` are served by `tester/preload.c`.
  procfs will not create those names.
- `insmod`, `rmmod`, `lsmod`, `nvidia-smi`, `fwupdmgr`, `dmesg`, `mokutil`,
  `systemctl`, `modinfo`, `make`, and `sleep` on `PATH` are `tester/fastshim`.
- `sleep 2` advances the fixture clock. It does not wait two seconds.

The production scripts are not given a test switch. `THERMAL_ROOT`,
`NVIDIA_SMI_STUB`, and `RAMP_SOURCE_ONLY` still abort before any command.

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

This is not part of `./scripts/check`. It installs a cross compiler and QEMU
if they are missing, downloads Linux 6.12.12, and boots an ARM64 `virt`
machine with TCG. The SSDT in `tester/guest/ssdt.asl` is labeled synthetic.
The guest loads `fsl_reply.ko` and `fsl_probe.ko` and evaluates
`\_SB.FAN0._FSL`. It does not emulate the EC, the GPU, or SMC `0xC400008D`.
A passing boot prints `GUEST_OK`.
