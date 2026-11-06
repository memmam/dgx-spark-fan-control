# DGX Spark Fan Control

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
| Update an install you already have | [Updating an existing install](docs/installation.md#6-updating-an-existing-install) |
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
dgx-fan-control status             # state=0/12 (no floor; EC curve)
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

Fan-floor **control** was tested on **NVIDIA DGX Spark, board P4242**, with
**EC 3.5.8** and the pre-October SoC firmware, DGX OS 7.5.0, and kernel
`6.17.0-1029-nvidia`. Later checks used kernel `7.0.0-1019-nvidia`. Upstream
does not pin one SoC build in the compatibility statement. The validation
record for this tree used SoC 2.155.11 (`0x02009b0b`). **EC 3.5.8 with October
SoC firmware 2.158.15** (`0x02009e0f`) has done both of the following on DGX
Spark: completed probe (`pinned EC 3.5.8 capabilities fan0=1260..9000
fan1=1890..13500`, cooling device registered) and, on other boots reported
earlier, timed out the first read-only query so the driver did not bind.
A timeout is still a reason to use the ACPI fan devices. It is not proof that
this pair can never bind.

**EC 3.13.7** (`fwupdmgr` `0x03000d07`) has been observed on the same board,
including kernel `7.0.0-1024-nvidia`. The capability reply is still revision 1,
unit mode 0, but both fans report **975–4350** instead of 1260–9000 and
1890–13500. Those figures match the ACPI tach target `975 + 33.75 × level`
(level 100 is 4350). They are not a rescaling of the older RPM-mode control
ranges, which the EC converts to PWM. A public diff of the EC images found the
override handlers and their dispatch table byte-identical between 3.5.8 and
3.13.7. They return **-5 (unsupported)** or **-116 (timeout)** when an enable
bit is clear. What changed is host command **0xEB**: it now honors a permission
table that marks the fan/thermal register block read-only, so the host can no
longer set that enable bit. This is deliberate access control. Floor writes on
3.13.7 are therefore expected to fail. Version 0.1.6 still accepts the exact
capability reply. Probe on that profile reads capabilities and telemetry only.
It does not send a floor command, and it does not create a cooling device.
`dgx-fan-control status` then prints `control unsupported on this firmware` once.
Floor writes require `allow_unverified_writes=1` at module load, which logs a
warning. The 3.13.7 state table behind that switch is **not qualified**. Do
not enable it to test a machine whose fans have stopped.

On **EC 3.5.8**, ACPI `Fan` `cur_state` and this module's lower floor are the
same EC register. The BIOS maps an ACPI level to `975 + 33.75 × level` RPM.
Forum measurements: level 100 about 4320 RPM, level 30 about 1940, level 20
about 1620 with the second fan stalling, and level 0 stops both fans. **Never
write an ACPI level below 30. Level 0 stops both fans immediately and they
stay stopped; it also does not clear a foreign floor.** This command refuses
levels below 40. `acpi-max` refuses while `dgx_ec_fan_control` is loaded or
`dgx-fan-control.service` is active, unless `--force` is passed. On EC 3.13.7
the same ACPI write is accepted and does not change fan speed. An ACPI level
does not survive reboot: these machines returned to about 2700/4050 RPM
without another `acpi-max`.

Other GB10 boards, generic kernels, and any capability reply other than the
two profiles above are rejected. The driver does not relax validation to make
an unknown reply succeed.

You need Python 3.10+, matching NVIDIA kernel headers and build tools, and an
enrolled local signing certificate when Secure Boot is enabled. DKMS is optional;
systemd provides boot loading and the optional performance service.

This is an independent, experimental project, unaffiliated with NVIDIA.
Version **0.1.6** recognizes the EC 3.13.7 capability reply and binds telemetry
only, with no floor command, unless `allow_unverified_writes=1`. It tells a
probe timeout apart from that case. The default performance curve never requests state 0 while the daemon is
running. `--curve quiet` or `DGX_FAN_CURVE=quiet` selects the quieter opt-in
curve from tenhkspark (upstream pull request 2). It is for EC 3.5.8 with the
pre-October SoC, including the 2.155.11 image in the validation record, when
less noise is preferred over lower temperatures. It also never selects state 0.
A reply-guard recovery refusal is retried after the 30-second cooldown; a
foreign floor remains fatal. Version **0.1.4** keeps a loaded module on NVIDIA's
curve when the performance service is not running. State 0 is that curve. A
latched lower clamp of 0, which stops both fans and survives a warm reboot, is
cleared on load and on every automatic restore when floor writes still work.
Version **0.1.3** includes bounded in-driver recovery for the observed stale-pending,
idle-mailbox failure. It validates physical mailbox observations, submits one
read-only recovery request, checks the reply, and reconciles floor ownership
before continuing. Timed-out setters are never blindly replayed. Recovery
attempts have a shared 30-second cooldown; busy, unreadable, or inconsistent
mailboxes remain errors. The underlying firmware defect remains, and long-term
unattended reliability is not yet qualified. See
the [incident logs](docs/incident-logging.md), which retain recent transaction
timing and recovery observations without extra EC traffic, and the
[recovery instructions](docs/troubleshooting.md#persistent-pending-and-service-exit-69).
Orderly stop, suspend, reboot, and module removal request and verify automatic
control. A hard crash or transport failure can prevent restoration; manual
floors have no expiry timer. Read the [validation and limitations](docs/validation.md).

## Known issues

Reported by the user on two DGX Sparks on 5 Nov 2026 (EC `0x03000d07`, kernel
`7.0.0-1024-nvidia`, BIOS `5.36_0ACUM028`). These are observations, not a
verified cause.

Twice, a Spark's ACPI fans (`acpi_fan` hwmon `fan1_input`) read a flat 0 RPM.
Once the machine was idle, the external fan rig was off, and the SoC reached
79°C. Once it was a cold boot, about 42°C. The stock 0.1.4 module was loaded
through DKMS but probe had already failed with `-EBADMSG` after the capability
read only, so no fan command had been sent. It is not believed to be the
cause. A shutdown and a 30-second power pull cleared both events.

External chassis fans (a front and rear rig) blowing through the unit lower
the Spark's own fan RPM by about 400–500 total. Rear fans alone are about
100 RPM. The change is smooth and reversible. Toggling the rig on and off
repeatedly while the machine was running never produced a 0 RPM reading. The
suspected trigger is boot-time state: boot order against external airflow, or
the EC stopping its own fans. That has not been tested.

Anyone running external airflow should read `fan1_input` after every boot.
`dgx-fan-watchdog.service` runs `dgx-fan-control watchdog` at boot. It does
not depend on the performance daemon or this kernel module, so disabling
`dgx-fan-control.service` leaves it running. `sudo ./scripts/install-userland`
enables it; `--no-watchdog` skips that. The same `install` commands, including
the health check and the motd snippet, are written out in
[startup modes](docs/installation.md#5-choose-your-startup-mode). Copying only
the performance unit leaves `status` at `watchdog-enabled: not-found`. The
watchdog reads `acpi_fan` and the hottest thermal zone. If every fan stays
at 0 RPM for 15 seconds it alerts and tries one repair: ACPI level 100 if
the module is unloaded, or the module's maximum state if it is loaded. It
never writes level 0. About 60 seconds after the zeros began, with no work
in progress, it reboots once. The default signal is an `nvidia-smi` compute
process, and only when `BUSY_CHECK` is unset. A persistent server keeps that
default busy, so the reboot stays deferred and only a true zero at 85°C
powers the machine off. A second boot that is still at 0 RPM powers off.
Work in progress waits, and a true zero at 85°C still powers off. Spinning
fans are not powered off for temperature. A foreign floor reboots once the
same way, without the repair; the next boot records needs-intervention
instead of powering off. `dgx-fan-health.service` checks
fans and thermals after boot and retries for 45 seconds.
`dgx-fan-control health` prints the same checks in one pass and does not
need root. Userspace cannot beat a rise of about 4°C/s to the kernel
critical trip near 104°C. Keeping the fans running, and pre-cooling, are
the defenses. See [usage](docs/usage.md#watch-for-stopped-fans). The same command
also logs and broadcasts
when `dgx-fan-control.service` is not `active`, before it looks at
temperature. That alert does not power the machine off. The cold alert and
the level-100 write have not been run on hardware yet.

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
unsupported, and systemd does not restart that exit. `module enable` on EC
3.13.7 loads telemetry and leaves the service disabled unless
`--force-service` is set. `dgx-fan-control watchdog` does not alarm when the
unit's last exit is 69. It still reports a failed unit, and an enabled unit
that is inactive for another reason.

## Firmware pairing

EC 3.5.8 with the October SoC (2.158.15, `0x02009e0f`) has completed the
fan-floor probe on both DGX Sparks in this tree's 6 Nov 2026 retest: baseline
about 2700/4050 RPM, and `set-state` changed speed while the daemon was
stopped. tenhkspark's notes and NVIDIA forum topic 386805 post 21 also
describe a mailbox timeout and 0/0 tachometers on that mix. Both outcomes
are real. If probe times out, `acpi-max` is the fallback, and only while
this module is not loaded. The two writers share one lower-floor register.

## ACPI-only mode

Use this when fans should be driven with `acpi-max` and the kernel module
should stay unloaded. `disable` does not uninstall the driver. Full removal
is `sudo dkms remove dgx-spark-fan-control/0.1.6 --all` (the version in
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
unsupported fan control. `module status` reports that exit, with the time it
happened, when the unit is failed or still enabled and inactive. It still
alerts when the unit is `failed`, or enabled but inactive after another exit.

`acpi-max` refuses while `dgx_ec_fan_control` is loaded or the fan service is
active, because both write the same EC lower floor. `--force` writes anyway
and logs a warning. It refuses levels below 40. Level 0 stops both fans; do
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
plus shell syntax checks. GitHub Actions runs the same checks. Kernel
compilation and real EC behavior need a compatible Spark.

- [Firmware protocol](docs/protocol.md)
- [Firmware pending-state investigation](docs/firmware-pending-analysis.md)
- [Validation record](docs/validation.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Contributing](CONTRIBUTING.md)

## License

[GPL-2.0-only](LICENSE), matching the original kernel driver's SPDX identifier.
Copyright 2026 Christopher Owen.
