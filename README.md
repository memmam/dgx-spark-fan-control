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
dgx-fan-control status             # state=0/12 means NVIDIA automatic control
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
SoC firmware 2.158.15** (`0x02009e0f`) times out the FF-A/eSPI mailbox on the
first read-only query, so this driver does not bind.
Probe says so and points at the ACPI fan devices below.

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
3.13.7 are therefore expected to fail. Version 0.1.5 still accepts the exact
capability reply. Probe on that profile reads capabilities and telemetry only.
It does not send a floor command, and it does not create a cooling device.
`dgx-fan-control status` then prints `control unsupported on this firmware`.
Floor writes require `allow_unverified_writes=1` at module load, which logs a
warning. The 3.13.7 state table behind that switch is **not qualified**. Do
not enable it to test a machine whose fans have stopped.

On **EC 3.5.8 plus the October SoC**, the alternative that has been observed to
work is the ACPI fan device: `/sys/class/thermal/cooling_device*` with type
`Fan` and `max_state` 100. The BIOS maps a level to `975 + 33.75 × level` RPM.
Reported measurements: level 100 about 4320 RPM, level 30 about 1940, level 20
about 1620 with the second fan stalling, and level 0 stops both fans. Do not
write a level below 30; the observers use 40. This project does not drive those
ACPI devices. On EC 3.13.7 the same ACPI write is accepted and does not change
fan speed.

Other GB10 boards, generic kernels, and any capability reply other than the
two profiles above are rejected. The driver does not relax validation to make
an unknown reply succeed.

You need Python 3.10+, matching NVIDIA kernel headers and build tools, and an
enrolled local signing certificate when Secure Boot is enabled. DKMS is optional;
systemd provides boot loading and the optional performance service.

This is an independent, experimental project, unaffiliated with NVIDIA.
Version **0.1.5** recognizes the EC 3.13.7 capability reply and binds telemetry
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

Owner-reported on two DGX Sparks on 5 Nov 2026 (EC `0x03000d07`, kernel
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

Anyone running external airflow should read `fan1_input` after every boot. A
watchdog should treat 0 RPM on both fans with the SoC above about 70°C as a
fault. `sudo dgx-fan-control watchdog` does that from userspace. It only
reads `acpi_fan` and `thermal_zone0`. It does not need this kernel module.
The default action is `systemctl poweroff`. The same command logs and
broadcasts with `wall` when `dgx-fan-control.service` is not `active`, before
it looks at temperature. That alert does not power the machine off.

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

## Firmware pairing

The kernel module's FF-A path does not provide fan control when EC 3.5.8 is
paired with the October SoC (2.158.15). tenhkspark's firmware notes report a
relay timeout and tachometers reading 0/0 on that mix. Separately, NVIDIA
forum topic 386805 post 21 reports that ACPI `cur_state` does work on EC 3.5.8
after an EC-only downgrade onto that October SoC. `dgx-fan-control acpi-max`
is the documented route for that pairing. It is not a kernel-module path.

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
