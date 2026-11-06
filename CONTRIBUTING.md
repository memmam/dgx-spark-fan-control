# Contributing

Issues and pull requests are welcome. Include your exact hardware, kernel and
firmware versions when reporting compatibility or thermal behavior. Please use
small, reviewable changes and describe how you checked them.

Run `./scripts/check` before submitting. Policy/filesystem tests use temporary
directories and mocks; they must never read or write the test host's real
thermal sysfs. Transaction tests compile the driver's actual C functions with
simulated FF-A responses; they exercise failure recovery without contacting
firmware. Driver changes also need a `W=1` build on the target kernel and
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
