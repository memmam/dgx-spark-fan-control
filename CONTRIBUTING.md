# Contributing

Issues and pull requests are welcome. Include your exact hardware, kernel and
firmware versions when reporting compatibility or thermal behavior. Please use
small, reviewable changes and describe how you checked them.

Run `./scripts/check` before submitting. It exports `DGX_FAN_CONTROL_OFFLINE=1`.
While that is set, the process refuses `systemctl`, module loaders, fwupd,
`nvidia-smi`, `dmesg`, reboot and poweroff, shell commands that name those
tools, process signals, and writes under `/sys`, `/etc`, `/var`, and the
other host config roots. A mock that calls back into `subprocess` still hits
the refusal. Direct `unittest` discovery loads the same guard. The variable
is not set in the fan services, so a normal watchdog or `set-state` is
unchanged. Policy tests use temporary directories. Transaction tests compile
the driver's C functions against a fake FF-A peer. `research/acpi-floor-sweep`
is not part of the check; running it requires `DGX_FAN_HARDWARE_TEST=1`.
Driver changes also need a `W=1` build on the target kernel and
an explicitly documented hardware validation result or testing limitation.

Keep `PACKAGE_VERSION` in `dkms.conf` aligned with `DGX_EC_DRIVER_VERSION`
in `kernel/dgx_ec_fan_control.c`, and update versioned installation examples.
Any change under `kernel/` must bump that version. `./scripts/check` hashes
`kernel/Makefile` and `kernel/dgx_ec_fan_control.c` and compares them with
`kernel/SOURCE_REV` (`<version> <sha256>`). Refresh that file in the same
change. A kernel edit that leaves the version at the previous hash fails
the check, which is what keeps `modinfo` from matching a module that does
not contain the new source.
DKMS changes need a target build using isolated source/state directories;
documentation-only checks must not enroll keys or alter a running controller.

Keep the interface narrow: a validated additive common lower floor, RPM
observability, and automatic restoration. Do not add raw packets, EC memory
access, upper clamps, or an interface that reduces firmware-requested cooling.
Keep documentation consistent with error paths as well as successful behavior.
Do not claim independent per-fan control or guaranteed crash rollback.

Contributions are under GPL-2.0-only. Do not include proprietary firmware,
signing keys, generated modules, private machine logs, or credentials.
