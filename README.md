# DGX Spark Fan Control

> **ACPI on EC 3.5.8 replaces NVIDIA's fan curve until reboot.** Level 100
> is not full speed. On the measured machine it held about 4320 RPM, well
> below the stock curve at the same temperature. Do not hold ACPI under a
> sustained full load. Never write a level below 35, and never write 0.
> Thresholds, the stop hook, reboot, and poweroff are in
> [ACPI holds](docs/ec-curve.md#ec-358-acpi-holds-replace-the-stock-curve).
> The 85°C poweroff for fans that read 0 RPM is a separate rule:
> [stopped fans](docs/usage.md#watch-for-stopped-fans).


**Pre-cool your Spark, run your job, return to NVIDIA's fan curve.**

Control a shared minimum fan RPM from Linux userland, or run a temperature-based
performance curve. A small kernel driver talks to the existing firmware; a
Python command controls it. NVIDIA's embedded controller (EC) continues to
apply its own cooling policy throughout.

## Start here

| I want to… | Guide |
| --- | --- |
| Install with Secure Boot | [Installation, signing, and enrollment](docs/installation.md) |
| Rebuild automatically for kernel updates | [DKMS installation](docs/installation.md#3a-dkms-builds-for-kernel-updates) |
| Build and sign a module myself | [Manual installation](docs/installation.md#3b-manual-build-and-sign) |
| Choose what runs at boot | [Startup modes](docs/installation.md#5-choose-your-startup-mode) |
| Update an install you already have | [Update this project's source](docs/maintenance.md#update-this-projects-source) |
| Turn the fans up before a big job | [Pre-cooling](docs/usage.md#pre-cool-before-a-big-job) |
| Restore the factory fan curve afterward | [NVIDIA automatic control](docs/usage.md#return-to-nvidia-automatic-control) |
| Upgrade or remove the software | [Updates and removal](docs/maintenance.md) |

## A typical session

After [installation](docs/installation.md), stop the optional performance
service before selecting a manual floor:

```sh
sudo systemctl stop dgx-fan-control.service
sudo dgx-fan-control set-state 12   # profile maximum (9000/13500 RPM on EC 3.5.8)
dgx-fan-control status
```

Allow time for the fans to accelerate and temperatures to fall, then run your
workload normally. When finished, remove the added floor:

```sh
sudo dgx-fan-control automatic
dgx-fan-control status             # state=0/12 (daemon not setting a floor; EC's own curve)
```

Leave the service stopped to keep NVIDIA's curve active. Starting the service
selects this project's more aggressive curve. The [usage guide](docs/usage.md)
includes a Bash job example that restores automatic control on ordinary exit
or interruption.

## What you control

The driver exposes two read-only RPM sensors through Linux `hwmon`, and a
thermal cooling device with state 0 (automatic) plus 12 common RPM floors.
These are **minimums, not exact per-fan targets**. Each fan saturates at its own
maximum, and firmware can ask for more cooling than your floor.

There is no firmware flashing, upper-clamp control, arbitrary EC memory access,
or raw PWM interface. This cannot make the fans quieter than NVIDIA's own
policy. See the [state table and performance curve](docs/usage.md#choose-a-fan-floor).

## Compatibility

| Firmware | What this does |
| --- | --- |
| **EC 3.5.8** on DGX Spark, board P4242 | Fan-floor control. State 0 is NVIDIA's curve. Checked on DGX OS 7.5.0 and kernels `6.17.0-1029-nvidia` and `7.0.0-1019-nvidia`. The validation image is SoC 2.155.11. October SoC 2.158.15 has both bound and timed out the first read. A timeout is a reason to use ACPI while the module is unloaded. It is not proof that this pair can never bind. |
| **EC 3.13.7** | Telemetry only. Probe does not send a floor command and does not create a cooling device, including after a timeout. `status` prints `control unsupported on this firmware`. Floor writes fail because the EC no longer lets the host enable them. `allow_unverified_writes=1` is unqualified. Do not turn it on to test a machine whose fans have stopped. |
| Anything else | Rejected. An unknown capability reply does not bind. |

On EC 3.5.8, ACPI `cur_state` and this module write the same lower floor. This command refuses ACPI levels below 35. Level 0 stops both fans and does not clear a floor the module does not own. On EC 3.13.7 the same ACPI write is accepted and does not change RPM. An ACPI level does not survive reboot.

Version **0.1.7** leaves a mailbox timeout as that timeout. It does not submit a floor read afterward unless `automatic_floor_recovery=1`, and it never does so on EC 3.13.7 or before a profile is known. An unsupported floor reply is not retried. A timed-out floor write is not replayed. A floor this driver did not set stays in place; reboot is what restored the EC curve on the measured machines. Ranges, the 0xEB permission change, and older recovery behavior are in the [protocol](docs/protocol.md) and [troubleshooting](docs/troubleshooting.md#persistent-pending-and-service-exit-69).

You need Python 3.10+, matching NVIDIA kernel headers, and a signing key when Secure Boot is on. This project is independent of NVIDIA. The quieter opt-in curve is under [Quiet curve](#quiet-curve).

## Known issues

**Both fans read 0 RPM.** Seen twice on EC 3.13.7 on 5 Nov 2026 (kernel `7.0.0-1024-nvidia`, BIOS `5.36_0ACUM028`): once idle at 79°C, once on a cold boot near 42°C. The loaded module's probe had already failed after the capability read, so no fan command had been sent. A shutdown and a 30-second power pull cleared both. External fans (front and rear) lowered this machine's own RPM by about 400–500 total, and rear fans alone by about 100, without a 0 RPM reading while it was already up. Boot-time airflow has not been tested. After every boot, read `fan1_input`. The watchdog alerts after 15 seconds at 0 RPM and reboots once if no work is running; a second boot that is still at 0 RPM powers off. What it will and will not do is in [stopped fans](docs/usage.md#watch-for-stopped-fans).

**The mailbox stays pending.** On one EC 3.5.8 boot (6 Nov 2026), operation 0x04 stayed pending (`-110`) after about 99 reads and stayed that way until reboot. Version 0.1.7 does not send another floor command for that. It is not fixed in firmware. See [persistent pending](docs/troubleshooting.md#persistent-pending-and-service-exit-69).

**Status or unload reports `-ESTALE` (`-116`).** The floor is not a value this driver set. That is an ownership change, not a mailbox timeout. Only 4350 RPM (`0x10fe`) is the known ACPI level-100 value. Another number is not proof of who wrote it. Reboot restores the EC curve. Do not write ACPI level 0. See [an ACPI floor](docs/troubleshooting.md#daemon-restarts-forever-after-an-acpi-floor).

## Quiet curve

The default curve is unchanged: state 3 at 50°C and state 12 at 70°C. An
opt-in `quiet` curve, from [tenhkspark](https://github.com/tenhkspark/dgx-spark-fan-tuning)
and [upstream pull request 2](https://github.com/christopherowen/dgx-spark-fan-control/pull/2),
holds state 1 below 52°C and reaches state 12 at 78°C. It is for EC 3.5.8
with the pre-October SoC (the validation image is 2.155.11) when lower noise
matters more than lower temperature. EC 3.13.7 still cannot write a floor, so
the curve has no effect there.

```sh
sudo systemctl stop dgx-fan-control.service
sudo DGX_FAN_CURVE=quiet dgx-fan-control daemon
# or: dgx-fan-control daemon --curve quiet
```

A systemd drop-in can set `Environment=DGX_FAN_CURVE=quiet`. Both curves keep
the state-1 pin and never select state 0 while the daemon runs.

The service restarts every 30 seconds with no start-rate limit
(`Restart=always`, `RestartSec=30s`, `StartLimitIntervalSec=0`). A crash is
retried forever. Exit 69 is only the startup check that fan control is
unsupported, and systemd does not restart that exit. Exit 79 means a held
manual floor: the daemon refuses to start and systemd does not restart that
exit either. `module enable` on EC
3.13.7 loads telemetry and leaves the service disabled unless
`--force-service` is set. `dgx-fan-control watchdog` does not alarm when the
unit's last exit is 69. Exit 79, a held floor, is also quiet: the daemon exits
79 on every start until `automatic` clears the record. It still reports a failed unit,
and an enabled unit that is inactive for another reason.

## Firmware pairing

EC 3.5.8 with the October SoC (2.158.15, `0x02009e0f`) has completed the
fan-floor probe on both DGX Sparks in this tree's 6 Nov 2026 retest: baseline
about 2700/4050 RPM, and `set-state` changed speed while the daemon was
stopped. tenhkspark's notes and NVIDIA forum topic 386805 post 21 also
describe a mailbox timeout and 0/0 tachometers on that mix. Both outcomes
are real. If probe times out, `acpi-max` is the fallback, and only while
this module is not loaded. The two writers share one lower-floor register.
Downgrading EC 3.13.7 to 3.5.8, and blocking fwupd from putting 3.13.7
back, are in
[EC firmware pairing and downgrade](docs/installation.md#6-ec-firmware-pairing-and-downgrade).

## ACPI-only mode

Use this when fans should be driven with `acpi-max` and the kernel module
should stay unloaded. `disable` does not uninstall the driver. Full removal
is `sudo dkms remove dgx-spark-fan-control/0.1.7 --all` (the version in
`dkms.conf`).

```sh
sudo dgx-fan-control module disable
sudo dgx-fan-control module status
sudo dgx-fan-control acpi-max
```

Do not load the module again while that ACPI level is still the EC floor.
Probe refuses a floor it did not set, and it does not clear it. `module
enable` also does not start the performance service.

`module disable` stops and disables `dgx-fan-control.service`, unloads
`dgx_ec_fan_control` when it is loaded, writes
`/etc/modprobe.d/dgx-spark-fan-control-disable.conf` (`blacklist` and
`install ... /bin/false`), and comments out
`/etc/modules-load.d/dgx_ec_fan_control.conf` when that file is the one this
project installs. Running it again changes nothing else. Any other file in
`/etc/modprobe.d` that blacklists the module or install-overrides it is
reported and left untouched. `module enable` removes only the managed
blacklist, restores the modules-load.d entry, and loads the module. It does
**not** enable or start `dgx-fan-control.service`. That is a separate choice:
`sudo dgx-fan-control module enable --start-service` or
`sudo systemctl enable --now dgx-fan-control.service`. Neither `module enable`
nor `module disable` enables, starts, or stops the watchdog. On EC 3.13.7, where the
module registers telemetry only, enable loads the module and leaves the
service disabled. It prints once that fan control is unsupported and points
at `acpi-max` for EC 3.5.8. `--force-service` starts the daemon anyway.

`module status` prints whether the module is loaded, whether the managed
blacklist or another blacklist is present, whether the performance service
and the watchdog are enabled and active, whether fan control is `supported`
or `unsupported (EC 3.13.7,
telemetry only)`, and the mode (`module` or `ACPI-only`). `service-exit` is
`n/a` while the unit is running, or while it is disabled and has not failed,
including a leftover code from a run before the latest disable. When the unit
is `failed`, or enabled but inactive because its main process exited, the
field is the exit code and `ExecMainExitTimestamp`.

The watchdog does not treat a disabled, inactive service as a fault. It also
stays quiet when the unit's last exit status is 69, the startup result for
unsupported fan control, and when the last exit is 79, a held floor.
Every start exits 79 while that floor is held. `module status` reports the exit
code, with the time it happened, when the unit is failed or still enabled
and inactive. It still
alerts when the unit is `failed`, or enabled but inactive after another exit.

`acpi-max` refuses while `dgx_ec_fan_control` is loaded or the fan service is
active, because both write the same EC lower floor. `--force` writes anyway
and logs a warning. It refuses levels below 35. Level 0 stops both fans; do
not use it as a recovery step. On success it prints each Fan device's
`cur_state/max_state` (for example `cooling_device0 (Fan): 100/100`). On EC
3.13.7, or when the EC is unknown, it adds that firmware may accept the write
without changing RPM. On EC 3.5.8 it prints `check fan RPM`. It exits nonzero
when no Fan cooling device is found or a write fails.

This project does not put the module in the initramfs. `modules-load.d` loads
it after the root filesystem is mounted, so `module disable` does not run
`update-initramfs` or `dracut`. If you have separately listed
`dgx_ec_fan_control` in `/etc/initramfs-tools/modules` or a dracut
configuration, `module status` names that file. Rebuild the image yourself
(`sudo update-initramfs -u` or `sudo dracut -f`) or early boot can still load
the module.

## Development

Run `./scripts/check` (Python 3.10+ and a C compiler) for hardware-free policy,
failure-path, compiled C transaction, signing-wrapper, and source-contract tests
plus shell syntax checks. The check refuses systemd, module, firmware, and
sysfs changes. It does not run the opt-in live harness under
[research/live-check](research/live-check/README.md). GitHub Actions runs the
same checks. Kernel compilation and real EC behavior need a compatible Spark.

- [Firmware protocol](docs/protocol.md)
- [Firmware pending-state investigation](docs/firmware-pending-analysis.md)
- [Validation record](docs/validation.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Contributing](CONTRIBUTING.md)

## License

[GPL-2.0-only](LICENSE), matching the original kernel driver's SPDX identifier.
Copyright 2026 Christopher Owen.
